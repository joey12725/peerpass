"""Discord transport contract tests with fixture interactions. No Discord login."""

import asyncio
import unittest
from types import SimpleNamespace as NS

from helpers import GatewayCase

discord = __import__('pytest').importorskip('discord')
from peerpass.transports.discord import DiscordTransport  # noqa: E402

OWNER = 900000000000000001
GUILD = 900000000000000002
BOT = 900000000000000003


class Interaction:
    def __init__(self, user=OWNER, ident=100, fail=False):
        self.user, self.id, self.channel_id = NS(id=user), ident, 5
        self.sent, self.deferred, self.fail = [], [], fail
        self.response = NS(send_message=self.send, defer=self.defer)
        self.followup = NS(send=self.send)

    async def send(self, text, **kwargs):
        if self.fail:
            raise RuntimeError('delivery failed')
        self.sent.append((text, kwargs))

    async def defer(self, **kwargs):
        self.deferred.append(kwargs)


class Channel:
    def __init__(self, ident=7):
        self.id, self.sent = ident, []

    async def send(self, text, **kwargs):
        self.sent.append(text)


class DM(discord.DMChannel):
    def __init__(self):  # bypass discord.py state; isinstance checks are all we need
        self.id, self.sent = 8, []

    async def send(self, text, **kwargs):
        self.sent.append(text)


class DiscordTests(GatewayCase, unittest.IsolatedAsyncioTestCase):
    def make(self):
        client = discord.Client(intents=discord.Intents.none())
        t = DiscordTransport(self.gw, {'owner_ids': [str(OWNER)], 'guild_ids': [str(GUILD)], 'wait_seconds': 5}, client=client)
        client._connection.user = NS(id=BOT)
        return t

    async def asyncTearDown(self):
        await asyncio.sleep(0)

    async def test_pass_is_a_real_guild_command_with_bounded_args(self):
        t = self.make()
        cmd = t.tree.get_command('pass', guild=discord.Object(id=GUILD))
        self.assertIsNotNone(cmd)
        self.assertEqual([p.name for p in cmd.parameters], ['args'])

    async def test_owner_slash_issue_is_ephemeral(self):
        t = self.make()
        it = Interaction()
        await t.slash(it, 'issue delegate "Fabricated coding agent" ttl=6h')
        self.assertEqual(it.deferred, [{'ephemeral': True, 'thinking': True}])
        text, kwargs = it.sent[0]
        self.assertTrue(kwargs['ephemeral'])
        token = next(line for line in text.splitlines() if line.startswith('pp_'))
        self.assertEqual(self.store.authenticate(token)['label'], 'Fabricated coding agent')

    async def test_non_owner_refused_before_store(self):
        t = self.make()
        self.store.owner = lambda *a, **k: self.fail('store touched')
        it = Interaction(user=OWNER + 1)
        await t.slash(it, 'issue delegate x')
        self.assertEqual(it.deferred, [])
        self.assertTrue(it.sent[0][1]['ephemeral'])
        self.assertNotIn('pp_', it.sent[0][0])

    async def test_failed_delivery_revokes_and_duplicates_do_not_mint(self):
        t = self.make()
        it = Interaction(fail=True)
        await t.slash(it, 'issue delegate lost')
        rows = self.store.owner('grants', {'all': True})
        self.assertEqual([r['revoked'] for r in rows], [1])
        await t.slash(Interaction(ident=200), 'issue delegate once')
        await t.slash(Interaction(ident=200), 'issue delegate once')
        self.assertEqual(len(self.store.owner('grants')), 1)

    async def test_bound_bot_in_channel_by_mention_and_dm(self):
        t = self.make()
        self.issue('delegate', bind=['discord:4242'])
        ch = Channel()
        msg = NS(id=1, author=NS(id=4242), content=f'<@{BOT}> ship it?', channel=ch, mentions=[NS(id=BOT)])
        await t.message(msg)
        self.assertEqual(ch.sent, ['safe reply'])
        self.assertEqual(self.host.calls[0].message, 'ship it?')
        quiet = NS(id=2, author=NS(id=4242), content='no mention', channel=ch, mentions=[])
        await t.message(quiet)
        self.assertEqual(len(self.host.calls), 1)
        dm = DM()
        await t.message(NS(id=3, author=NS(id=4242), content='in a DM', channel=dm, mentions=[]))
        self.assertEqual(dm.sent, ['safe reply'])
        await t.message(NS(id=4, author=NS(id=BOT), content='my own echo', channel=dm, mentions=[]))
        self.assertEqual(len(self.host.calls), 2)
