# Visiting agents

A visiting agent needs two values from the owner: the gateway URL and the pass. Keep the pass in
the process environment (`PEERPASS_URL`, `PEERPASS_TOKEN`), never in a command line, URL, file or
chat message.

## Single-file client

`peerpass-client` comes with the package. The same code is a standalone file served by every
gateway at `<gateway>/client.py`: Python 3.10+, no dependencies.

```sh
curl -fsSLo peerpass_client.py https://atlas.example.com/client.py   # read it before running it
python3 peerpass_client.py me
python3 peerpass_client.py ask "Is Thursday lunch possible?" --wait 120
python3 peerpass_client.py job 11dec03609b2ce0181522d1a837830d5
python3 peerpass_client.py availability
echo '{"request_id":"lunch-1","title":"Lunch","note":"Team offsite planning",
       "start":"2026-12-03T12:00:00-05:00","end":"2026-12-03T13:00:00-05:00"}' \
  | python3 peerpass_client.py propose --json-stdin
```

`ask` prints the `request_id` it used. Reuse it with `--request-id` when retrying after a network
error, so the request runs once.

## MCP

Register the client as a stdio MCP server. Tools appear only for the scopes the pass has:
`peer_access`, `peer_ask`, `peer_job`, `peer_context`, `peer_availability`, `peer_propose`,
`peer_proposal`.

```json
{
  "mcpServers": {
    "atlas": {
      "command": "peerpass-client",
      "args": ["mcp"],
      "env": { "PEERPASS_URL": "https://atlas.example.com", "PEERPASS_TOKEN": "${ATLAS_PASS}" }
    }
  }
}
```

For agents that read skills, [`skills/ask-peer`](../skills/ask-peer/SKILL.md) covers etiquette:
reuse request ids, wait out `needs_owner`, stop on 401.

## A2A

The gateway publishes an Agent Card at `/.well-known/agent-card.json` declaring an HTTP bearer
security scheme. Authenticate with the pass, then:

```sh
curl -s https://atlas.example.com/a2a -H "Authorization: Bearer $PEERPASS_TOKEN" -d '{
  "jsonrpc": "2.0", "id": 1, "method": "message/send",
  "params": {"message": {"role": "user", "messageId": "msg-001",
                         "parts": [{"kind": "text", "text": "Is Thursday lunch possible?"}]}}}'
```

The response is a Task. Poll it with `tasks/get` (`{"id": "<task id>"}`). `messageId` is the
idempotency key. States map as: queued to `submitted`; running or held for owner review to
`working`; completed to `completed` with a text artifact; declined to `rejected`; anything else to
`failed`.

## HTTP API

All endpoints take `Authorization: Bearer <pass>` and JSON bodies. Unknown fields are rejected.

| Method | Path | Scope | Body / result |
|---|---|---|---|
| GET | `/health` | none | `{"status": "ok"}` |
| GET | `/client.py` | none | the single-file client |
| GET | `/.well-known/agent-card.json` | none | A2A agent card |
| GET | `/v1/me` | any | label, profile, mode, expiry, calls used, budget, scopes |
| POST | `/v1/ask` | `agent.ask` or `agent.consult` | `{message, request_id}` to `{job_id, status}` |
| GET | `/v1/jobs/{id}` | `jobs.read` | `{job_id, status, result}` |
| GET | `/v1/context` | `context.read` | `{context, valid_until}` |
| GET | `/v1/availability` | `availability.read` | `{slots, valid_until}` |
| POST | `/v1/proposals` | `proposal.create` | `{request_id, title, note, start?, end?}` to `{proposal_id, status}` |
| GET | `/v1/proposals/{id}` | `proposal.read` | `{proposal_id, status}` |
| POST | `/a2a` | as `/v1/ask` | A2A JSON-RPC |

Job statuses: `queued`, `running`, `completed`, `declined`, `needs_owner`, `failed`,
`interrupted`, `expired`, `revoked`.

Errors are `{"error": "..."}` with 400 (bad request), 401 (pass invalid, expired or revoked), 403
(scope not granted, browser origin), 404, 409 (one job already active for this pass), 413 (body
too large) or 429 (budget, rate limit or queue full).

## Chat

On Discord, Slack, Telegram or Matrix, DM the gateway bot `redeem <pass>` once. That binds your
account to the pass and the bot deletes the message where the platform allows. After that, DM it
or mention it in a shared channel and it routes your message through the gateway. `status <job id>`
checks on anything slow. Never paste a pass into a shared channel: the gateway revokes it on sight.
