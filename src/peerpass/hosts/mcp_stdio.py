"""Reach a host agent that is exposed as an MCP server over stdio.

The adapter starts the configured command, performs the MCP handshake, calls one tool with
the visitor message (and, optionally, the gateway's system framing), and returns the text
content of the result. A fresh process per request keeps visitors fully isolated.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading

from .base import MAX_REPLY_CHARS, HostError, HostRequest

PROTOCOL = '2025-06-18'


class MCPHost:
    def __init__(
        self,
        command: list[str],
        tool: str = 'ask',
        message_arg: str = 'message',
        system_arg: str | None = 'instructions',
        env: dict | None = None,
        pass_env: list[str] | None = None,
        timeout: float = 300,
    ):
        if not command or not all(isinstance(x, str) for x in command):
            raise ValueError('mcp host needs command = ["program", "arg", ...]')
        self.command, self.tool, self.timeout = list(command), tool, timeout
        self.message_arg, self.system_arg = message_arg, system_arg
        base = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG') if k in os.environ}
        base.update({k: os.environ[k] for k in (pass_env or []) if k in os.environ})
        self.env = {**base, **(env or {})}

    def __call__(self, request: HostRequest) -> str:
        args = {self.message_arg: request.message}
        if self.system_arg:
            args[self.system_arg] = request.system
        proc = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=self.env, text=True
        )
        timer = threading.Timer(self.timeout, proc.kill)
        timer.start()
        try:
            self._send(
                proc,
                {
                    'jsonrpc': '2.0',
                    'id': 1,
                    'method': 'initialize',
                    'params': {
                        'protocolVersion': PROTOCOL,
                        'capabilities': {},
                        'clientInfo': {'name': 'peerpass', 'version': '1'},
                    },
                },
            )
            self._read(proc, 1)
            self._send(proc, {'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            self._send(
                proc, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': self.tool, 'arguments': args}}
            )
            result = self._read(proc, 2)
        finally:
            timer.cancel()
            proc.kill()
            proc.wait()
        if result.get('isError'):
            raise HostError('mcp tool reported an error')
        text = ''.join(
            part.get('text', '') for part in result.get('content', []) if isinstance(part, dict) and part.get('type') == 'text'
        )
        if not text:
            raise HostError('mcp tool returned no text')
        return text[:MAX_REPLY_CHARS]

    @staticmethod
    def _send(proc, msg):
        try:
            proc.stdin.write(json.dumps(msg) + '\n')
            proc.stdin.flush()
        except OSError:
            raise HostError('mcp host exited') from None

    @staticmethod
    def _read(proc, ident):
        for _ in range(1000):
            line = proc.stdout.readline(4 * 1024 * 1024)
            if not line:
                raise HostError('mcp host exited')
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get('id') == ident:
                if 'error' in msg or not isinstance(msg.get('result'), dict):
                    raise HostError('mcp call failed')
                return msg['result']
        raise HostError('mcp host sent too many messages')
