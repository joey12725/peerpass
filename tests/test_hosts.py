"""Host adapters against local fake servers. No network beyond 127.0.0.1."""

import json
import os
import sys
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from peerpass.hosts import HostError, HostRequest, build_host
from peerpass.hosts.webhook import sign, verify_signature

REQ = HostRequest(
    'direct',
    'gateway framing',
    'hello there',
    'peerpass-abc',
    {'id': 'abc', 'label': 'Fabricated agent', 'profile': 'delegate', 'purpose': 'demo'},
    frozenset({'search'}),
)


def fake_server(responder):
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            seen.append((self.path, dict(self.headers.items()), body))
            status, payload = responder(self.path, body)
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            if status in (301, 302):
                self.send_header('Location', 'http://127.0.0.1:1/elsewhere')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen, f'http://127.0.0.1:{server.server_port}'


class OpenAITests(unittest.TestCase):
    def test_chat_completions_contract(self):
        server, seen, url = fake_server(lambda p, b: (200, {'choices': [{'message': {'content': 'hi back'}}]}))
        self.addCleanup(server.shutdown)
        with patch.dict(os.environ, {'FAKE_KEY': 'k-123'}):
            host = build_host(
                {
                    'adapter': 'openai',
                    'base_url': url + '/v1',
                    'model': 'local-model',
                    'api_key_env': 'FAKE_KEY',
                    'session_header': 'X-Session-Id',
                }
            )
        self.assertEqual(host(REQ), 'hi back')
        path, headers, body = seen[0]
        payload = json.loads(body)
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual(headers['Authorization'], 'Bearer k-123')
        self.assertEqual(headers['X-Session-Id'], 'peerpass-abc')
        self.assertEqual([m['role'] for m in payload['messages']], ['system', 'user'])
        self.assertNotIn('response_format', payload)
        host(HostRequest('reasoner', 's', '{}', 'x'))
        self.assertEqual(json.loads(seen[1][2])['response_format'], {'type': 'json_object'})

    def test_errors_redirects_and_bad_shapes_are_opaque(self):
        for responder in (
            lambda p, b: (500, {'error': 'SECRET detail'}),
            lambda p, b: (302, b''),
            lambda p, b: (200, {'choices': []}),
            lambda p, b: (200, b'not json'),
        ):
            server, _, url = fake_server(responder)
            host = build_host({'adapter': 'openai', 'base_url': url, 'model': 'm'})
            with self.assertRaises(HostError) as c:
                host(REQ)
            self.assertNotIn('SECRET', str(c.exception))
            server.shutdown()

    def test_rejects_credentials_in_url(self):
        with self.assertRaises(ValueError):
            build_host({'adapter': 'openai', 'base_url': 'https://user:pw@example.test/v1', 'model': 'm'})


class WebhookTests(unittest.TestCase):
    def test_signed_payload_and_reply(self):
        key = b'fabricated-webhook-secret'
        server, seen, url = fake_server(lambda p, b: (200, {'reply': 'webhook says hi'}))
        self.addCleanup(server.shutdown)
        with patch.dict(os.environ, {'HOOK_SECRET': key.decode()}):
            host = build_host({'adapter': 'webhook', 'url': url + '/hook', 'secret_env': 'HOOK_SECRET'})
        self.assertEqual(host(REQ), 'webhook says hi')
        _, raw_headers, body = seen[0]
        headers = {k.lower(): v for k, v in raw_headers.items()}
        self.assertTrue(verify_signature(key, headers['x-peerpass-timestamp'], body, headers['x-peerpass-signature']))
        self.assertFalse(verify_signature(b'wrong', headers['x-peerpass-timestamp'], body, headers['x-peerpass-signature']))
        payload = json.loads(body)
        self.assertEqual(payload['tools'], ['search'])
        self.assertEqual(payload['grant']['label'], 'Fabricated agent')

    def test_signature_replay_window(self):
        sig = sign(b'k', '1000', b'{}')
        self.assertTrue(verify_signature(b'k', '1000', b'{}', sig, now=1100))
        self.assertFalse(verify_signature(b'k', '1000', b'{}', sig, now=2000))
        self.assertFalse(verify_signature(b'k', 'nope', b'{}', sig))
        self.assertFalse(verify_signature(b'k', '1000', b'{"x":1}', sig, now=1000))

    def test_missing_secret_is_a_config_error(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            build_host({'adapter': 'webhook', 'url': 'http://127.0.0.1:9/x', 'secret_env': 'NOPE'})

    def test_reply_contract(self):
        server, _, url = fake_server(lambda p, b: (200, {'text': 'wrong key'}))
        self.addCleanup(server.shutdown)
        with patch.dict(os.environ, {'S': 'x'}):
            host = build_host({'adapter': 'webhook', 'url': url, 'secret_env': 'S'})
        with self.assertRaises(HostError):
            host(REQ)


FAKE_MCP = textwrap.dedent("""
    import json, sys
    for line in sys.stdin:
        msg = json.loads(line)
        if 'id' not in msg:
            continue
        if msg['method'] == 'initialize':
            result = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fake'}}
        elif msg['method'] == 'tools/call':
            args = msg['params']['arguments']
            if args['message'] == 'fail':
                result = {'isError': True, 'content': [{'type': 'text', 'text': 'boom'}]}
            else:
                text = msg['params']['name'] + ':' + args['message'] + '|' + args.get('instructions', '')[:8]
                result = {'content': [{'type': 'text', 'text': text}]}
        print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}), flush=True)
""")


class MCPTests(unittest.TestCase):
    def test_calls_tool_over_stdio(self):
        host = build_host({'adapter': 'mcp', 'command': [sys.executable, '-c', FAKE_MCP], 'tool': 'ask'})
        self.assertEqual(host(REQ), 'ask:hello there|gateway ')

    def test_tool_error_and_dead_process(self):
        host = build_host({'adapter': 'mcp', 'command': [sys.executable, '-c', FAKE_MCP]})
        with self.assertRaises(HostError):
            host(HostRequest('direct', 's', 'fail', 'x'))
        dead = build_host({'adapter': 'mcp', 'command': [sys.executable, '-c', 'pass']})
        with self.assertRaises(HostError):
            dead(REQ)

    def test_environment_is_not_inherited_wholesale(self):
        with patch.dict(os.environ, {'UNRELATED_SECRET': 'x', 'WANTED': 'y'}):
            host = build_host({'adapter': 'mcp', 'command': ['true'], 'pass_env': ['WANTED']})
        self.assertNotIn('UNRELATED_SECRET', host.env)
        self.assertEqual(host.env['WANTED'], 'y')


class FactoryTests(unittest.TestCase):
    def test_echo_and_unknown(self):
        echo = build_host(None)
        self.assertIn('hello there', echo(REQ))
        reasoned = json.loads(echo(HostRequest('reasoner', 's', json.dumps({'question': 'q?'}), 'x')))
        self.assertEqual(reasoned['decision'], 'answer')
        with self.assertRaises(ValueError):
            build_host({'adapter': 'carrier-pigeon'})

    def test_python_target(self):
        host = build_host({'adapter': 'python', 'target': 'peerpass.hosts.echo:EchoHost', 'name': 'custom'})
        self.assertIn('[custom]', host(REQ))
