"""Reviewed consultations: private reasoning, an independent release check, and owner review."""

import json
import unittest

from helpers import GatewayCase
from peerpass.review import (
    DECLINE_MESSAGE,
    REASONER_POLICY,
    consult,
    credential_shaped,
    guard_tools,
    parse_decision,
    release_decision,
)
from peerpass.store import Denied


def decision(kind='answer', answer='Tuesday evening works.', note=''):
    return json.dumps({'decision': kind, 'answer': answer, 'review_note': note})


REQUEST = dict(
    profile='friend',
    label='Sam',
    purpose='dinner',
    audience='A friend.',
    disclosure_policy='',
    question='When can we meet?',
    history=[],
)


class PolicyTests(unittest.TestCase):
    def test_reasoner_gets_tools_reviewer_gets_none(self):
        calls = []

        def model(system, user, tools):
            calls.append((system, json.loads(user), tools))
            return decision()

        tools = frozenset({'memory.search', 'calendar.free_busy'})
        r = consult(model, REQUEST, host='Atlas', owner='Alex', tools=tools)
        self.assertEqual(r['answer'], 'Tuesday evening works.')
        self.assertEqual(calls[0][2], tools)
        self.assertEqual(calls[1][2], frozenset())
        self.assertNotIn('candidate', calls[0][1])
        self.assertEqual(calls[1][1]['candidate']['answer'], 'Tuesday evening works.')
        self.assertIn('Atlas', calls[0][0])
        self.assertTrue(calls[1][0].startswith('You are the independent release reviewer'))

    def test_separate_reviewer_adapter_is_used(self):
        seen = []
        consult(
            lambda *a: decision(),
            REQUEST,
            host='Atlas',
            owner='Alex',
            tools=frozenset(),
            reviewer=lambda s, u, t: seen.append(t) or decision(),
        )
        self.assertEqual(seen, [frozenset()])

    def test_reviewer_cannot_promote_or_modify_candidate(self):
        self.assertEqual(release_decision(decision('needs_owner'), decision())['decision'], 'needs_owner')
        self.assertEqual(release_decision(decision(), decision(answer='Secret explanation'))['decision'], 'needs_owner')
        self.assertEqual(release_decision(decision(), decision('decline', ''))['decision'], 'decline')
        self.assertEqual(release_decision(decision('decline', ''), decision())['decision'], 'decline')

    def test_credential_shapes_and_malformed_decisions_fail_closed(self):
        for text in (
            'pp_' + 'a' * 40,
            'ghp_' + 'b' * 36,
            'sk-proj-' + 'c' * 30,
            'xoxb-' + 'd' * 30,
            'Bearer ' + 'e' * 30,
            '-----BEGIN OPENSSH PRIVATE KEY-----',
        ):
            self.assertTrue(credential_shaped(text), text)
            self.assertEqual(release_decision(decision(answer=text), decision(answer=text))['decision'], 'needs_owner')
        self.assertFalse(credential_shaped('Tuesday at 7 works for dinner.'))
        self.assertTrue(credential_shaped('acme_' + 'f' * 40, token_prefix='acme_'))
        for text in (
            'private raw output',
            '{"decision":"answer","answer":"hi"}',
            decision() + ' trailing',
            decision('execute'),
            decision('answer', '   '),
            json.dumps([1]),
        ):
            with self.assertRaises((ValueError, TypeError)):
                parse_decision(text)

    def test_tool_guard_blocks_anything_outside_the_allowlist(self):
        allowed = frozenset({'memory.search', 'memory.get'})
        for name in ['terminal', 'execute_code', 'delegate_task', 'memory.write', 'send_email', 'tool_call']:
            with self.assertRaises(PermissionError):
                guard_tools([name], allowed)
        guard_tools(['memory.search', 'memory.get'], allowed)
        with self.assertRaises(PermissionError):
            guard_tools(['memory.search'], frozenset())

    def test_policy_text_has_no_unfilled_placeholders(self):
        text = REASONER_POLICY.format(host='Atlas', owner='Alex')
        self.assertNotIn('{host}', text)
        self.assertIn('"decision":"answer|needs_owner|decline"', text)


