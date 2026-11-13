"""HTTP front door for visitors, plus a Unix-socket owner API that never touches the network.

Stdlib only. Deliberately strict: bounded bodies and threads, no query strings, no browser
origins, no redirects, no request logging, and opaque errors.
"""

from __future__ import annotations

import json
import os
import socketserver
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import a2a
from .store import Denied

MAX_BODY = 32768
# A raw route receives (headers, body_bytes) and returns (status, content_type, body_bytes).
RawRoute = Callable[[dict, bytes], tuple[int, str, bytes]]


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    server_version = 'peerpass'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        pass  # No URL, body, token or error-body logging, by design.

    def send(self, code: int, ctype: str, data: bytes, extra: dict | None = None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def read_body(self) -> bytes:
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) > 1:
            raise Denied(400, 'Invalid framing')
        n = int(self.headers.get('Content-Length', '0'))
        if n < 0 or n > MAX_BODY:
            raise Denied(413, 'Body too large')
        return self.rfile.read(n) if n else b''

    def handle_request(self):
        srv = self.server
        try:
            route = getattr(srv, 'routes', {}).get((self.command, self.path))
            if route is not None:
                code, ctype, data = route(dict(self.headers.items()), self.read_body())
                self.send(code, ctype, data)
                return
            if '?' in self.path or '#' in self.path:
                raise Denied(404, 'Not found')
            if not srv.owner and self.command == 'GET' and self.path == '/client.py':
                data = (Path(__file__).parent / 'client.py').read_bytes()
                self.send(
                    200, 'text/x-python; charset=utf-8', data, {'Content-Disposition': 'attachment; filename=peerpass_client.py'}
                )
                return
            if (
                not srv.owner
                and self.command == 'GET'
                and self.path == '/.well-known/agent-card.json'
                and srv.app.config.agent_card
            ):
                self.send(200, 'application/json', json.dumps(a2a.agent_card(srv.app.config)).encode())
                return
            if self.headers.get('Origin'):
                raise Denied(403, 'Browser-origin requests are disabled')
            if len(self.headers.get_all('Authorization', [])) > 1:
                raise Denied(401, 'Invalid authorization')
            raw = self.read_body()
            body = json.loads(raw) if raw else {}
            if srv.owner:
                result = srv.app.store.owner(self.path, body)
            else:
                auth = self.headers.get('Authorization', '')
                token = auth[7:] if auth.startswith('Bearer ') else ''
                result = srv.app.public(self.command, self.path, token, body)
            code = 200
        except Denied as e:
            code, result = e.status, {'error': e.message}
        except (ValueError, TypeError, TimeoutError):
            code, result = 400, {'error': 'Invalid request'}
        except Exception:  # noqa: BLE001
            code, result = 500, {'error': 'Internal failure'}
        extra = {'WWW-Authenticate': 'Bearer realm="peerpass"'} if code == 401 else None
        self.send(code, 'application/json', json.dumps(result).encode(), extra)

    do_GET = handle_request
    do_POST = handle_request


class LimitedHTTP(ThreadingHTTPServer):
    """Threaded HTTP server that sheds load past a fixed number of open connections."""

    daemon_threads = True

    def __init__(self, *args, capacity: int = 32, **kwargs):
        self.capacity = threading.BoundedSemaphore(capacity)
        self.owner, self.routes = False, {}
        super().__init__(*args, **kwargs)

    def process_request(self, request, address):
        if not self.capacity.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.capacity.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.capacity.release()


def socket_path(path) -> str:
    """Unix socket paths are capped near 104 bytes; use a relative path when that is shorter."""
    full = str(path)
    if len(full.encode()) < 100:
        return full
    rel = os.path.relpath(full)
    return rel if len(rel.encode()) < len(full.encode()) else full


class OwnerSocket(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path, app):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        old = os.umask(0o077)
        try:
            super().__init__(socket_path(path), Handler)
        finally:
            os.umask(old)
        os.chmod(path, 0o600)
        self.app, self.owner, self.routes = app, True, {}


def http_server(app, host: str, port: int) -> LimitedHTTP:
    server = LimitedHTTP((host, port), Handler)
    server.app = app
    return server
