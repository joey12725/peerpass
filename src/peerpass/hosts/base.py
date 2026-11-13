"""The host adapter contract.

A host adapter is any callable that takes a :class:`HostRequest` and returns the host agent's
reply as a string. The gateway decides everything security-relevant (system framing, allowed
tools, session id) before the adapter runs; the visitor controls only ``message``.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

MAX_REPLY_BYTES = 2 * 1024 * 1024
MAX_REPLY_CHARS = 64000


class HostError(RuntimeError):
    """Raised by adapters. Its text is never shown to visitors."""


@dataclass(frozen=True)
class HostRequest:
    mode: str  # 'direct', 'reasoner' or 'reviewer'
    system: str  # gateway-written instructions
    message: str  # visitor text (direct) or a JSON envelope (reasoner/reviewer)
    session_id: str  # server-chosen, stable per pass
    grant: dict[str, Any] = field(default_factory=dict)  # id, label, profile, purpose
    tools: frozenset = frozenset()  # tool allowlist for this call; empty means no tools


class HostAdapter(Protocol):
    def __call__(self, request: HostRequest) -> str: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def post_bytes(url: str, body: bytes, headers: dict[str, str], timeout: float) -> Any:
    """POST raw bytes, refuse redirects, bound the reply size, and parse JSON."""
    req = urllib.request.Request(url, data=body, method='POST', headers={'Content-Type': 'application/json', **headers})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout) as r:
            raw = r.read(MAX_REPLY_BYTES + 1)
    except Exception as e:  # noqa: BLE001 - adapters surface one opaque error type
        raise HostError(f'host request failed: {type(e).__name__}') from None
    if len(raw) > MAX_REPLY_BYTES:
        raise HostError('host reply too large')
    try:
        return json.loads(raw)
    except ValueError:
        raise HostError('host reply was not JSON') from None


def post_json(url: str, payload: dict, headers: dict[str, str], timeout: float) -> Any:
    return post_bytes(url, json.dumps(payload).encode(), headers, timeout)


def build_host(settings: dict | None) -> HostAdapter:
    settings = dict(settings or {'adapter': 'echo'})
    kind = settings.pop('adapter', 'echo')
    if kind == 'echo':
        from .echo import EchoHost

        return EchoHost(**settings)
    if kind in ('openai', 'openai-compatible'):
        from .openai_compat import OpenAICompatibleHost

        return OpenAICompatibleHost(**settings)
    if kind == 'webhook':
        from .webhook import WebhookHost

        return WebhookHost(**settings)
    if kind == 'mcp':
        from .mcp_stdio import MCPHost

        return MCPHost(**settings)
    if kind == 'python':
        # adapter = "python", target = "my_module:make_host" -> make_host(**rest)
        import importlib

        module, _, attr = settings.pop('target').partition(':')
        return getattr(importlib.import_module(module), attr)(**settings)
    raise ValueError(f'unknown host adapter: {kind}')
