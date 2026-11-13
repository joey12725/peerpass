"""A stand-in host for demos and tests. Replies without calling any model."""

from __future__ import annotations

import json

from .base import HostRequest


class EchoHost:
    def __init__(self, name: str = 'echo-agent'):
        self.name = name

    def __call__(self, request: HostRequest) -> str:
        if request.mode == 'direct':
            return f'[{self.name}] received from {request.grant.get("label", "a visitor")}: {request.message[:500]}'
        envelope = json.loads(request.message)
        if request.mode == 'reviewer':
            # Approve exactly what the reasoner proposed; a real reviewer is a model call.
            return json.dumps(envelope['candidate'])
        answer = f'[{self.name}] would answer: {envelope["question"][:500]}'
        return json.dumps({'decision': 'answer', 'answer': answer, 'review_note': ''})
