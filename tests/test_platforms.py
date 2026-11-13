"""Slack, Telegram and Matrix transports with fake platform APIs."""

import hashlib
import hmac
import json
import os
import time
import urllib.parse
from unittest.mock import patch

from helpers import GatewayCase
from peerpass.server import http_server
from peerpass.transports.matrix import MatrixTransport
from peerpass.transports.slack import SlackTransport, verify
from peerpass.transports.telegram import TelegramTransport


class FakeApi:
    def __init__(self, responses=None):
        self.calls, self.responses = [], responses or {}

    def __call__(self, method, path, body=None, timeout=None):
        self.calls.append((method, path, body))
        for prefix, value in self.responses.items():
            if path.startswith(prefix):
                return value(path, body) if callable(value) else value
        return {'ok': True}

    def sent(self, kind):
        return [c for c in self.calls if kind in c[1]]


SIGNING = 'fabricated-signing-secret'


def slack_headers(body: bytes, ts=None):
    ts = str(int(ts or time.time()))
    sig = 'v0=' + hmac.new(SIGNING.encode(), b'v0:' + ts.encode() + b':' + body, hashlib.sha256).hexdigest()
    return {'X-Slack-Request-Timestamp': ts, 'X-Slack-Signature': sig}


class SlackTests(GatewayCase):
    def setUp(self):
        super().setUp()
        self.api = FakeApi()
        with patch.dict(os.environ, {'SLACK_SIGNING': SIGNING}):
            self.slack = SlackTransport(
                self.gw,
                {'signing_secret_env': 'SLACK_SIGNING', 'owner_ids': ['U0OWNER'], 'bot_user_id': 'U0BOT', 'wait_seconds': 5},
                api=self.api,
            )
        self.slack.background = False

    def command(self, text, user='U0OWNER', trigger='t1'):
        body = urllib.parse.urlencode(
            {'command': '/pass', 'text': text, 'user_id': user, 'channel_id': 'C0SHARED', 'trigger_id': trigger}
        ).encode()
        status, _, data = self.slack.on_command(slack_headers(body), body)
        return status, json.loads(data)

    def event(self, event, event_id='Ev1'):
        body = json.dumps({'type': 'event_callback', 'event_id': event_id, 'event': event}).encode()
        return self.slack.on_events(slack_headers(body), body)

    def test_signature_verification(self):
        body = b'payload'
        self.assertTrue(verify(SIGNING.encode(), slack_headers(body), body))
        self.assertFalse(verify(SIGNING.encode(), slack_headers(body, ts=time.time() - 600), body))
        self.assertFalse(verify(b'other', slack_headers(body), body))
        status, _, _ = self.slack.on_command({'X-Slack-Signature': 'v0=bad', 'X-Slack-Request-Timestamp': '1'}, body)
        self.assertEqual(status, 401)

    def test_slash_command_issues_ephemerally(self):
        status, reply = self.command('issue colleague "Fabricated PM agent" ttl=7d')
        self.assertEqual((status, reply['response_type']), (200, 'ephemeral'))
        token = next(t for t in reply['text'].split() if t.startswith('pp_'))
        self.assertEqual(self.store.authenticate(token)['label'], 'Fabricated PM agent')
        _, reply = self.command('issue delegate x', user='U0SOMEONE', trigger='t2')
        self.assertIn('Only the owner', reply['text'])

    def test_url_verification(self):
        body = json.dumps({'type': 'url_verification', 'challenge': 'abc123'}).encode()
        _, _, data = self.slack.on_events(slack_headers(body), body)
        self.assertEqual(json.loads(data), {'challenge': 'abc123'})

    def test_dm_from_bound_bot_routes_and_replies_in_thread(self):
        self.issue('delegate', bind=['slack:B0VISITOR'])
        self.event(
            {
                'type': 'message',
                'subtype': 'bot_message',
                'bot_id': 'B0VISITOR',
                'channel': 'D0DM',
                'channel_type': 'im',
                'text': 'status report please',
                'thread_ts': '1.2',
            }
        )
        self.assertEqual(self.host.calls[0].message, 'status report please')
        self.assertEqual(
            self.api.sent('chat.postMessage')[0][2],
            {'channel': 'D0DM', 'text': 'safe reply', 'unfurl_links': False, 'thread_ts': '1.2'},
        )

    def test_mentions_stripped_own_messages_and_channel_chatter_ignored(self):
        self.issue('delegate', bind=['slack:U0VISITOR'])
        self.event({'type': 'message', 'user': 'U0BOT', 'channel': 'D0DM', 'channel_type': 'im', 'text': 'loop?'}, 'E1')
        self.event({'type': 'message', 'user': 'U0VISITOR', 'channel': 'C0', 'channel_type': 'channel', 'text': 'hi'}, 'E2')
        self.assertEqual(self.host.calls, [])
        self.event({'type': 'app_mention', 'user': 'U0VISITOR', 'channel': 'C0', 'text': '<@U0BOT> hi'}, 'E3')
        self.assertEqual(self.host.calls[0].message, 'hi')

    def test_routes_mount_on_the_http_server(self):
        server = http_server(self.gw, '127.0.0.1', 0)
        server.routes.update(self.slack.routes())
        self.addCleanup(server.server_close)
        self.assertIn(('POST', '/slack/events'), server.routes)


