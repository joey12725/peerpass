"""Platform-neutral chat routing: owner commands, private delivery, bindings and visitors."""

from helpers import GatewayCase
from peerpass.transports.base import PRIVATE_ONLY, REFUSAL, ChatEvent, ChatRouter

OWNER = '111'
VISITOR = '222'


class RouterCase(GatewayCase):
    def setUp(self):
        super().setUp()
        self.router = ChatRouter(self.gw, 'testchat', [OWNER], wait_seconds=5)
        self.n = 0

    def say(self, text, user=OWNER, private=True, addressed=True, command=False, fail=False, delete=None):
        self.n += 1
        out = []

        def reply(t):
            if fail:
                raise RuntimeError('delivery failed')
            out.append(t)

        self.router.handle(ChatEvent('testchat', str(self.n), user, 'chat-1', text, private, addressed, command, delete), reply)
        return out

    def token_from(self, text):
        return next(line for line in text.splitlines() if line.startswith('pp_'))


class OwnerCommandTests(RouterCase):
    def test_owner_issues_privately_with_full_handoff(self):
        out = self.say('/pass issue friend Fabricated Agent ttl=2d calls=12 purpose="plan a dinner"')
        text = out[0]
        token = self.token_from(text)
        g = self.store.authenticate(token)
        self.assertEqual(
            (g['profile'], g['label'], g['max_calls'], g['purpose']), ('friend', 'Fabricated Agent', 12, 'plan a dinner')
        )
        self.assertEqual(g['expires'] - self.now, 2 * 86400)
        for expected in ('PEERPASS_URL', 'PEERPASS_TOKEN', '/client.py', 'agent-card.json', 'redeem'):
            self.assertIn(expected, text)

    def test_non_owner_never_reaches_the_store(self):
        self.store.owner = lambda *a, **k: self.fail('store touched')
        for text in ('/pass issue delegate x', '/pass list', '/pass revoke abcd', 'pass reviews'):
            self.assertEqual(self.say(text, user=VISITOR), [REFUSAL])
        self.assertEqual(self.say('issue delegate x', user=VISITOR, command=True), [REFUSAL])

    def test_issue_refused_in_shared_channel(self):
        self.assertEqual(self.say('/pass issue delegate x', private=False), [PRIVATE_ONLY])
        self.assertEqual(self.store.owner('grants'), [])

    def test_failed_private_delivery_revokes_that_exact_pass(self):
        keep = self.issue()
        with self.assertRaises(RuntimeError):
            self.say('/pass issue delegate coder', fail=True)
        rows = self.store.owner('grants', {'all': True})
        lost = next(r for r in rows if r['label'] == 'coder')
        self.assertEqual(lost['revoked'], 1)
        self.store.authenticate(keep['token'])

    def test_duplicate_event_cannot_mint_twice(self):
        ev = ChatEvent('testchat', 'same-id', OWNER, 'c', '/pass issue delegate x', True)
        out = []
        self.router.handle(ev, out.append)
        self.router.handle(ev, out.append)
        self.assertEqual(len(out), 1)
        self.assertEqual(len(self.store.owner('grants')), 1)

    def test_list_revoke_audit_and_help(self):
        g = self.issue(label='Fabricated helper')
        self.assertIn('Fabricated helper', self.say('/pass list')[0])
        self.assertIn('Revoked', self.say(f'/pass revoke {g["grant_id"][:8]}')[0])
        self.assertIn('revoked', self.say('/pass audit')[0])
        self.assertIn('Owner commands', self.say('/pass')[0])
        self.assertIn('Unknown profile', self.say('/pass issue nope x')[0])
        self.assertIn('ttl', self.say('/pass issue friend x ttl=soon')[0])

    def test_reviews_and_release_from_chat(self):
        import json

        g = self.issue('friend')
        self.host.reply = json.dumps({'decision': 'needs_owner', 'answer': 'draft', 'review_note': 'check'})
        j, _ = self.store.submit(g['token'], {'message': 'Is Friday free?', 'request_id': 'q'})
        self.gw.run(j['job_id'])
        self.assertEqual(self.say('/pass reviews', private=False), [PRIVATE_ONLY])
        self.assertIn('Is Friday free?', self.say('/pass reviews')[0])
        self.say(f'/pass release {j["job_id"]} answer Friday works after 6.')
        self.assertEqual(self.store.job(g['token'], j['job_id'])['result'], 'Friday works after 6.')


