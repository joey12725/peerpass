"""Slack transport: Events API and a /pass slash command over plain HTTPS (stdlib only).

Settings (``[transports.slack]``):
    bot_token_env        env var holding the bot token (xoxb-...)
    signing_secret_env   env var holding the app's signing secret
    owner_ids            Slack user ids (U...) allowed to run /pass
    wait_seconds         how long to wait for the host agent before replying "still working"

Point the app's Event Subscriptions at ``<public_url>/slack/events`` (subscribe to
``message.im`` and ``app_mention``) and its /pass slash command at ``<public_url>/slack/commands``.
Every request is checked against the signing secret before anything else happens.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
import time
import urllib.parse

from ..config import secret
from .base import ChatEvent, ChatRouter, JsonApi

log = logging.getLogger('peerpass.slack')


def verify(signing_secret: bytes, headers: dict, body: bytes, now: float | None = None) -> bool:
    h = {k.lower(): v for k, v in headers.items()}
    ts, sig = h.get('x-slack-request-timestamp', ''), h.get('x-slack-signature', '')
    try:
        if abs((now or time.time()) - int(ts)) > 300:
            return False
    except ValueError:
        return False
    base = b'v0:' + ts.encode() + b':' + body
    expected = 'v0=' + hmac.new(signing_secret, base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _json(status: int, value) -> tuple[int, str, bytes]:
    return status, 'application/json', json.dumps(value).encode()


class SlackTransport:
    platform = 'slack'

    def __init__(self, gateway, settings: dict, api=None):
        self.router = ChatRouter(
            gateway,
            self.platform,
            settings.get('owner_ids', []),
            settings.get('wait_seconds', 120),
            settings.get('timezone', 'UTC'),
        )
        self.signing_secret = secret(settings, 'signing_secret').encode()
        self.api = api or JsonApi('https://slack.com/api', {'Authorization': 'Bearer ' + secret(settings, 'bot_token')})
        self.bot_user = settings.get('bot_user_id', '')
        self.background = True

    def routes(self) -> dict:
        return {('POST', '/slack/events'): self.on_events, ('POST', '/slack/commands'): self.on_command}

    def send(self, channel: str, text: str, thread_ts: str | None = None) -> None:
        body = {'channel': channel, 'text': text[:3900], 'unfurl_links': False}
        if thread_ts:
            body['thread_ts'] = thread_ts
        r = self.api('POST', '/chat.postMessage', body)
        if not r.get('ok'):
            raise RuntimeError('slack chat.postMessage failed')

    # ----- slash command: runs synchronously, the reply is the ephemeral HTTP response ------

    def on_command(self, headers: dict, body: bytes):
        if not verify(self.signing_secret, headers, body):
            return _json(401, {'error': 'bad signature'})
        form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode(), keep_blank_values=True).items()}
        replies: list[str] = []
        ev = ChatEvent(
            self.platform,
            'cmd:' + form.get('trigger_id', ''),
            form.get('user_id', ''),
            form.get('channel_id', ''),
            form.get('text', ''),
            private=True,
            command=True,
        )
        self.router.handle(ev, replies.append)
        return _json(200, {'response_type': 'ephemeral', 'text': '\n\n'.join(replies)[:3900] or 'Done.'})

    # ----- events: acknowledged at once, handled in the background --------------------------

    def event(self, payload: dict) -> ChatEvent | None:
        e = payload.get('event') or {}
        if e.get('type') not in ('message', 'app_mention') or e.get('subtype') not in (None, 'bot_message'):
            return None
        user = e.get('user') or e.get('bot_id') or ''
        if not user or user == self.bot_user or not isinstance(e.get('text'), str):
            return None
        private = e.get('channel_type') == 'im'
        if e['type'] == 'message' and not private:
            return None  # channel chatter only counts when it mentions the bot (app_mention)
        text = re.sub(r'<@[A-Z0-9]+>', '', e['text']).strip()
        return ChatEvent(
            self.platform,
            payload.get('event_id') or e.get('ts', ''),
            user,
            e.get('channel', ''),
            text,
            private=private,
            addressed=True,
        )

    def on_events(self, headers: dict, body: bytes):
        if not verify(self.signing_secret, headers, body):
            return _json(401, {'error': 'bad signature'})
        payload = json.loads(body)
        if payload.get('type') == 'url_verification':
            return _json(200, {'challenge': payload.get('challenge', '')})
        if payload.get('type') == 'event_callback':
            ev = self.event(payload)
            if ev is not None:
                thread = (payload.get('event') or {}).get('thread_ts')
                work = lambda: self._safe(ev, thread)  # noqa: E731
                if self.background:
                    threading.Thread(target=work, daemon=True).start()
                else:
                    work()
        return _json(200, {'ok': True})

    def _safe(self, ev: ChatEvent, thread_ts):
        try:
            self.router.handle(ev, lambda text: self.send(ev.chat_id, text, thread_ts))
        except Exception:  # noqa: BLE001
            log.warning('slack event failed')

    def start(self):
        if not self.bot_user:
            try:
                self.bot_user = self.api('POST', '/auth.test', {}).get('user_id', '')
            except Exception:  # noqa: BLE001
                log.warning("slack auth.test failed; set bot_user_id to ignore the bot's own messages")
        return None
