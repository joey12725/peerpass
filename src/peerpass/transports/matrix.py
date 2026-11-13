"""Matrix transport over the client-server API (sync loop, stdlib only).

Settings (``[transports.matrix]``):
    homeserver         e.g. https://matrix.example.org
    user_id            the bot account, e.g. @gatekeeper:example.org
    access_token_env   env var holding that account's access token
    owner_ids          Matrix user ids allowed to run /pass
    auto_join          accept room invites (default true)

A room with exactly two members (the bot and one person or agent) counts as private.
The bot reads plain-text events, so use rooms without end-to-end encryption.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import urllib.parse

from ..config import secret
from .base import ChatEvent, ChatRouter, JsonApi

log = logging.getLogger('peerpass.matrix')

FILTER = json.dumps(
    {
        'room': {'timeline': {'limit': 50, 'types': ['m.room.message']}, 'state': {'lazy_load_members': True}},
        'presence': {'types': []},
        'account_data': {'types': []},
    }
)


def q(value: str) -> str:
    return urllib.parse.quote(value, safe='')


class MatrixTransport:
    platform = 'matrix'

    def __init__(self, gateway, settings: dict, api=None):
        self.user_id = settings['user_id']
        self.router = ChatRouter(
            gateway,
            self.platform,
            settings.get('owner_ids', []),
            settings.get('wait_seconds', 120),
            settings.get('timezone', 'UTC'),
        )
        self.auto_join = settings.get('auto_join', True)
        self.api = api or JsonApi(
            settings['homeserver'].rstrip('/') + '/_matrix/client/v3',
            {'Authorization': 'Bearer ' + secret(settings, 'access_token')},
        )
        self.since: str | None = None
        self.members: dict[str, int] = {}
        self.stopped = threading.Event()

    def send(self, room: str, text: str) -> None:
        self.api('PUT', f'/rooms/{q(room)}/send/m.room.message/{secrets.token_hex(8)}', {'msgtype': 'm.text', 'body': text})

    def private(self, room: str) -> bool:
        if room not in self.members:
            data = self.api('GET', f'/rooms/{q(room)}/joined_members')
            self.members[room] = len(data.get('joined', {}))
        return self.members[room] == 2

    def event(self, room: str, ev: dict) -> ChatEvent | None:
        content = ev.get('content') or {}
        if (
            ev.get('type') != 'm.room.message'
            or ev.get('sender') == self.user_id
            or content.get('msgtype') != 'm.text'
            or not isinstance(content.get('body'), str)
        ):
            return None
        text = content['body']
        private = self.private(room)
        local = self.user_id.split(':')[0].lstrip('@')
        addressed = private or self.user_id in text or text.lower().startswith(local.lower() + ':')
        for handle in (self.user_id, local + ':'):
            if text.startswith(handle):
                text = text[len(handle) :].strip()

        def delete():
            self.api(
                'PUT',
                f'/rooms/{q(room)}/redact/{q(ev["event_id"])}/{secrets.token_hex(8)}',
                {'reason': 'access pass removed from history'},
            )

        return ChatEvent(
            self.platform, ev['event_id'], ev['sender'], room, text, private=private, addressed=addressed, delete=delete
        )

    def handle_sync(self, data: dict, initial: bool = False) -> None:
        rooms = data.get('rooms', {})
        for room in rooms.get('invite', {}):
            if self.auto_join:
                self.api('POST', f'/join/{q(room)}', {})
                self.members.pop(room, None)
        for room, info in rooms.get('join', {}).items():
            state = info.get('state', {}).get('events', []) + info.get('timeline', {}).get('events', [])
            if any(e.get('type') == 'm.room.member' for e in state):
                self.members.pop(room, None)
            if initial:
                continue  # never act on history from before startup
            for ev in info.get('timeline', {}).get('events', []):
                chat = self.event(room, ev)
                if chat is not None:
                    threading.Thread(target=self._safe, args=(chat,), daemon=True).start()
        self.since = data.get('next_batch', self.since)

    def _safe(self, chat: ChatEvent) -> None:
        try:
            self.router.handle(chat, lambda text: self.send(chat.chat_id, text))
        except Exception:  # noqa: BLE001
            log.warning('matrix event failed')

    def poll_once(self, timeout_ms: int = 30000) -> None:
        params = {'timeout': str(timeout_ms if self.since else 0), 'filter': FILTER}
        if self.since:
            params['since'] = self.since
        initial = self.since is None
        data = self.api('GET', '/sync?' + urllib.parse.urlencode(params), timeout=timeout_ms / 1000 + 10)
        self.handle_sync(data, initial=initial)

    def start(self) -> threading.Thread:
        def loop():
            while not self.stopped.is_set():
                try:
                    self.poll_once()
                except Exception:  # noqa: BLE001
                    log.warning('matrix sync failed; retrying')
                    time.sleep(5)

        t = threading.Thread(target=loop, name='peerpass-matrix', daemon=True)
        t.start()
        return t
