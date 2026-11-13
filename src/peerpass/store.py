"""Pass store: issuance, authentication, budgets, jobs, review queue and audit, all in SQLite.

Only a SHA-256 digest of each pass is stored. Every visitor operation re-checks expiry and
revocation inside the same transaction that does the work.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .review import DECLINE_MESSAGE, credential_shaped

ACTIVE = ('queued', 'running')


class Denied(Exception):
    """A request the gateway refuses. ``status`` maps directly to an HTTP status code."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass(frozen=True)
class Subject:
    """A chat-platform identity bound to a pass, e.g. ``Subject('telegram', '1234')``."""

    platform: str
    id: str

    def key(self) -> str:
        return f'{self.platform}:{self.id}'


Credential = str | Subject


def bounded(value, name: str, maximum: int = 8000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise Denied(400, 'Invalid ' + name)
    return value


def exact(body, allowed, required=()) -> None:
    if not isinstance(body, dict) or set(body) - set(allowed) or set(required) - set(body):
        raise Denied(400, 'Invalid request fields')


def timestamp(value) -> float:
    try:
        t = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if t.tzinfo is None:
            raise ValueError()
        return t.timestamp()
    except (ValueError, TypeError, AttributeError):
        raise Denied(400, 'Expected an ISO timestamp with timezone') from None


def slots_checked(slots, now: float) -> list[dict]:
    if not isinstance(slots, list) or len(slots) > 100:
        raise Denied(400, 'Expected at most 100 time slots')
    out = []
    for slot in slots:
        exact(slot, ['start', 'end'], ['start', 'end'])
        a, b = timestamp(slot['start']), timestamp(slot['end'])
        if a < now or b <= a or b - a > 86400 or b > now + 90 * 86400:
            raise Denied(400, 'Invalid time window')
        out.append({'start': slot['start'], 'end': slot['end']})
    return out


def subject_of(text: str) -> Subject:
    m = re.fullmatch(r'([a-z][a-z0-9_-]{0,31}):([A-Za-z0-9@._:!+=-]{1,200})', text or '')
    if not m:
        raise Denied(400, 'Bindings look like platform:id, for example telegram:123456')
    return Subject(m.group(1), m.group(2))


SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
  id TEXT PRIMARY KEY, digest TEXT UNIQUE, label TEXT, purpose TEXT,
  profile TEXT, expires REAL, revoked INTEGER DEFAULT 0,
  calls INTEGER DEFAULT 0, max_calls INTEGER, created REAL,
  disclosure_policy TEXT DEFAULT '', context TEXT DEFAULT '',
  slots TEXT DEFAULT '[]', shared_until REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS bindings (
  subject TEXT PRIMARY KEY, grant_id TEXT, created REAL);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, grant_id TEXT, request_key TEXT, status TEXT,
  message TEXT, result TEXT, created REAL, UNIQUE(grant_id, request_key));
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY, grant_id TEXT, request_key TEXT, body TEXT,
  status TEXT, created REAL, UNIQUE(grant_id, request_key));
CREATE TABLE IF NOT EXISTS history (
  job_id TEXT PRIMARY KEY, grant_id TEXT, question TEXT, answer TEXT, created REAL);
CREATE TABLE IF NOT EXISTS reviews (
  job_id TEXT PRIMARY KEY, grant_id TEXT, question TEXT, draft TEXT, note TEXT, created REAL);
CREATE TABLE IF NOT EXISTS audit (
  at REAL, grant_id TEXT, action TEXT, object_id TEXT);
