"""Generic signed webhook: POST the request to your agent, read ``{"reply": "..."}`` back.

Signature: ``X-PeerPass-Signature: v1=<hex hmac-sha256(secret, "<timestamp>.<raw body>")>`` and
``X-PeerPass-Timestamp: <unix seconds>``. Receivers should call :func:`verify_signature`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse

from ..config import secret
from .base import MAX_REPLY_CHARS, HostError, HostRequest, post_bytes


def sign(key: bytes, ts: str, body: bytes) -> str:
    return 'v1=' + hmac.new(key, ts.encode() + b'.' + body, hashlib.sha256).hexdigest()


def verify_signature(key: bytes, ts: str, body: bytes, signature: str, now: float | None = None, tolerance: int = 300) -> bool:
    try:
        skew = abs((now if now is not None else time.time()) - int(ts))
    except (TypeError, ValueError):
        return False
    return skew <= tolerance and hmac.compare_digest(sign(key, ts, body), signature or '')


class WebhookHost:
    def __init__(self, url: str, secret_env: str, timeout: float = 300, headers: dict | None = None):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
            raise ValueError('webhook url must be http(s) without embedded credentials')
        self.url, self.timeout = url, timeout
        self.key = secret({'secret_env': secret_env}, 'secret').encode()
        self.headers = dict(headers or {})

    def payload(self, request: HostRequest) -> dict:
        return {
            'version': 1,
            'mode': request.mode,
            'system': request.system,
            'message': request.message,
            'session_id': request.session_id,
            'grant': request.grant,
            'tools': sorted(request.tools),
        }

    def __call__(self, request: HostRequest) -> str:
        body = json.dumps(self.payload(request))
        ts = str(int(time.time()))
        headers = {**self.headers, 'X-PeerPass-Timestamp': ts, 'X-PeerPass-Signature': sign(self.key, ts, body.encode())}
        data = post_bytes(self.url, body.encode(), headers, self.timeout)
        reply = data.get('reply') if isinstance(data, dict) else None
        if not isinstance(reply, str):
            raise HostError('webhook reply must be {"reply": "..."}')
        return reply[:MAX_REPLY_CHARS]


__all__ = ['WebhookHost', 'sign', 'verify_signature']