class TelegramTests(GatewayCase):
    def setUp(self):
        super().setUp()
        self.api = FakeApi({'/getMe': {'ok': True, 'result': {'username': 'gate_bot'}}})
        self.tg = TelegramTransport(self.gw, {'owner_ids': ['1001'], 'wait_seconds': 5}, api=self.api)
        self.tg.username = 'gate_bot'
        self.uid = 0

    def update(self, text, user=1001, chat_type='private', chat=None):
        self.uid += 1
        self.tg.process(
            {
                'update_id': self.uid,
                'message': {
                    'message_id': 50 + self.uid,
                    'text': text,
                    'from': {'id': user},
                    'chat': {'id': chat or user, 'type': chat_type},
                },
            }
        )

    def test_owner_issue_in_dm_and_refusal_in_group(self):
        self.update('/pass@gate_bot issue friend "Fabricated friend agent"')
        text = self.api.sent('sendMessage')[-1][2]['text']
        self.assertIn('New friend pass', text)
        self.update('/pass issue friend x', chat=-500, chat_type='group')
        self.assertIn('direct message', self.api.sent('sendMessage')[-1][2]['text'])

    def test_redeem_deletes_message_then_visitor_chats(self):
        g = self.issue('delegate')
        self.update('/redeem ' + g['token'], user=2002)
        self.assertEqual(self.api.sent('deleteMessage')[0][2]['chat_id'], 2002)
        self.update('what is on the agenda?', user=2002)
        self.assertEqual(self.api.sent('sendMessage')[-1][2]['text'], 'safe reply')

    def test_group_needs_mention(self):
        self.issue('delegate', bind=['telegram:2002'])
        self.update('random chatter', user=2002, chat=-500, chat_type='group')
        self.assertEqual(self.host.calls, [])
        self.update('@gate_bot summarize', user=2002, chat=-500, chat_type='group')
        self.assertEqual(self.host.calls[0].message, 'summarize')

    def test_poll_advances_offset(self):
        self.api.responses['/getUpdates'] = {'ok': True, 'result': [{'update_id': 41}, {'update_id': 42}]}
        self.assertEqual(self.tg.poll_once(timeout=0), 2)
        self.assertEqual(self.tg.offset, 43)
        self.tg.start()
        self.tg.stopped.set()


class MatrixTests(GatewayCase):
    def setUp(self):
        super().setUp()
        self.members = {'!dm:example.org': 2, '!group:example.org': 5}
        self.api = FakeApi({'/rooms/': self.room_api})
        self.mx = MatrixTransport(
            self.gw, {'user_id': '@gate:example.org', 'owner_ids': ['@alex:example.org'], 'wait_seconds': 5}, api=self.api
        )

    def room_api(self, path, body):
        if path.endswith('/joined_members'):
            room = urllib.parse.unquote(path.split('/')[2])
            return {'joined': {f'@u{i}:example.org': {} for i in range(self.members[room])}}
        return {'event_id': '$x'}

    def sync(self, room, sender, text, event_id='$1', initial=False):
        self.mx.handle_sync(
            {
                'next_batch': 's2',
                'rooms': {
                    'join': {
                        room: {
                            'timeline': {
                                'events': [
                                    {
                                        'type': 'm.room.message',
                                        'sender': sender,
                                        'event_id': event_id,
                                        'content': {'msgtype': 'm.text', 'body': text},
                                    }
                                ]
                            }
                        }
                    }
                },
            },
            initial=initial,
        )

    def wait_for(self, kind, n=1):
        deadline = time.time() + 5
        while len(self.api.sent(kind)) < n and time.time() < deadline:
            time.sleep(0.02)
        return self.api.sent(kind)

    def test_owner_issues_in_dm_room(self):
        self.sync('!dm:example.org', '@alex:example.org', '!pass issue colleague "Fabricated team agent"')
        sent = self.wait_for('/send/m.room.message')
        self.assertIn('New colleague pass', sent[0][2]['body'])
        self.assertEqual(self.mx.since, 's2')

    def test_initial_sync_history_is_ignored_and_invites_joined(self):
        self.sync('!dm:example.org', '@alex:example.org', '!pass issue delegate x', initial=True)
        self.mx.handle_sync({'rooms': {'invite': {'!new:example.org': {}}}})
        time.sleep(0.1)
        self.assertEqual(self.api.sent('/send/'), [])
        self.assertEqual(self.api.sent('/join/')[0][1], '/join/%21new%3Aexample.org')

    def test_redeem_redacts_and_group_mentions_route(self):
        g = self.issue('delegate')
        self.sync('!dm:example.org', '@agent:example.net', 'redeem ' + g['token'])
        self.wait_for('/send/')
        self.assertTrue(self.api.sent('/redact/'))
        self.sync('!group:example.org', '@agent:example.net', 'gate: what changed today?', '$2')
        sent = self.wait_for('/send/', 2)
        self.assertEqual(sent[-1][2]['body'], 'safe reply')
        self.assertEqual(self.host.calls[0].message, 'what changed today?')

    def test_own_messages_ignored(self):
        self.sync('!dm:example.org', '@gate:example.org', '!pass list')
        time.sleep(0.1)
        self.assertEqual(self.api.sent('/send/'), [])
