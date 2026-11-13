"""Discord transport (requires the ``discord`` extra).

* ``/pass`` is a guild or global application command. Replies are ephemeral, so a new pass is
  visible only to the owner who ran it, never posted to channel history.
* Visiting agents talk to the bot in a DM, or by mentioning it in a shared channel. Other bots
  are welcome: that is the point of agent-to-agent access.

Settings (``[transports.discord]``):
    token_env      env var holding the bot token
    owner_ids      Discord user ids allowed to run /pass
    guild_ids      optional guilds to register /pass in (instant); otherwise global
    wait_seconds   how long to wait for the host agent before replying "still working"
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading

import discord
from discord import app_commands

from ..config import secret
from .base import REFUSAL, ChatEvent, ChatRouter

log = logging.getLogger('peerpass.discord')
NO_MENTIONS = discord.AllowedMentions.none()


class DiscordTransport:
    platform = 'discord'

    def __init__(self, gateway, settings: dict, client: discord.Client | None = None):
        self.router = ChatRouter(
            gateway,
            self.platform,
            settings.get('owner_ids', []),
            settings.get('wait_seconds', 120),
            settings.get('timezone', 'UTC'),
        )
        self.settings = settings
        self.guild_ids = [int(g) for g in settings.get('guild_ids', [])]
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        self.client = client or discord.Client(intents=intents)
        self.tree = app_commands.CommandTree(self.client)
        self.wire()

    def wire(self) -> None:
        @app_commands.command(name='pass', description='Issue, list or revoke agent access passes (owner only)')
        @app_commands.describe(args='issue <profile> <label> [ttl=12h] | list | revoke <id> | reviews | help')
        async def pass_command(interaction: discord.Interaction, args: str = ''):
            await self.slash(interaction, args)

        guilds = [discord.Object(id=g) for g in self.guild_ids]
        self.tree.add_command(pass_command, guilds=guilds or None)
        self.command = pass_command

        @self.client.event
        async def on_ready():
            for g in guilds:
                await self.tree.sync(guild=g)
            if not guilds:
                await self.tree.sync()
            log.info('discord transport ready')

        @self.client.event
        async def on_message(message: discord.Message):
            await self.message(message)

    async def slash(self, interaction, args: str) -> None:
        if str(interaction.user.id) not in self.router.owners:
            # Refuse before touching the store, privately.
            await interaction.response.send_message(REFUSAL, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        loop = asyncio.get_running_loop()

        def reply(text: str) -> None:
            for i in range(0, max(len(text), 1), 1900):
                fut = asyncio.run_coroutine_threadsafe(
                    interaction.followup.send(text[i : i + 1900], ephemeral=True, allowed_mentions=NO_MENTIONS), loop
                )
                fut.result(30)

        ev = ChatEvent(
            self.platform,
            f'interaction:{interaction.id}',
            str(interaction.user.id),
            str(getattr(interaction, 'channel_id', '') or ''),
            args,
            private=True,
            command=True,
        )
        try:
            await asyncio.to_thread(self.router.handle, ev, reply)
        except Exception:  # noqa: BLE001 - the router has already revoked any undelivered pass
            log.warning('discord /pass delivery failed')

    async def message(self, message) -> None:
        me = self.client.user
        if me is None or message.author.id == me.id or not message.content:
            return
        private = isinstance(message.channel, discord.DMChannel)
        mentioned = any(u.id == me.id for u in message.mentions)
        text = re.sub(rf'<@!?{me.id}>', '', message.content).strip()
        loop = asyncio.get_running_loop()

        def reply(out: str) -> None:
            for i in range(0, max(len(out), 1), 1900):
                fut = asyncio.run_coroutine_threadsafe(
                    message.channel.send(out[i : i + 1900], allowed_mentions=NO_MENTIONS), loop
                )
                fut.result(30)

        def delete() -> None:
            if not private:  # bots cannot delete the other side's DM messages
                asyncio.run_coroutine_threadsafe(message.delete(), loop).result(30)

        ev = ChatEvent(
            self.platform,
            str(message.id),
            str(message.author.id),
            str(message.channel.id),
            text,
            private=private,
            addressed=private or mentioned,
            delete=delete,
        )
        try:
            await asyncio.to_thread(self.router.handle, ev, reply)
        except Exception:  # noqa: BLE001
            log.warning('discord message handling failed')

    def start(self) -> threading.Thread:
        token = secret(self.settings, 'token')
        t = threading.Thread(target=lambda: self.client.run(token, log_handler=None), name='peerpass-discord', daemon=True)
        t.start()
        return t