class VisitorTests(RouterCase):
    def test_redeem_in_dm_binds_and_then_messages_route(self):
        g = self.issue('delegate')
        deleted = []
        out = self.say('redeem ' + g['token'], user=VISITOR, delete=lambda: deleted.append(True))
        self.assertIn('Linked', out[0])
        self.assertEqual(deleted, [True])
        self.assertEqual(self.say('please summarize the plan', user=VISITOR), ['safe reply'])
        self.assertEqual(self.host.calls[-1].message, 'please summarize the plan')
        self.assertEqual(self.host.calls[-1].grant['id'], g['grant_id'])

    def test_pass_pasted_in_shared_channel_is_burned(self):
        g = self.issue('friend')
        deleted = []
        out = self.say(
            f'here is my pass {g["token"]} lol', user=VISITOR, private=False, addressed=False, delete=lambda: deleted.append(1)
        )
        self.assertIn('revoked', out[0])
        self.assertEqual(deleted, [1])
        self.denied(401, self.store.authenticate, g['token'])
        g2 = self.issue('friend')
        self.say('redeem ' + g2['token'], user=VISITOR, private=False)
        self.denied(401, self.store.authenticate, g2['token'])

    def test_unbound_visitor_gets_hint_in_dm_and_silence_in_channels(self):
        self.assertIn('redeem', self.say('hello', user=VISITOR)[0])
        self.assertEqual(self.say('hello', user=VISITOR, private=False), [])
        self.assertEqual(self.host.calls, [])

    def test_unaddressed_group_chatter_is_ignored(self):
        self.issue('delegate', bind=['testchat:' + VISITOR])
        self.assertEqual(self.say('just chatting', user=VISITOR, private=False, addressed=False), [])
        self.assertEqual(self.say('@bot do the thing', user=VISITOR, private=False, addressed=True), ['safe reply'])

    def test_bindings_are_per_platform_and_revocation_unbinds(self):
        g = self.issue('delegate', bind=['otherchat:' + VISITOR])
        self.assertIn('redeem', self.say('hi', user=VISITOR)[0])
        self.store.owner('bind', {'grant_id': g['grant_id'], 'subject': 'testchat:' + VISITOR})
        self.assertEqual(self.say('hi', user=VISITOR), ['safe reply'])
        self.store.owner('revoke', {'grant_id': g['grant_id']})
        self.assertIn('redeem', self.say('hi again', user=VISITOR)[0])

    def test_identity_cannot_be_bound_to_two_live_passes(self):
        self.issue('delegate', bind=['testchat:' + VISITOR])
        other = self.issue('friend')
        self.assertIn('already bound', self.say('redeem ' + other['token'], user=VISITOR)[0])

    def test_budget_errors_are_relayed_and_slow_jobs_report_status(self):
        g = self.issue('delegate', max_calls=1, bind=['testchat:' + VISITOR])
        self.router.wait_seconds = 0
        self.gw.submit = lambda cred, body: {'job_id': 'f' * 32, 'status': 'queued'}
        self.gw.wait = lambda cred, jid, t: {'job_id': jid, 'status': 'running', 'result': None}
        self.assertIn('status ' + 'f' * 32, self.say('slow one', user=VISITOR)[0])
        del self.gw.submit, self.gw.wait
        self.router.wait_seconds = 5
        self.assertEqual(self.say('first', user=VISITOR), ['safe reply'])
        self.assertIn('budget', self.say('second', user=VISITOR)[0])
        self.assertEqual(self.store.me(g['token'])['calls'], 1)
