"""``peerpass``: run the gateway and manage passes as the owner."""

from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

from . import __version__, a2a
from . import config as config_mod
from .config import duration
from .server import socket_path
from .store import Denied, Store

STARTER = """# PeerPass configuration. Secrets never go in this file: settings ending in _env name an
# environment variable that holds the secret.

[peerpass]
host_name = "Atlas"                 # what visitors call the agent behind the gateway
owner_name = "Alex"                 # who issues passes
public_url = "http://127.0.0.1:8750"
listen = "127.0.0.1:8750"
state_dir = "./peerpass-data"
timezone = "UTC"

# How the gateway reaches your agent. "echo" needs nothing and is handy for a first run.
[host]
adapter = "echo"

# [host]
# adapter = "openai"                # any OpenAI-compatible endpoint
# base_url = "http://127.0.0.1:11434/v1"
# model = "llama3.1"
# api_key_env = "HOST_API_KEY"

# [host]
# adapter = "webhook"               # POSTs signed JSON to your agent
# url = "http://127.0.0.1:9000/peerpass"
# secret_env = "PEERPASS_WEBHOOK_SECRET"

# Profiles: delegate, colleague and friend are built in. Override or add your own.
# [profiles.contractor]
# mode = "reviewed"
# scopes = ["agent.consult", "jobs.read", "proposal.create", "proposal.read"]
# ttl_default = "7d"
# ttl_max = "30d"
# max_calls_default = 50
# rate_per_minute = 4
# audience = "A contractor on one project. Share only what that project needs."

# [transports.telegram]
# token_env = "TELEGRAM_BOT_TOKEN"
# owner_ids = ["123456789"]
"""


def owner_call(args, op: str, body: dict | None = None):
    sock = args.socket or os.environ.get('PEERPASS_OWNER_SOCKET')
    if sock:
        conn = http.client.HTTPConnection('localhost', timeout=15)
        conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.sock.settimeout(15)
        conn.sock.connect(socket_path(sock))
        try:
            conn.request('POST', '/' + op, json.dumps(body or {}), {'Content-Type': 'application/json'})
            resp = conn.getresponse()
            result = json.loads(resp.read())
            if resp.status != 200:
                raise Denied(resp.status, result.get('error', 'Owner operation failed'))
            return result
        finally:
            conn.close()
    cfg = config_mod.load(args.config)
    return Store(cfg.db_path, cfg, recover=False).owner(op, body or {})


def when(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).astimezone().strftime('%Y-%m-%d %H:%M %Z')


def out(args, value, human=None):
    if args.json or human is None:
        print(json.dumps(value, indent=2))
    else:
        print(human(value))


def cmd_issue(args):
    body = {'profile': args.profile, 'label': args.label}
    if args.ttl:
        body['ttl'] = duration(args.ttl)
    if args.calls:
        body['max_calls'] = args.calls
    if args.purpose:
        body['purpose'] = args.purpose
    if args.policy:
        body['disclosure_policy'] = args.policy
    if args.bind:
        body['bind'] = args.bind
    g = owner_call(args, 'issue', body)

    def human(g):
        lines = [
            f'Issued {g["profile"]} pass "{g["label"]}"  grant {g["grant_id"]}',
            f'expires {when(g["expires_at"])}, {g["max_calls"]} requests, scopes: {", ".join(g["scopes"])}',
        ]
        if g['bindings']:
            lines.append('bound to ' + ', '.join(g['bindings']))
        lines += ['', 'Pass (shown once; hand it over as a secret):', g['token']]
        return '\n'.join(lines)

    out(args, g, human)


def cmd_list(args):
    rows = owner_call(args, 'grants', {'all': True} if args.all else {})

    def human(rows):
        if not rows:
            return 'No active passes.'
        lines = []
        for r in rows:
            state = 'revoked' if r['revoked'] else ('expired' if r['expires'] <= time.time() else 'active')
            bound = f'  bound {", ".join(r["bindings"])}' if r['bindings'] else ''
            lines.append(
                f'{r["id"][:8]}  {state:<7}  {r["profile"]:<10}  {r["calls"]:>4}/{r["max_calls"]:<5} '
                f'{when(r["expires"])}  {r["label"]}{bound}'
            )
        return '\n'.join(lines)

    out(args, rows, human)


def cmd_card(args):
    print(json.dumps(a2a.agent_card(config_mod.load(args.config)), indent=2))


def cmd_init(args):
    path = Path(args.path)
    if path.exists() and not args.force:
        sys.exit(f'{path} already exists (use --force to overwrite)')
    path.write_text(STARTER)
    print(f'Wrote {path}. Next: peerpass serve --config {path}')


