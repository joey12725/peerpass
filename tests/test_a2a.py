"""A2A interop: agent card plus message/send and tasks/get on the same pass rules."""

import json

from helpers import GatewayCase
from peerpass.a2a import agent_card
from peerpass.store import Denied


def send(text, mid='m-1'):
    return {
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'message/send',
        'params': {'message': {'role': 'user', 'messageId': mid, 'kind': 'message', 'parts': [{'kind': 'text', 'text': text}]}},
    }


class A2ATests(GatewayCase):
    def test_card_shape(self):
        card = agent_card(self.config)
        for key in ('protocolVersion', 'name', 'url', 'capabilities', 'skills', 'securitySchemes', 'security'):
            self.assertIn(key, card)
        self.assertTrue(card['url'].endswith('/a2a'))

    def test_send_then_get_completed_task(self):
        g = self.issue()
        r = self.gw.public('POST', '/a2a', g['token'], send('hello over a2a'))
        task = r['result']
        self.assertEqual(task['kind'], 'task')
        self.assertIn(task['status']['state'], ('submitted', 'working', 'completed'))
        self.gw.wait(g['token'], task['id'], 5, poll=0.02)
        got = self.gw.public(
            'POST', '/a2a', g['token'], {'jsonrpc': '2.0', 'id': 2, 'method': 'tasks/get', 'params': {'id': task['id']}}
        )['result']
        self.assertEqual(got['status']['state'], 'completed')
        self.assertEqual(got['artifacts'][0]['parts'][0]['text'], 'safe reply')
        self.assertEqual(self.host.calls[0].message, 'hello over a2a')

    def test_message_id_is_the_idempotency_key(self):
        g = self.issue()
        a = self.gw.public('POST', '/a2a', g['token'], send('once', 'same'))['result']['id']
        b = self.gw.public('POST', '/a2a', g['token'], send('once', 'same'))['result']['id']
        self.assertEqual(a, b)

    def test_held_and_declined_states(self):
        g = self.issue('friend')
        self.host.reply = json.dumps({'decision': 'needs_owner', 'answer': 'secret draft', 'review_note': ''})
        task = self.gw.public('POST', '/a2a', g['token'], send('sensitive?'))['result']
        self.gw.wait(g['token'], task['id'], 5, poll=0.02)
        got = self.gw.public(
            'POST', '/a2a', g['token'], {'jsonrpc': '2.0', 'id': 3, 'method': 'tasks/get', 'params': {'id': task['id']}}
        )['result']
        self.assertEqual(got['status']['state'], 'working')
        self.assertNotIn('secret draft', json.dumps(got))
        self.store.owner('release', {'job_id': task['id'], 'decision': 'decline'})
        got = self.gw.public(
            'POST', '/a2a', g['token'], {'jsonrpc': '2.0', 'id': 4, 'method': 'tasks/get', 'params': {'id': task['id']}}
        )['result']
        self.assertEqual(got['status']['state'], 'rejected')

    def test_errors(self):
        g = self.issue()
        with self.assertRaises(Denied) as c:
            self.gw.public('POST', '/a2a', 'pp_wrong', send('x'))
        self.assertEqual(c.exception.status, 401)
        bad = self.gw.public(
            'POST', '/a2a', g['token'], {'jsonrpc': '2.0', 'id': 1, 'method': 'tasks/get', 'params': {'id': 'a' * 32}}
        )
        self.assertEqual(bad['error']['code'], -32001)
        self.assertEqual(self.gw.public('POST', '/a2a', g['token'], {'jsonrpc': '1.0'})['error']['code'], -32600)
        self.assertEqual(
            self.gw.public('POST', '/a2a', g['token'], {'jsonrpc': '2.0', 'id': 1, 'method': 'nope'})['error']['code'], -32601
        )
        other = self.issue()
        mine = self.gw.public('POST', '/a2a', g['token'], send('x'))['result']['id']
        stolen = self.gw.public(
            'POST', '/a2a', other['token'], {'jsonrpc': '2.0', 'id': 1, 'method': 'tasks/get', 'params': {'id': mine}}
        )
        self.assertEqual(stolen['error']['code'], -32001)