class GatewayReviewTests(GatewayCase):
    def setUp(self):
        super().setUp()
        self.grant = self.store.issue(
            {
                'profile': 'colleague',
                'label': 'team agent',
                'purpose': 'project coordination',
                'disclosure_policy': 'Never name clients.',
            }
        )

    def run_job(self, reply, text='Question', key='one'):
        self.host.reply = reply
        j, _ = self.store.submit(self.grant['token'], {'message': text, 'request_id': key})
        self.gw.run(j['job_id'])
        return j['job_id'], self.store.job(self.grant['token'], j['job_id'])

    def test_held_draft_never_public_and_owner_can_release_exact_answer(self):
        jid, result = self.run_job(decision('needs_owner', 'PRIVATE-DRAFT', 'Private owner note'))
        self.assertEqual(result['status'], 'needs_owner')
        self.assertEqual(result['result'], self.gw.held_message)
        self.assertNotIn('PRIVATE', json.dumps(result))
        reviews = self.store.owner('reviews')
        self.assertEqual(reviews[0]['draft'], 'PRIVATE-DRAFT')
        with self.assertRaises(Denied):
            self.store.owner('release', {'job_id': jid, 'decision': 'answer', 'answer': 'pp_' + 'x' * 40})
        self.store.owner('release', {'job_id': jid, 'decision': 'answer', 'answer': 'Here is the approved summary.'})
        self.assertEqual(self.store.job(self.grant['token'], jid)['result'], 'Here is the approved summary.')
        self.assertEqual(self.store.owner('reviews'), [])

    def test_owner_decline_returns_neutral_refusal(self):
        jid, _ = self.run_job(decision('needs_owner', 'draft'))
        self.store.owner('release', {'job_id': jid, 'decision': 'decline'})
        r = self.store.job(self.grant['token'], jid)
        self.assertEqual((r['status'], r['result']), ('declined', DECLINE_MESSAGE))

    def test_failures_and_malformed_output_hold_for_owner_never_fall_back(self):
        def boom(request):
            raise RuntimeError('host down')

        for i, reply in enumerate([boom, 'not json', json.dumps({'decision': 'answer', 'answer': 'hi'})]):
            _, r = self.run_job(reply, key=str(i))
            self.assertEqual((r['status'], r['result']), ('needs_owner', self.gw.held_message))
        self.assertNotIn('direct', {req.mode for req in self.host.calls})

    def test_visitor_cannot_supply_policy_fields(self):
        for field in ('profile', 'disclosure_policy', 'history', 'label', 'audience', 'tools'):
            with self.assertRaises(Denied):
                self.store.submit(self.grant['token'], {'message': 'x', 'request_id': 'x', field: 'override'})

    def test_owner_policy_and_only_released_history_are_forwarded(self):
        self.run_job(decision('needs_owner', 'PRIVATE-DRAFT', 'PRIVATE-NOTE'))
        self.run_job(decision(), text='next', key='two')
        envelope = json.loads(self.host.calls[-2].message)
        self.assertEqual(envelope['disclosure_policy'], 'Never name clients.')
        self.assertEqual(envelope['profile'], 'colleague')
        self.assertIn('whole team', envelope['audience'])
        self.assertNotIn('PRIVATE-', json.dumps(envelope['history']))
        self.assertEqual(envelope['history'][0]['answer'], self.gw.held_message)

    def test_revocation_during_consultation_discards_draft(self):
        def reply(request):
            self.store.owner('revoke', {'grant_id': self.grant['grant_id']})
            return decision('needs_owner', 'private')

        self.host.reply = reply
        j, _ = self.store.submit(self.grant['token'], {'message': 'x', 'request_id': 'one'})
        self.gw.run(j['job_id'])
        self.assertEqual(self.store.owner('reviews'), [])
        with self.assertRaises(Denied):
            self.store.owner('release', {'job_id': j['job_id'], 'decision': 'answer', 'answer': 'x'})

    def test_one_reviewed_consultation_at_a_time(self):
        other = self.store.issue({'profile': 'friend', 'label': 'other', 'purpose': 'x'})
        self.store.submit(self.grant['token'], {'message': 'x', 'request_id': 'one'})
        with self.assertRaises(Denied) as c:
            self.store.submit(other['token'], {'message': 'x', 'request_id': 'one'})
        self.assertEqual(c.exception.status, 429)
        direct = self.store.issue({'profile': 'delegate', 'label': 'coder'})
        self.store.submit(direct['token'], {'message': 'x', 'request_id': 'one'})

    def test_pending_review_pruned_with_job_retention(self):
        self.run_job(decision('needs_owner', 'private'))
        self.now += 86401
        self.store.cleanup()
        self.assertEqual(self.store.owner('reviews'), [])
