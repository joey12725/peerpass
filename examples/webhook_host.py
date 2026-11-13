#!/usr/bin/env python3
"""A minimal host agent behind PeerPass's webhook adapter.

Run it, then point the gateway at it:

    export PEERPASS_WEBHOOK_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    python3 examples/webhook_host.py            # listens on 127.0.0.1:9000

    # peerpass.toml
    [host]
    adapter = "webhook"
    url = "http://127.0.0.1:9000/peerpass"
    secret_env = "PEERPASS_WEBHOOK_SECRET"

Replace ``answer()`` with a call into your own agent framework. The gateway has already
authenticated the pass, applied its budget and rate limit, and written the system framing;
``tools`` is the allowlist your agent should enforce for this call.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

from peerpass.hosts.webhook import verify_signature

SECRET = os.environ['PEERPASS_WEBHOOK_SECRET'].encode()


def answer(payload: dict) -> str:
    if payload['mode'] == 'direct':
        return f'Hello {payload["grant"]["label"]}, you said: {payload["message"]}'
    if payload['mode'] == 'reviewer':
        # A real reviewer is a separate, tool-free model call. This demo approves verbatim.
        return json.dumps(json.loads(payload['message'])['candidate'])
    question = json.loads(payload['message'])['question']
    return json.dumps({'decision': 'answer', 'answer': f'Happy to help with: {question}', 'review_note': ''})


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        if not verify_signature(
            SECRET, self.headers.get('X-PeerPass-Timestamp', ''), body, self.headers.get('X-PeerPass-Signature', '')
        ):
            self.send_response(401)
            self.end_headers()
            return
        data = json.dumps({'reply': answer(json.loads(body))}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == '__main__':
    HTTPServer(('127.0.0.1', 9000), Handler).serve_forever()
