#!/usr/bin/env python3
"""PeerPass visitor client: CLI and stdio MCP server for agents that hold an access pass.

Single file, Python 3.10+, no dependencies. Copy it anywhere, or fetch it from a gateway at
``<gateway>/client.py``. Configure with environment variables:

    PEERPASS_URL    gateway base URL (HTTPS; plain HTTP only for localhost)
    PEERPASS_TOKEN  the pass. Keep it in the environment, never on a command line.

Commands: me, ask, job, context, availability, propose, proposal, mcp.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

VERSION = '0.1.0'
USER_AGENT = 'peerpass-client/' + VERSION
LOOPBACK = ('localhost', '127.0.0.1', '::1')
TERMINAL = ('completed', 'declined', 'needs_owner', 'failed', 'interrupted', 'expired', 'revoked')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # never forward a bearer to another origin


def base_url():
    base = os.environ.get('PEERPASS_URL', '').rstrip('/')
    p = urllib.parse.urlsplit(base)
    if p.username or p.password or p.query or p.fragment or not p.hostname:
        raise RuntimeError('PEERPASS_URL must be a plain URL without credentials, query or fragment')
    if p.scheme != 'https' and not (p.scheme == 'http' and p.hostname in LOOPBACK):
        raise RuntimeError('PEERPASS_URL must use HTTPS (plain HTTP is allowed only for localhost)')
    return base


def request(path, body=None):
    base = base_url()
    token = os.environ.get('PEERPASS_TOKEN', '')
    if not token:
        raise RuntimeError('Set PEERPASS_TOKEN in this process environment')
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path,
        data=data,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', 'User-Agent': USER_AGENT},
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read(4096)).get('error', '')
        except Exception:  # noqa: BLE001 - only the gateway's own error contract is echoed
            detail = ''
        hint = {401: ' (pass expired or revoked; ask the owner for a new one)', 429: ' (budget or rate limit; slow down)'}.get(
            e.code, ''
        )
        raise RuntimeError(f'gateway HTTP {e.code}: {str(detail)[:200]}{hint}') from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError('connection failed; retry with the same request id') from None


def hex32(value, name):
    if not isinstance(value, str) or len(value) != 32 or any(c not in '0123456789abcdef' for c in value):
        raise RuntimeError('Invalid ' + name)
    return value


def tool(name, desc, properties=None, required=None):
    return {
        'name': name,
        'description': desc,
        'inputSchema': {
            'type': 'object',
            'properties': properties or {},
            'required': required or [],
            'additionalProperties': False,
        },
    }


S = {'type': 'string'}
TOOLS = [
    tool('peer_access', 'Show this pass: label, profile, expiry, budget and scopes.'),
    tool(
        'peer_ask',
        'Send a message to the host agent. Returns a job id; poll peer_job. Use a unique '
        'request_id and reuse it when retrying the same message.',
        {'message': S, 'request_id': S},
        ['message', 'request_id'],
    ),
    tool(
        'peer_job',
        'Read a job created by this pass. Poll until completed, declined, needs_owner, failed, '
        'interrupted, expired or revoked. needs_owner means the owner is reviewing; check back later.',
        {'job_id': S},
        ['job_id'],
    ),
    tool('peer_context', 'Read context the owner explicitly published for this pass.'),
    tool('peer_availability', 'Read time offers the owner published for this pass. Not a live calendar.'),
    tool(
        'peer_propose',
        "Put a proposal in the owner's inbox. Nothing is booked or sent automatically. "
        'start/end must fall inside a published offer.',
        {'request_id': S, 'title': S, 'note': S, 'start': S, 'end': S},
        ['request_id', 'title', 'note'],
    ),
    tool('peer_proposal', 'Read the status of a proposal created by this pass.', {'proposal_id': S}, ['proposal_id']),
]
SCOPE = {
    'peer_access': None,
    'peer_ask': ('agent.ask', 'agent.consult'),
    'peer_job': ('jobs.read',),
    'peer_context': ('context.read',),
    'peer_availability': ('availability.read',),
    'peer_propose': ('proposal.create',),
    'peer_proposal': ('proposal.read',),
}


def call_tool(name, args):
    spec = next((t for t in TOOLS if t['name'] == name), None)
    if spec is None:
        raise RuntimeError('Unknown tool')
    schema = spec['inputSchema']
    if not isinstance(args, dict) or set(args) - set(schema['properties']) or set(schema['required']) - set(args):
        raise RuntimeError('Invalid tool arguments')
    if any(not isinstance(v, str) for v in args.values()):
        raise RuntimeError('Tool arguments must be strings')
    if name == 'peer_ask':
        return request('/v1/ask', args)
    if name == 'peer_job':
        return request('/v1/jobs/' + hex32(args['job_id'], 'job id'))
    if name == 'peer_proposal':
        return request('/v1/proposals/' + hex32(args['proposal_id'], 'proposal id'))
    if name == 'peer_propose':
        return request('/v1/proposals', args)
    return request({'peer_access': '/v1/me', 'peer_context': '/v1/context', 'peer_availability': '/v1/availability'}[name])


def visible_tools(scopes):
    return [t for t in TOOLS if SCOPE[t['name']] is None or set(SCOPE[t['name']]) & set(scopes)]


def mcp(stdin=None, stdout=None):
    """MCP over stdio: one JSON-RPC message per line. Never writes logs to stdout."""
    stdin, stdout = stdin or sys.stdin.buffer, stdout or sys.stdout
    while True:
        line = stdin.readline(65537)
        if not line:
            return
        if len(line) > 65536:
            raise SystemExit('MCP input too large')
        msg = None
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0' or not isinstance(msg.get('method'), str):
                raise ValueError('Invalid JSON-RPC request')
            if 'id' not in msg:
                continue
            method, params = msg['method'], msg.get('params', {})
            if not isinstance(params, dict):
                raise ValueError('Invalid params')
            if method == 'initialize':
                asked = params.get('protocolVersion')
                version = asked if asked in ('2024-11-05', '2025-03-26', '2025-06-18') else '2025-06-18'
                result = {
                    'protocolVersion': version,
                    'capabilities': {'tools': {}},
                    'serverInfo': {'name': 'peerpass', 'version': VERSION},
                }
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': visible_tools(request('/v1/me')['scopes'])}
            elif method == 'tools/call':
                try:
                    value = call_tool(params.get('name'), params.get('arguments', {}))
                    result = {'content': [{'type': 'text', 'text': json.dumps(value)}]}
                except Exception as e:  # noqa: BLE001
                    result = {'isError': True, 'content': [{'type': 'text', 'text': str(e)}]}
            else:
                print(
                    json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'error': {'code': -32601, 'message': 'Method not found'}}),
                    file=stdout,
                    flush=True,
                )
                continue
            reply = {'jsonrpc': '2.0', 'id': msg['id'], 'result': result}
        except Exception as e:  # noqa: BLE001
            reply = {
                'jsonrpc': '2.0',
                'id': msg.get('id') if isinstance(msg, dict) else None,
                'error': {'code': -32603, 'message': str(e)},
            }
        print(json.dumps(reply), file=stdout, flush=True)


def main(argv=None):
    p = argparse.ArgumentParser(prog='peerpass-client', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    for name in ('me', 'context', 'availability', 'mcp'):
        sub.add_parser(name)
    ask = sub.add_parser('ask', help='send a message; prints the job (and the result with --wait)')
    ask.add_argument('message')
    ask.add_argument('--request-id', help='stable retry key; generated if omitted')
    ask.add_argument('--wait', type=int, default=0, metavar='SECONDS', help='poll up to this long (max 600)')
    job = sub.add_parser('job')
    job.add_argument('job_id')
    prop = sub.add_parser('propose', help='read {request_id,title,note,start?,end?} JSON from stdin')
    prop.add_argument('--json-stdin', action='store_true', required=True)
    status = sub.add_parser('proposal')
    status.add_argument('proposal_id')
    a = p.parse_args(argv)
    if a.cmd == 'mcp':
        return mcp()
    if a.cmd == 'ask':
        if not 0 <= a.wait <= 600:
            p.error('--wait must be between 0 and 600')
        key = a.request_id or str(uuid.uuid4())
        print('request_id: ' + key, file=sys.stderr)
        result = request('/v1/ask', {'message': a.message, 'request_id': key})
        until = time.monotonic() + a.wait
        while result.get('status') not in TERMINAL and time.monotonic() < until:
            time.sleep(2)
            result = call_tool('peer_job', {'job_id': result['job_id']})
    elif a.cmd == 'job':
        result = call_tool('peer_job', {'job_id': a.job_id})
    elif a.cmd == 'proposal':
        result = call_tool('peer_proposal', {'proposal_id': a.proposal_id})
    elif a.cmd == 'propose':
        result = request('/v1/proposals', json.load(sys.stdin))
    else:
        result = request('/v1/' + a.cmd)
    print(json.dumps(result, indent=2))
    return 0


def run():
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, OSError) as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    run()
