"""Any OpenAI-compatible chat completions endpoint: OpenAI, OpenRouter, vLLM, Ollama, LM Studio,
or an agent framework that exposes one (many do)."""

from __future__ import annotations

import urllib.parse

from ..config import secret
from .base import MAX_REPLY_CHARS, HostError, HostRequest, post_json


class OpenAICompatibleHost:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key_env: str | None = None,
        timeout: float = 300,
        json_mode: bool = True,
        session_header: str | None = None,
        extra_body: dict | None = None,
    ):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
            raise ValueError('base_url must be http(s) without embedded credentials')
        self.url = base_url.rstrip('/') + '/chat/completions'
        self.model, self.timeout, self.json_mode = model, timeout, json_mode
        self.session_header, self.extra_body = session_header, dict(extra_body or {})
        self.api_key = secret({'api_key_env': api_key_env}, 'api_key', required=False) if api_key_env else ''

    def __call__(self, request: HostRequest) -> str:
        payload = {
            **self.extra_body,
            'model': self.model,
            'stream': False,
            'messages': [{'role': 'system', 'content': request.system}, {'role': 'user', 'content': request.message}],
        }
        if request.mode != 'direct' and self.json_mode:
            payload['response_format'] = {'type': 'json_object'}
        headers = {}
        if self.api_key:
            headers['Authorization'] = 'Bearer ' + self.api_key
        if self.session_header:
            headers[self.session_header] = request.session_id
        data = post_json(self.url, payload, headers, self.timeout)
        try:
            content = data['choices'][0]['message']['content']
        except (KeyError, IndexError, TypeError):
            raise HostError('unexpected chat completions response') from None
        if not isinstance(content, str):
            raise HostError('unexpected chat completions response')
        return content[:MAX_REPLY_CHARS]