def cmd_serve(args):
    from .gateway import Gateway
    from .hosts import build_host
    from .server import OwnerSocket, http_server
    from .transports import build_transport

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
    log = logging.getLogger('peerpass')
    cfg = config_mod.load(args.config)
    os.umask(0o077)
    store = Store(cfg.db_path, cfg)
    host_settings = dict(cfg.host)
    if host_settings.get('adapter', 'echo') == 'echo':
        host_settings.setdefault('name', cfg.host_name)
    gw = Gateway(store, build_host(host_settings), build_host(cfg.review_host) if cfg.review_host else None, cfg)
    host, _, port = cfg.listen.rpartition(':')
    server = http_server(gw, host.strip('[]') or '0.0.0.0', int(port))
    owner = OwnerSocket(cfg.owner_socket, gw)
    started = []
    for name, settings in cfg.transports.items():
        if not settings.get('enabled', True):
            continue
        t = build_transport(name, gw, {'timezone': cfg.timezone, **settings})
        if hasattr(t, 'routes'):
            server.routes.update(t.routes())
        t.start()
        started.append(name)
    threading.Thread(target=owner.serve_forever, daemon=True).start()

    def cleanup():
        while True:
            try:
                store.cleanup()
            except Exception:  # noqa: BLE001
                log.warning('cleanup failed')
            time.sleep(60)

    threading.Thread(target=cleanup, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    log.info(
        'peerpass %s listening on %s (owner socket %s; transports: %s)',
        __version__,
        cfg.listen,
        cfg.owner_socket,
        ', '.join(started) or 'http only',
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        owner.shutdown()
        Path(cfg.owner_socket).unlink(missing_ok=True)


def simple(op, fields=(), human=None):
    def run(args):
        body = {k: getattr(args, k) for k in fields if getattr(args, k, None) is not None}
        out(args, owner_call(args, op, body), human)

    return run


def cmd_release(args):
    body = {'job_id': args.job_id, 'decision': args.decision}
    if args.decision == 'answer':
        if not args.text:
            sys.exit('release ... answer needs the exact answer text')
        body['answer'] = args.text
    out(args, owner_call(args, 'release', body))


def cmd_raw(args):
    body = json.load(sys.stdin) if args.json_stdin else {}
    out(args, owner_call(args, args.op, body))


def cmd_audit(args):
    body = {'limit': args.limit}
    if args.grant_id:
        body['grant_id'] = args.grant_id
    out(
        args,
        owner_call(args, 'audit', body),
        lambda rows: (
            '\n'.join(f'{when(r["at"])}  {r["grant_id"][:8]}  {r["action"]:<14} {r["object_id"][:8]}' for r in rows)
            or 'No audit entries.'
        ),
    )


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='peerpass', description='Scoped, expiring, revocable access passes that let other agents talk to yours.'
    )
    p.add_argument('--version', action='version', version='peerpass ' + __version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--config', '-c', help='TOML config (default: $PEERPASS_CONFIG or ./peerpass.toml)')
    common.add_argument('--socket', help="use a running gateway's owner socket instead of opening the database")
    common.add_argument('--json', action='store_true', help='machine-readable output')
    sub = p.add_subparsers(dest='cmd', required=True, metavar='command')

    s = sub.add_parser('init', help='write a starter config')
    s.add_argument('path', nargs='?', default='peerpass.toml')
    s.add_argument('--force', action='store_true')
    s.set_defaults(fn=cmd_init)
    s = sub.add_parser('serve', parents=[common], help='run the gateway and any configured transports')
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser('card', parents=[common], help='print the A2A agent card')
    s.set_defaults(fn=cmd_card)
    s = sub.add_parser('profiles', parents=[common], help='list pass profiles')
    s.set_defaults(fn=simple('profiles'))

    s = sub.add_parser('issue', parents=[common], help='issue a pass')
    s.add_argument('profile')
    s.add_argument('label')
    s.add_argument('--ttl', help='lifetime, e.g. 90m, 12h, 30d (default: the profile default)')
    s.add_argument('--calls', type=int, help='request budget')
    s.add_argument('--purpose', help='context the host agent sees with every request')
    s.add_argument('--policy', help='disclosure policy for reviewed passes')
    s.add_argument('--bind', action='append', metavar='PLATFORM:ID', help='bind a chat identity (repeatable)')
    s.set_defaults(fn=cmd_issue)
    s = sub.add_parser('list', parents=[common], help='list passes')
    s.add_argument('--all', action='store_true', help='include revoked and expired')
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser('revoke', parents=[common], help='revoke a pass immediately')
    s.add_argument('grant_id')
    s.set_defaults(fn=simple('revoke', ['grant_id']))
    s = sub.add_parser('bind', parents=[common], help='bind a chat identity to a pass')
    s.add_argument('grant_id')
    s.add_argument('subject', metavar='PLATFORM:ID')
    s.set_defaults(fn=simple('bind', ['grant_id', 'subject']))
    s = sub.add_parser('unbind', parents=[common], help='remove a chat identity binding')
    s.add_argument('subject', metavar='PLATFORM:ID')
    s.set_defaults(fn=simple('unbind', ['subject']))
    s = sub.add_parser('reviews', parents=[common], help='consultations held for owner review')
    s.set_defaults(fn=simple('reviews'))
    s = sub.add_parser('release', parents=[common], help='release an exact answer, or decline, for a held job')
    s.add_argument('job_id')
    s.add_argument('decision', choices=['answer', 'decline'])
    s.add_argument('text', nargs='?')
    s.set_defaults(fn=cmd_release)
    s = sub.add_parser('inbox', parents=[common], help='pending proposals from visitors')
    s.set_defaults(fn=simple('inbox'))
    s = sub.add_parser('resolve', parents=[common], help='mark a proposal handled or declined')
    s.add_argument('proposal_id')
    s.add_argument('status', choices=['handled', 'declined'])
    s.set_defaults(fn=simple('resolve', ['proposal_id', 'status']))
    s = sub.add_parser('audit', parents=[common], help='recent audit log (metadata only, never message content)')
    s.add_argument('grant_id', nargs='?')
    s.add_argument('--limit', type=int, default=50)
    s.set_defaults(fn=cmd_audit)
    s = sub.add_parser('owner', parents=[common], help='raw owner operation with a JSON body (for scripts and agents)')
    s.add_argument('op', choices=Store.OWNER_OPS)
    s.add_argument('--json-stdin', action='store_true')
    s.set_defaults(fn=cmd_raw)
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        args.fn(args)
    except Denied as e:
        print(f'error: {e.message}', file=sys.stderr)
        return 1
    except (ValueError, OSError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 1
    return 0
