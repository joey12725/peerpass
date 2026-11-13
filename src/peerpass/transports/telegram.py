"""Telegram transport over the Bot API (long polling, stdlib only).

Settings (``[transports.telegram]``):
    token_env      env var holding the bot token from @BotFather
    owner_ids      Telegram numeric user ids allowed to run /pass
    wait_seconds   how long to wait for the host agent before replying "still working"
"""

from __future__ import annotations

import logging
import re
import threading
import time

from ..config import secret
from .base import ChatEvent, ChatRouter, JsonApi

log = logging.getLogger('peerpass.telegram')


class TelegramTransport:
    platform = 'telegram'

    def __init__(self, gateway, settings: dict, api=None):
        self.router = ChatRouter(
            gateway,
            self.platform,
            settings.get('owner_ids', []),
            settings.get('wait_seconds', 120),
            settings.get('timezone', 'UTC'),
        )
        self.api = api or JsonApi('https://api.telegram.org/bot' + secret(settings, 'token'))
        self.offset = 0
        self.username = ''
        self.stopped = threading.Event()

    def send(self, chat_id, text: str) -> None:
        for i in range(0, max(len(text), 1), 4000):
            self.api('POST', '/sendMessage', {'chat_id': chat_id, 'text': text[i : i + 4000], 'disable_web_page_preview': True})

    def event(self, update: dict) -> ChatEvent | None:
        msg = update.get('message')
        if not isinstance(msg, dict) or not isinstance(msg.get('text'), str) or 'from' not in msg:
            return None
        chat = msg['chat']
        private = chat.get('type') == 'private'
        text = msg['text']
        mention = f'@{self.username}' if self.username else None
        addressed = private or bool(mention and mention.lower() in text.lower())
        if mention:
            text = re.sub(re.escape(mention), '', text, flags=re.I).strip()

        def delete():
            self.api('POST', '/deleteMessage', {'chat_id': chat['id'], 'message_id': msg['message_id']})

        return ChatEvent(
            self.platform,
            str(update['update_id']),
            str(msg['from']['id']),
            str(chat['id']),
            text,
            private=private,
            addressed=addressed,
            delete=delete,
        )

    def process(self, update: dict) -> None:
        ev = self.event(update)
        if ev is not None:
            self.router.handle(ev, lambda text: self.send(ev.chat_id, text))

    def poll_once(self, timeout: int = 30) -> int:
        data = self.api(
            'GET', f'/getUpdates?timeout={timeout}&offset={self.offset}&allowed_updates=%5B%22message%22%5D', timeout=timeout + 10
        )
        updates = data.get('result', []) if data.get('ok') else []
        for update in updates:
            self.offset = max(self.offset, update['update_id'] + 1)
            threading.Thread(target=self._safe, args=(update,), daemon=True).start()
        return len(updates)

    def _safe(self, update):
        try:
            self.process(update)
        except Exception:  # noqa: BLE001
            log.warning('telegram update failed')

    def start(self) -> threading.Thread:
        me = self.api('GET', '/getMe')
        self.username = me.get('result', {}).get('username', '')

        def loop():
            while not self.stopped.is_set():
                try:
                    self.poll_once()
                except Exception:  # noqa: BLE001
                    log.warning('telegram polling failed; retrying')
                    time.sleep(5)

        t = threading.Thread(target=loop, name='peerpass-telegram', daemon=True)
        t.start()
        return t
