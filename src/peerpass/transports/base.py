"""Platform-neutral chat routing shared by every chat transport.

Each transport (Discord, Slack, Telegram, Matrix) only translates platform events into a
:class:`ChatEvent` and supplies a ``reply`` function. Everything that matters for security
happens here, once:

* Owner commands (``/pass issue|list|revoke|reviews|release|audit``) are accepted only from
  configured owner ids, and a new pass is only ever delivered in a private context
  (a DM or an ephemeral slash-command reply). If delivery fails, that exact pass is revoked.
* Visitors talk to the host agent from a chat identity bound to a pass, so no token appears
  in later messages. ``redeem <pass>`` in a DM creates the binding.
* A pass pasted into a shared channel is treated as burned and revoked on the spot.
* Platform retries are deduplicated by event id, so one interaction never mints two passes.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import shlex
import threading
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from ..config import duration
from ..store import Denied, Subject

if TYPE_CHECKING:
    from ..gateway import Gateway

log = logging.getLogger('peerpass.chat')

Reply = Callable[[str], None]

REFUSAL = 'Only the owner can manage passes here.'
PRIVATE_ONLY = 'Use a direct message or the slash command so the pass stays private to you.'
HELP = """Owner commands:
  issue <profile> <label> [ttl=12h] [calls=40] [purpose="..."] [bind=platform:id]
  list                      active passes
  revoke <grant-id>         id prefix is fine
  reviews                   consultations held for you
  release <job-id> answer <text>  |  release <job-id> decline
  audit [grant-id]"""


@dataclass
class ChatEvent:
    platform: str
    event_id: str  # unique per message or interaction, used for dedupe and as the request id
    user_id: str
    chat_id: str
    text: str
    private: bool  # DM, or an ephemeral slash-command context
    addressed: bool = True  # DM, mention, or slash command. Unaddressed group chatter is ignored
    command: bool = False  # arrived through a native /pass command; text holds only its arguments
    delete: Callable[[], None] | None = None  # removes the triggering message, where the platform allows


class Dedupe:
    def __init__(self, size: int = 2048):
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.size, self.lock = size, threading.Lock()

    def first(self, key: str) -> bool:
        with self.lock:
            if key in self.seen:
                return False
            self.seen[key] = None
            if len(self.seen) > self.size:
                self.seen.popitem(last=False)
            return True


def parse_options(words: list[str]) -> tuple[list[str], dict[str, str]]:
    positional, options = [], {}
    for w in words:
        k, sep, v = w.partition('=')
        if sep and re.fullmatch(r'[a-z_]+', k):
            options[k] = v
        else:
            positional.append(w)
    return positional, options


class ChatRouter:
    def __init__(
        self, gateway: Gateway, platform: str, owners: list[str] | set[str], wait_seconds: float = 120, timezone: str = 'UTC'
    ):
        self.gw, self.store, self.platform = gateway, gateway.store, platform
        self.owners = {str(o) for o in owners}
        self.wait_seconds, self.tz = wait_seconds, ZoneInfo(timezone)
        self.dedupe = Dedupe()
        prefix = re.escape(self.store.config.token_prefix)
        self.token_re = re.compile(prefix + r'[A-Za-z0-9_-]{32,64}')

    # ----- entry point ----------------------------------------------------------------------

    def handle(self, event: ChatEvent, reply: Reply) -> None:
        if not self.dedupe.first(f'{event.platform}:{event.event_id}'):
            return
        text = event.text.strip()
        if event.command:
            return self.owner_command(event, text, reply)
        m = re.match(r'^[/!]?pass(?:@\S+)?(?:\s+|$)(.*)', text, re.S)
        if m:
            return self.owner_command(event, m.group(1), reply)
        token = self.token_re.search(text)
        if token and not event.private:
            return self.burn(event, token.group(0), reply)
        m = re.match(r'^[/!]?redeem(?:@\S+)?\s+(\S+)\s*$', text)
        if m:
            return self.redeem(event, m.group(1), reply)
        if not event.addressed:
            return None
        m = re.match(r'^[/!]?status(?:@\S+)?\s+([a-f0-9]{32})\s*$', text)
        if m:
            return self.status(event, m.group(1), reply)
        return self.visit(event, text, reply)

    # ----- owner side -----------------------------------------------------------------------

    def owner_command(self, event: ChatEvent, args: str, reply: Reply) -> None:
        if event.user_id not in self.owners:
            reply(REFUSAL)  # never touches the store
            return
        try:
            words = shlex.split(args)
        except ValueError:
            reply('Could not parse that command. Check your quotes.')
            return
        sub, rest = (words[0].lower(), words[1:]) if words else ('help', [])
        try:
            if sub == 'issue':
                if not event.private:
                    reply(PRIVATE_ONLY)
                    return
                return self.issue(rest, reply)
            if sub == 'list':
                rows = self.store.owner('grants')
                reply('\n'.join(self.describe(r) for r in rows) or 'No active passes.')
            elif sub == 'revoke' and len(rest) == 1:
                r = self.store.owner('revoke', {'grant_id': rest[0]})
                reply(f'Revoked {r["revoked"][:8]} ({r["label"]}). {r["note"]}')
            elif sub == 'reviews':
                rows = self.store.owner('reviews')
                if not event.private and rows:
                    reply(PRIVATE_ONLY)
                    return
                reply(
                    '\n\n'.join(
                        f'job {r["job_id"]} from {r["label"]} ({r["profile"]})\n'
                        f'Q: {r["question"][:600]}\nDraft: {r["draft"][:600] or "-"}\nNote: {r["note"][:300]}'
                        for r in rows
                    )
                    or 'Nothing is waiting for review.'
                )
            elif sub == 'release' and len(rest) >= 2 and rest[1] in ('answer', 'decline'):
                body = {'job_id': rest[0], 'decision': rest[1]}
                if rest[1] == 'answer':
                    body['answer'] = ' '.join(rest[2:])
                r = self.store.owner('release', body)
                reply(f'Released job {r["job_id"][:8]} as {r["decision"]}.')
            elif sub == 'audit' and len(rest) <= 1:
                rows = self.store.owner('audit', {'grant_id': rest[0], 'limit': 20} if rest else {'limit': 20})
                reply('\n'.join(f'{self.when(r["at"])}  {r["grant_id"][:8]}  {r["action"]}' for r in rows) or 'No audit entries.')
            else:
                reply(HELP)
        except Denied as e:
            reply(e.message)

    def issue(self, rest: list[str], reply: Reply) -> None:
        positional, opt = parse_options(rest)
        if len(positional) < 2:
            reply('Usage: issue <profile> <label> [ttl=12h] [calls=40] [purpose="..."] [bind=platform:id]')
            return
        body: dict[str, Any] = {'profile': positional[0], 'label': ' '.join(positional[1:])}
        try:
            if 'ttl' in opt:
                body['ttl'] = duration(opt['ttl'])
            if 'calls' in opt:
                body['max_calls'] = int(opt['calls'])
        except ValueError:
            reply('ttl looks like 90m, 12h or 30d; calls is a whole number.')
            return
        if 'purpose' in opt:
            body['purpose'] = opt['purpose']
        if 'policy' in opt:
            body['disclosure_policy'] = opt['policy']
        if 'bind' in opt:
            body['bind'] = [b for b in opt['bind'].split(',') if b]
        grant = self.store.owner('issue', body)
        try:
            reply(self.handoff(grant))
        except Exception:  # noqa: BLE001 - an undelivered pass must not stay live
            log.warning('pass delivery failed; revoking the undelivered pass')
            self.store.owner('revoke', {'grant_id': grant['grant_id']})
            raise

    def handoff(self, g: dict) -> str:
        cfg = self.store.config
        url = cfg.public_url.rstrip('/')
        p = self.store.profiles[g['profile']]
        lines = [
            f'New {g["profile"]} pass for "{g["label"]}"',
            f'Grant {g["grant_id"][:8]} · expires {self.when(g["expires_at"])} · {g["max_calls"]} requests',
            f'Scopes: {", ".join(g["scopes"])}',
            '',
            'Pass (shown once, stored only as a hash):',
            g['token'],
            '',
            'Give the visiting agent:',
            f'  PEERPASS_URL={url}',
            '  PEERPASS_TOKEN=<the pass, set as a secret, never in a command line>',
            f'  client: {url}/client.py  (or: pipx install git+https://github.com/joey12725/peerpass)',
            '  peerpass-client me | ask "..." --wait 120 | mcp',
            f'  A2A agent card: {url}/.well-known/agent-card.json',
        ]
        if g.get('bindings'):
            lines.append(f'Already bound to: {", ".join(g["bindings"])}')
        elif p.can_message:
            lines.append('Chat agents can DM me "redeem <pass>" once instead.')
        if p.mode == 'direct':
            lines.append('This is a direct pass: broad access to the host agent and its tools. Hand it only to agents you run.')
        return '\n'.join(lines)

    def describe(self, r: dict) -> str:
        bound = f' · bound {", ".join(r["bindings"])}' if r.get('bindings') else ''
        return (
            f'{r["id"][:8]}  {r["profile"]:<10} {r["label"][:40]}  '
            f'{r["calls"]}/{r["max_calls"]} used · expires {self.when(r["expires"])}{bound}'
        )

    def when(self, ts: float) -> str:
        return dt.datetime.fromtimestamp(ts, self.tz).strftime('%Y-%m-%d %H:%M %Z')

    # ----- visitor side ---------------------------------------------------------------------

    def subject(self, event: ChatEvent) -> Subject:
        return Subject(event.platform, event.user_id)

    @staticmethod
    def remove(event: ChatEvent) -> None:
        if event.delete:
            try:
                event.delete()
            except Exception:  # noqa: BLE001 - deleting is best effort and platform-dependent
                pass

    def burn(self, event: ChatEvent, token: str, reply: Reply) -> None:
        try:
            g = self.store.authenticate(token)
        except Denied:
            return
        self.store.owner('revoke', {'grant_id': g['id']})
        self.remove(event)
        reply(
            'That looked like a live access pass in a shared channel, so it has been revoked. '
            'Ask the owner for a new one and redeem it in a direct message.'
        )

    def redeem(self, event: ChatEvent, token: str, reply: Reply) -> None:
        if not event.private:
            return self.burn(event, token, reply)
        self.remove(event)
        try:
            r = self.store.redeem(token, self.subject(event))
        except Denied as e:
            reply('That pass is not valid or cannot be used here.' if e.status in (401, 403) else e.message)
            return
        reply(
            f'Linked this account to the "{r["label"]}" pass until {self.when(r["expires_at"])}. '
            'Send messages here and I will route them through the gateway.'
        )

    def status(self, event: ChatEvent, jid: str, reply: Reply) -> None:
        try:
            r = self.store.job(self.subject(event), jid)
        except Denied as e:
            reply(e.message)
            return
        reply(self.render(r))

    def visit(self, event: ChatEvent, text: str, reply: Reply) -> None:
        cred = self.subject(event)
        try:
            self.store.authenticate(cred)
        except Denied:
            if event.private:
                reply('No active pass is linked to this account. Ask the owner for one, then send "redeem <pass>" here.')
            return
        try:
            job = self.gw.submit(cred, {'message': text, 'request_id': f'{event.platform}:{event.event_id}'[:100]})
            r = self.gw.wait(cred, job['job_id'], self.wait_seconds)
        except Denied as e:
            reply(e.message)
            return
        reply(self.render(r))

    @staticmethod
    def render(r: dict) -> str:
        if r['status'] in ('queued', 'running'):
            return f'Still working on it. Send "status {r["job_id"]}" to check later.'
        if r.get('result'):
            return r['result']
        return f'Request {r["status"]}.'


class JsonApi:
    """Tiny JSON-over-HTTPS client used by the Slack, Telegram and Matrix transports."""

    def __init__(self, base: str, headers: dict[str, str] | None = None, timeout: float = 40):
        self.base, self.headers, self.timeout = base.rstrip('/'), dict(headers or {}), timeout

    def __call__(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={'Content-Type': 'application/json; charset=utf-8', **self.headers},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read(8 * 1024 * 1024) or b'{}')
        except urllib.error.HTTPError as e:
            raise RuntimeError(f'{method} {path.split("?")[0]} failed with HTTP {e.code}') from None
