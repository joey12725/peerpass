"""A2A protocol interop: a public Agent Card plus ``message/send`` and ``tasks/get`` over JSON-RPC.

An A2A client points at ``/.well-known/agent-card.json``, authenticates with the pass as an
HTTP bearer token, and gets the same scopes, budgets, review flow and audit trail as the
native ``/v1`` API. Only non-streaming calls are offered; tasks are polled with ``tasks/get``.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from . import __version__
from .store import Denied

if TYPE_CHECKING:
    from .gateway import Gateway

PROTOCOL_VERSION = '0.3.0'

STATE = {
    'queued': 'submitted',
    'running': 'working',
    'needs_owner': 'working',
    'completed': 'completed',
    'declined': 'rejected',
    'failed': 'failed',
    'interrupted': 'failed',
    'expired': 'failed',
    'revoked': 'failed',
}


def agent_card(config) -> dict:
    base = config.public_url.rstrip('/')
    return {
        'protocolVersion': PROTOCOL_VERSION,
        'name': config.host_name,
        'description': f'{config.host_name}, reachable by visiting agents that hold an access pass '
        f'issued by {config.owner_name}. Every request is scoped, rate limited and audited.',
        'url': base + '/a2a',
        'preferredTransport': 'JSONRPC',
        'version': __version__,
        'capabilities': {'streaming': False, 'pushNotifications': False},
        'securitySchemes': {
            'peerpass': {
                'type': 'http',
                'scheme': 'bearer',
                'description': 'An owner-issued PeerPass access pass. Ask the owner for one; there is no self-service issuance.',
            }
        },
        'security': [{'peerpass': []}],
        'defaultInputModes': ['text/plain'],
        'defaultOutputModes': ['text/plain'],
        'skills': [
            {
                'id': 'ask',
                'name': 'Ask',
                'tags': ['peerpass', 'scoped-access'],
                'description': 'Send a message. Depending on the pass, it reaches the agent directly or goes '
                'through a reviewed consultation that releases only a minimal answer.',
            }
        ],
    }


def _task(job: dict, context_id: str) -> dict:
    state = STATE.get(job['status'], 'failed')
    task = {
        'kind': 'task',
        'id': job['job_id'],
        'contextId': context_id,
        'status': {'state': state, 'timestamp': dt.datetime.now(dt.UTC).isoformat()},
    }
    text = job.get('result')
    if state == 'completed' and text:
        task['artifacts'] = [{'artifactId': job['job_id'], 'name': 'reply', 'parts': [{'kind': 'text', 'text': text}]}]
    elif text:
        task['status']['message'] = {
            'kind': 'message',
            'role': 'agent',
            'messageId': job['job_id'] + '-status',
            'parts': [{'kind': 'text', 'text': text}],
        }
    return task


def _error(ident, code: int, message: str) -> dict:
    return {'jsonrpc': '2.0', 'id': ident, 'error': {'code': code, 'message': message}}


def _code(status: int) -> int:
    return {400: -32602, 404: -32001, 403: -32004, 409: -32000, 429: -32000}.get(status, -32603)


def handle(gw: Gateway, token: str, body) -> dict:
    # Authenticate before parsing anything else; a bad pass is a plain HTTP 401.
    grant = gw.store.authenticate(token)
    if not isinstance(body, dict) or body.get('jsonrpc') != '2.0' or not isinstance(body.get('method'), str):
        return _error(body.get('id') if isinstance(body, dict) else None, -32600, 'Invalid request')
    ident, method, params = body.get('id'), body['method'], body.get('params') or {}
    if not isinstance(params, dict):
        return _error(ident, -32602, 'Invalid params')
    try:
        if method == 'message/send':
            msg = params.get('message')
            if not isinstance(msg, dict) or not isinstance(msg.get('parts'), list):
                return _error(ident, -32602, 'message.parts is required')
            text = '\n'.join(
                p.get('text', '') for p in msg['parts'] if isinstance(p, dict) and p.get('kind', p.get('type')) == 'text'
            )
            request_id = msg.get('messageId')
            if not isinstance(request_id, str):
                return _error(ident, -32602, 'message.messageId is required')
            submitted = gw.submit(token, {'message': text, 'request_id': request_id})
            job = (
                gw.store.job(token, submitted['job_id'])
                if 'jobs.read' in gw.store.profile(grant['profile']).scopes
                else {'job_id': submitted['job_id'], 'status': submitted['status'], 'result': None}
            )
            return {'jsonrpc': '2.0', 'id': ident, 'result': _task(job, grant['id'])}
        if method == 'tasks/get':
            tid = params.get('id')
            if not isinstance(tid, str) or len(tid) != 32:
                return _error(ident, -32001, 'Task not found')
            return {'jsonrpc': '2.0', 'id': ident, 'result': _task(gw.store.job(token, tid), grant['id'])}
        if method == 'tasks/cancel':
            return _error(ident, -32002, 'Task cannot be canceled')
        return _error(ident, -32601, 'Method not found')
    except Denied as e:
        if e.status == 401:
            raise
        return _error(ident, _code(e.status), e.message)
