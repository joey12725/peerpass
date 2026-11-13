"""The single-file visitor client: URL rules, tool validation, MCP stdio, and a live round trip."""

import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

from helpers import GatewayCase
from peerpass import client
from peerpass.server import http_server

CLIENT = Path(client.__file__)


class ClientTests(GatewayCase):
    def test_url_rules(self):
        for url in ('http://evil.test', 'https://user:pw@gw.test', 'https://gw.test/?x=1', 'ftp://gw.test'):
            with patch.dict(os.environ, {'PEERPASS_URL': url, 'PEERPASS_TOKEN': 't'}), self.assertRaises(RuntimeError):
                client.request('/v1/me')
        for url in ('https://gw.example.test', 'http://127.0.0.1:8750', 'http://localhost:1'):
            with patch.dict(os.environ, {'PEERPASS_URL': url}):
                self.assertEqual(client.base_url(), url)
        with patch.dict(os.environ, {'PEERPASS_URL': 'https://gw.test'}, clear=True), self.assertRaises(RuntimeError):
            client.request('/v1/me')

    def test_tool_argument_validation(self):
        for name, args in [
            ('peer_job', {'job_id': '../owner'}),
            ('peer_job', {}),
            ('peer_ask', {'message': 1}),
            ('peer_access', {'extra': 'x'}),
            ('peer_proposal', {'proposal_id': 'A' * 32}),
            ('nope', {}),
        ]:
            with self.assertRaises(RuntimeError):
                client.call_tool(name, args)

    def test_tools_follow_scopes(self):
        names = {t['name'] for t in client.visible_tools(['agent.consult', 'jobs.read'])}
        self.assertEqual(names, {'peer_access', 'peer_ask', 'peer_job'})
        self.assertEqual(len(client.visible_tools(list(self.store.profiles['colleague'].scopes))), 7)

    def test_mcp_initialize_ping_and_errors(self):
        inp = (
            '\n'.join(
                json.dumps(m)
                for m in [
                    {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
                    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'},
                    {
                        'jsonrpc': '2.0',
                        'id': 3,
                        'method': 'tools/call',
                        'params': {'name': 'peer_ask', 'arguments': {'message': 'x'}},
                    },
                    {'jsonrpc': '2.0', 'id': 4, 'method': 'resources/list'},
                ]
            )
            + '\n'
        )
        p = subprocess.run(
            [sys.executable, str(CLIENT), 'mcp'],
            input=inp,
            text=True,
            capture_output=True,
            check=True,
            env={'PATH': os.environ['PATH']},
        )
        rows = [json.loads(x) for x in p.stdout.splitlines()]
        self.assertEqual([r['id'] for r in rows], [1, 2, 3, 4])
        self.assertEqual(rows[0]['result']['protocolVersion'], '2025-06-18')
        self.assertTrue(rows[2]['result']['isError'])
        self.assertEqual(rows[3]['error']['code'], -32601)

    def test_round_trip_against_a_real_gateway(self):
        server = http_server(self.gw, '127.0.0.1', 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        g = self.issue('colleague')
        self.host.reply = json.dumps({'decision': 'answer', 'answer': 'Thursday works.', 'review_note': ''})
        env = {'PEERPASS_URL': f'http://127.0.0.1:{server.server_port}', 'PEERPASS_TOKEN': g['token']}
        with patch.dict(os.environ, env):
            self.assertEqual(client.request('/v1/me')['profile'], 'colleague')
            out = io.StringIO()
            with patch('sys.stdout', out), patch('sys.stderr', io.StringIO()):
                client.main(['ask', 'When can we meet?', '--wait', '10'])
            self.assertEqual(json.loads(out.getvalue())['result'], 'Thursday works.')
            lines = io.BytesIO(
                b'\n'.join(
                    json.dumps(m).encode()
                    for m in [
                        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'peer_access', 'arguments': {}}},
                    ]
                )
                + b'\n'
            )
            sink = io.StringIO()
            client.mcp(lines, sink)
            rows = [json.loads(x) for x in sink.getvalue().splitlines()]
            self.assertEqual(len(rows[0]['result']['tools']), 7)
            self.assertIn('colleague', rows[1]['result']['content'][0]['text'])
        with patch.dict(os.environ, env | {'PEERPASS_TOKEN': 'pp_' + 'x' * 43}), self.assertRaises(RuntimeError) as c:
            client.request('/v1/me')
        self.assertIn('401', str(c.exception))