CREATE INDEX IF NOT EXISTS audit_grant ON audit(grant_id, action, at);
"""


class Store:
    def __init__(self, path, config: Config | None = None, clock: Callable[[], float] = time.time, recover: bool = True):
        self.path, self.clock = str(path), clock
        self.config = config or Config()
        self.profiles = self.config.profiles
        self.limits = self.config.limits
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.db() as c:
            c.executescript(SCHEMA)
            if recover:
                # The serving process starts here: never replay work an earlier process left running.
                c.execute("UPDATE jobs SET status='interrupted', message=NULL, result=NULL WHERE status IN ('queued','running')")
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA secure_delete=ON')
        try:
            with c:
                yield c
        finally:
            c.close()

    def audit(self, c, grant, action, obj='') -> None:
        c.execute('INSERT INTO audit VALUES (?,?,?,?)', (self.clock(), grant, action, obj))

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def profile(self, name: str):
        try:
            return self.profiles[name]
        except KeyError:
            raise Denied(403, 'Pass profile is no longer configured') from None

    # ----- issuance -------------------------------------------------------------------------

    def issue(self, body: dict) -> dict:
        exact(body, ['profile', 'label', 'purpose', 'ttl', 'max_calls', 'disclosure_policy', 'bind'], ['profile', 'label'])
        name = body['profile']
        if name not in self.profiles:
            raise Denied(400, 'Unknown profile')
        p = self.profiles[name]
        ttl = body.get('ttl', p.ttl_default)
        cap = body.get('max_calls', p.max_calls_default)
        if type(ttl) is not int or not 60 <= ttl <= p.ttl_max:
            raise Denied(400, f'Invalid lifetime (60 to {p.ttl_max} seconds for {name})')
        if type(cap) is not int or not 1 <= cap <= 10000:
            raise Denied(400, 'Invalid request budget')
        policy = body.get('disclosure_policy', '')
        if not isinstance(policy, str) or len(policy) > 4000:
            raise Denied(400, 'Invalid disclosure policy')
        label = bounded(body['label'], 'label', 100)
        purpose = body.get('purpose') or f'{name} pass'
        bounded(purpose, 'purpose', 2000)
        binds = body.get('bind', [])
        if not isinstance(binds, list) or len(binds) > self.limits.max_bindings:
            raise Denied(400, 'Invalid bindings')
        subjects = [subject_of(b) for b in binds]
        token = self.config.token_prefix + secrets.token_urlsafe(32)
        gid = secrets.token_hex(16)
        now = self.clock()
        with self.lock, self.db() as c:
            c.execute(
                'INSERT INTO grants(id,digest,label,purpose,profile,expires,max_calls,created,disclosure_policy) '
                'VALUES(?,?,?,?,?,?,?,?,?)',
                (gid, self.digest(token), label, purpose, name, now + ttl, cap, now, policy),
            )
            for s in subjects:
                self._bind(c, gid, s)
            self.audit(c, gid, 'issued')
        return {
            'grant_id': gid,
            'token': token,
            'profile': name,
            'label': label,
            'expires_at': now + ttl,
            'scopes': list(p.scopes),
            'max_calls': cap,
            'bindings': [s.key() for s in subjects],
        }

    def _bind(self, c, gid: str, s: Subject) -> None:
        old = c.execute('SELECT grant_id FROM bindings WHERE subject=?', (s.key(),)).fetchone()
        if old and old['grant_id'] != gid:
            live = c.execute(
                'SELECT 1 FROM grants WHERE id=? AND revoked=0 AND expires>?', (old['grant_id'], self.clock())
            ).fetchone()
            if live:
                raise Denied(409, 'That identity is already bound to another active pass')
        count = c.execute('SELECT count(*) FROM bindings WHERE grant_id=?', (gid,)).fetchone()[0]
        if count >= self.limits.max_bindings and not old:
            raise Denied(400, 'Too many bindings for this pass')
        c.execute('INSERT OR REPLACE INTO bindings VALUES(?,?,?)', (s.key(), gid, self.clock()))
        self.audit(c, gid, 'bound', s.platform)

    # ----- authentication -------------------------------------------------------------------

    def authenticate(self, cred: Credential, scope: str | None = None, c=None) -> dict:
        if c is None:
            with self.db() as conn:
                return self.authenticate(cred, scope, conn)
        if isinstance(cred, Subject):
            g = c.execute(
                'SELECT g.* FROM bindings b JOIN grants g ON g.id=b.grant_id WHERE b.subject=?', (cred.key(),)
            ).fetchone()
        else:
            if not cred or not isinstance(cred, str) or len(cred) > 200:
                raise Denied(401, 'Invalid or expired pass')
            g = c.execute('SELECT * FROM grants WHERE digest=?', (self.digest(cred),)).fetchone()
        if not g or g['revoked'] or g['expires'] <= self.clock():
            raise Denied(401, 'Invalid or expired pass')
        p = self.profile(g['profile'])
        if scope and scope not in p.scopes:
            raise Denied(403, 'Scope not granted')
        return dict(g)

    def me(self, cred: Credential) -> dict:
        g = self.authenticate(cred)
        p = self.profile(g['profile'])
        return {
            'grant_id': g['id'],
            'label': g['label'],
            'profile': g['profile'],
            'mode': p.mode,
            'expires_at': g['expires'],
            'calls': g['calls'],
            'max_calls': g['max_calls'],
            'scopes': list(p.scopes),
        }

    def redeem(self, token: str, subject: Subject) -> dict:
        """Bind a chat identity to the pass it presents, so later messages need no token."""
        with self.lock, self.db() as c:
            g = self.authenticate(token, c=c)
            if not self.profile(g['profile']).can_message:
                raise Denied(403, 'This pass cannot send messages')
            self._bind(c, g['id'], subject)
        return {'grant_id': g['id'], 'label': g['label'], 'profile': g['profile'], 'expires_at': g['expires']}

    # ----- visitor operations ---------------------------------------------------------------

    def reserve(self, c, g: dict, action: str) -> None:
        if g['calls'] >= g['max_calls']:
            raise Denied(429, 'Pass request budget exhausted')
        # A persistent rolling window, so restarts do not reset it.
        recent = c.execute(
            'SELECT count(*) FROM audit WHERE grant_id=? AND action=? AND at>?', (g['id'], action, self.clock() - 60)
        ).fetchone()[0]
        if recent >= self.profile(g['profile']).rate_per_minute:
            raise Denied(429, 'Rate limit reached; wait before submitting again')
        c.execute('UPDATE grants SET calls=calls+1 WHERE id=?', (g['id'],))

    def submit(self, cred: Credential, body: dict) -> tuple[dict, bool]:
        exact(body, ['message', 'request_id'], ['message', 'request_id'])
        message = bounded(body['message'], 'message', self.limits.max_message)
        key = bounded(body['request_id'], 'request_id', 100)
        with self.lock, self.db() as c:
            g = self.authenticate(cred, c=c)
            p = self.profile(g['profile'])
            if not p.can_message:
                raise Denied(403, 'Scope not granted')
            prior = c.execute('SELECT id,status FROM jobs WHERE grant_id=? AND request_key=?', (g['id'], key)).fetchone()
            if prior:
                return {'job_id': prior['id'], 'status': prior['status']}, False
            if c.execute("SELECT count(*) FROM jobs WHERE grant_id=? AND status IN ('queued','running')", (g['id'],)).fetchone()[
                0
            ]:
                raise Denied(409, 'One request is already active for this pass')
            if (
                c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
                >= self.limits.global_concurrency
            ):
                raise Denied(429, 'The gateway queue is full; retry shortly')
            if p.mode == 'reviewed':
                reviewed = [n for n, q in self.profiles.items() if q.mode == 'reviewed']
                marks = ','.join('?' * len(reviewed))
                busy = c.execute(
                    'SELECT count(*) FROM jobs j JOIN grants g ON j.grant_id=g.id '
                    f"WHERE g.profile IN ({marks}) AND j.status IN ('queued','running')",
                    reviewed,
                ).fetchone()[0]
                if busy >= self.limits.reviewed_concurrency:
                    raise Denied(429, 'A reviewed consultation is already running; retry shortly')
            self.reserve(c, g, 'ask')
            jid = secrets.token_hex(16)
            c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)', (jid, g['id'], key, 'queued', message, None, self.clock()))
            self.audit(c, g['id'], 'ask', jid)
            return {'job_id': jid, 'status': 'queued'}, True

    def job(self, cred: Credential, jid: str) -> dict:
        with self.db() as c:
            g = self.authenticate(cred, 'jobs.read', c)
            r = c.execute('SELECT id,status,result FROM jobs WHERE id=? AND grant_id=?', (jid, g['id'])).fetchone()
            if not r:
                raise Denied(404, 'Not found')
            return {'job_id': r['id'], 'status': r['status'], 'result': r['result']}

    def shared(self, cred: Credential, kind: str) -> dict:
        with self.db() as c:
            g = self.authenticate(cred, kind + '.read', c)
            fresh = g['shared_until'] > self.clock()
            if kind == 'context':
                return {'context': g['context'] if fresh else '', 'valid_until': g['shared_until']}
            slots = [s for s in json.loads(g['slots']) if timestamp(s['start']) > self.clock()] if fresh else []
            return {
                'slots': slots,
                'valid_until': g['shared_until'],
                'source': 'owner-published offers, not live calendar free/busy',
            }

    def propose(self, cred: Credential, body: dict) -> dict:
        exact(body, ['request_id', 'title', 'note', 'start', 'end'], ['request_id', 'title', 'note'])
        for k, limit in [('request_id', 100), ('title', 200), ('note', 2000)]:
            bounded(body[k], k, limit)
        if ('start' in body) != ('end' in body):
            raise Denied(400, 'Supply start and end together')
        with self.lock, self.db() as c:
            g = self.authenticate(cred, 'proposal.create', c)
            if 'start' in body:
                slots_checked([{'start': body['start'], 'end': body['end']}], self.clock())
                a, b = timestamp(body['start']), timestamp(body['end'])
                offered = g['shared_until'] > self.clock() and any(
                    timestamp(s['start']) <= a < b <= timestamp(s['end']) for s in json.loads(g['slots'])
                )
                if not offered:
                    raise Denied(400, 'Requested time is not inside a published offer')
            old = c.execute(
                'SELECT id,status FROM proposals WHERE grant_id=? AND request_key=?', (g['id'], body['request_id'])
            ).fetchone()
            if old:
                return {'proposal_id': old['id'], 'status': old['status']}
            self.reserve(c, g, 'proposal')
            pid = secrets.token_hex(16)
            c.execute(
                'INSERT INTO proposals VALUES(?,?,?,?,?,?)',
                (pid, g['id'], body['request_id'], json.dumps(body), 'pending', self.clock()),
            )
            self.audit(c, g['id'], 'proposal', pid)
            return {'proposal_id': pid, 'status': 'pending'}

    def proposal(self, cred: Credential, pid: str) -> dict:
        with self.db() as c:
            g = self.authenticate(cred, 'proposal.read', c)
            row = c.execute('SELECT id,status FROM proposals WHERE id=? AND grant_id=?', (pid, g['id'])).fetchone()
            if not row:
                raise Denied(404, 'Not found')
            return {'proposal_id': row['id'], 'status': row['status']}

    # ----- owner operations -----------------------------------------------------------------

    OWNER_OPS = (
        'issue',
        'grants',
        'revoke',
        'bind',
        'unbind',
        'publish',
        'inbox',
        'resolve',
        'reviews',
        'release',
        'audit',
        'profiles',
    )

    def owner(self, op: str, body: dict | None = None):
        """Single entry point for owner operations (CLI, owner socket, chat owner commands)."""
        body = body if body is not None else {}
        op = op.strip('/')
        if op not in self.OWNER_OPS:
            raise Denied(404, 'Not found')
        if op == 'issue':
            return self.issue(body)
        return getattr(self, '_owner_' + op)(body)

    def _owner_profiles(self, body):
        return [p.public() for p in self.profiles.values()]

    def _owner_grants(self, body):
        exact(body, ['all'])
        with self.db() as c:
            where = '' if body.get('all') else 'WHERE revoked=0 AND expires>?'
            args = () if body.get('all') else (self.clock(),)
            rows = [
                dict(r)
                for r in c.execute(
                    'SELECT id,label,purpose,profile,created,expires,revoked,calls,max_calls FROM grants '
                    f'{where} ORDER BY created DESC LIMIT 500',
                    args,
                )
            ]
            for r in rows:
                r['bindings'] = [b[0] for b in c.execute('SELECT subject FROM bindings WHERE grant_id=?', (r['id'],))]
            return rows

    def _find(self, c, gid):
        if not isinstance(gid, str) or not re.fullmatch(r'[a-f0-9]{4,32}', gid):
            raise Denied(400, 'Invalid grant id')
        rows = c.execute('SELECT * FROM grants WHERE id LIKE ?', (gid + '%',)).fetchall()
        if len(rows) != 1:
            raise Denied(404, 'No unique pass matches that id')
        return rows[0]

    def _owner_revoke(self, body):
        exact(body, ['grant_id'], ['grant_id'])
        with self.lock, self.db() as c:
            g = self._find(c, body['grant_id'])
            gid = g['id']
            c.execute("UPDATE grants SET revoked=1,context='',slots='[]' WHERE id=?", (gid,))
            c.execute("UPDATE jobs SET status='revoked',message=NULL,result=NULL WHERE grant_id=?", (gid,))
            c.execute('DELETE FROM reviews WHERE grant_id=?', (gid,))
            c.execute('DELETE FROM history WHERE grant_id=?', (gid,))
            c.execute('DELETE FROM bindings WHERE grant_id=?', (gid,))
            self.audit(c, gid, 'revoked')
            return {
                'revoked': gid,
                'label': g['label'],
                'note': 'Further access is blocked. Work the host agent already started is not undone.',
            }

    def _owner_bind(self, body):
        exact(body, ['grant_id', 'subject'], ['grant_id', 'subject'])
        s = subject_of(body['subject'])
        with self.lock, self.db() as c:
            g = self._find(c, body['grant_id'])
            if g['revoked'] or g['expires'] <= self.clock():
                raise Denied(404, 'No active pass')
            self._bind(c, g['id'], s)
            return {'grant_id': g['id'], 'bound': s.key()}

    def _owner_unbind(self, body):
        exact(body, ['subject'], ['subject'])
        s = subject_of(body['subject'])
        with self.lock, self.db() as c:
            row = c.execute('SELECT grant_id FROM bindings WHERE subject=?', (s.key(),)).fetchone()
            if not row:
                raise Denied(404, 'Not bound')
            c.execute('DELETE FROM bindings WHERE subject=?', (s.key(),))
            self.audit(c, row['grant_id'], 'unbound', s.platform)
            return {'unbound': s.key()}

    def _owner_publish(self, body):
        exact(body, ['grant_id', 'context', 'slots', 'valid_until'], ['grant_id', 'valid_until'])
        with self.lock, self.db() as c:
            g = self._find(c, body['grant_id'])
            if g['revoked'] or g['expires'] <= self.clock():
                raise Denied(404, 'No active pass')
            until = timestamp(body['valid_until'])
            if not self.clock() < until <= min(g['expires'], self.clock() + 7 * 86400):
                raise Denied(400, 'Shared data must expire within seven days and within the pass lifetime')
            scopes = self.profile(g['profile']).scopes
            context = body.get('context', '')
            if not isinstance(context, str) or len(context) > 16000 or (context and 'context.read' not in scopes):
                raise Denied(400, 'Context needs a pass with context.read and at most 16000 characters')
            slots = slots_checked(body.get('slots', []), self.clock())
            if slots and 'availability.read' not in scopes:
                raise Denied(400, 'Time offers need a pass with availability.read')
            c.execute(
                'UPDATE grants SET context=?,slots=?,shared_until=? WHERE id=?', (context, json.dumps(slots), until, g['id'])
            )
            self.audit(c, g['id'], 'published')
            return {'published': g['id'], 'valid_until': until}

    def _owner_inbox(self, body):
        with self.db() as c:
            return [
                dict(r)
                for r in c.execute(
                    'SELECT p.id,p.grant_id,g.label,p.body,p.created FROM proposals p JOIN grants g ON g.id=p.grant_id '
                    "WHERE p.status='pending' ORDER BY p.created LIMIT 100"
                )
            ]

    def _owner_resolve(self, body):
        exact(body, ['proposal_id', 'status'], ['proposal_id', 'status'])
        if body['status'] not in ('handled', 'declined'):
            raise Denied(400, 'Status is handled or declined')
        with self.lock, self.db() as c:
            if not c.execute('SELECT 1 FROM proposals WHERE id=?', (body['proposal_id'],)).fetchone():
                raise Denied(404, 'Not found')
            c.execute('UPDATE proposals SET status=? WHERE id=?', (body['status'], body['proposal_id']))
            return {'proposal_id': body['proposal_id'], 'status': body['status']}

    def _owner_reviews(self, body):
        with self.db() as c:
            return [
                dict(r)
                for r in c.execute(
                    'SELECT r.*,g.label,g.profile FROM reviews r JOIN grants g ON g.id=r.grant_id '
                    'WHERE g.revoked=0 AND g.expires>? ORDER BY r.created LIMIT 100',
                    (self.clock(),),
                )
            ]

    def _owner_release(self, body):
        exact(body, ['job_id', 'decision', 'answer'], ['job_id', 'decision'])
        with self.lock, self.db() as c:
            row = c.execute(
                'SELECT r.*,g.expires,g.revoked FROM reviews r JOIN grants g ON g.id=r.grant_id WHERE r.job_id=?',
                (body['job_id'],),
            ).fetchone()
            if not row or row['revoked'] or row['expires'] <= self.clock():
                raise Denied(404, 'No active review')
            decision = body['decision']
            if decision not in ('answer', 'decline'):
                raise Denied(400, 'Decision is answer or decline')
            answer = bounded(body.get('answer'), 'answer', 8000) if decision == 'answer' else DECLINE_MESSAGE
            if credential_shaped(answer, self.config.token_prefix):
                raise Denied(400, 'Credential-shaped answer rejected')
            status = 'completed' if decision == 'answer' else 'declined'
            c.execute('UPDATE jobs SET status=?,result=? WHERE id=?', (status, answer, row['job_id']))
            c.execute(
                'INSERT OR REPLACE INTO history VALUES(?,?,?,?,?)',
                (row['job_id'], row['grant_id'], row['question'], answer, self.clock()),
            )
            c.execute('DELETE FROM reviews WHERE job_id=?', (row['job_id'],))
            self.audit(c, row['grant_id'], 'owner_release', row['job_id'])
            return {'job_id': row['job_id'], 'decision': decision}

    def _owner_audit(self, body):
        exact(body, ['grant_id', 'limit'])
        limit = body.get('limit', 100)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise Denied(400, 'Invalid limit')
        with self.db() as c:
            if body.get('grant_id'):
                gid = self._find(c, body['grant_id'])['id']
                rows = c.execute('SELECT * FROM audit WHERE grant_id=? ORDER BY at DESC LIMIT ?', (gid, limit))
            else:
                rows = c.execute('SELECT * FROM audit ORDER BY at DESC LIMIT ?', (limit,))
            return [dict(r) for r in rows]

    # ----- retention ------------------------------------------------------------------------

    def cleanup(self) -> None:
        lim = self.limits
        with self.lock, self.db() as c:
            now = self.clock()
            dead = 'SELECT id FROM grants WHERE revoked=1 OR expires<=?'
            c.execute(
                "UPDATE jobs SET status=CASE WHEN status IN ('queued','running') THEN 'expired' ELSE status END,"
                f'message=NULL,result=NULL WHERE grant_id IN ({dead})',
                (now,),
            )
            c.execute("UPDATE grants SET context='',slots='[]' WHERE shared_until<=? OR expires<=? OR revoked=1", (now, now))
            c.execute(f'DELETE FROM bindings WHERE grant_id IN ({dead})', (now,))
            c.execute('DELETE FROM jobs WHERE created<?', (now - lim.job_retention,))
            c.execute('DELETE FROM reviews WHERE job_id NOT IN (SELECT id FROM jobs)')
            c.execute('DELETE FROM proposals WHERE created<?', (now - lim.proposal_retention,))
            c.execute('DELETE FROM audit WHERE at<?', (now - lim.audit_retention,))
            for table in ('reviews', 'history'):
                c.execute(f'DELETE FROM {table} WHERE created<? OR grant_id IN ({dead})', (now - lim.history_retention, now))
            c.execute('DELETE FROM grants WHERE expires<?', (now - lim.audit_retention,))
