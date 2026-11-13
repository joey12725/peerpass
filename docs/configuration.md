# Configuration

PeerPass reads one TOML file: `--config PATH`, else `$PEERPASS_CONFIG`, else `./peerpass.toml`,
else built-in defaults. Secrets never go in the file. Any setting ending in `_env` names the
environment variable that holds the secret, so the file is safe to commit.

`peerpass init` writes a starter file. [`examples/peerpass.toml`](../examples/peerpass.toml) shows
every section.

## `[peerpass]`

| Key | Default | Meaning |
|---|---|---|
| `host_name` | `"the host agent"` | What visitors call your agent; used in prompts and the agent card |
| `owner_name` | `"the owner"` | Who issues passes; used in prompts and the "waiting for review" message |
| `public_url` | `http://127.0.0.1:8750` | The URL visitors use; appears in handoffs and the agent card |
| `listen` | `127.0.0.1:8750` | Bind address. Docker sets `0.0.0.0:8750` via `PEERPASS_LISTEN` |
| `state_dir` | `./peerpass-data` | Database and owner socket. Relative to the config file |
| `timezone` | `UTC` | For expiry times in chat replies |
| `token_prefix` | `pp_` | 1 to 8 lowercase letters plus `_`. Also feeds the credential filter |
| `agent_card` | `true` | Serve `/.well-known/agent-card.json` |
| `default_profiles` | `true` | Keep `delegate`, `colleague` and `friend` alongside your own |

`PEERPASS_STATE_DIR`, `PEERPASS_LISTEN` and `PEERPASS_PUBLIC_URL` override the file.

## Profiles

```toml
[profiles.contractor]
mode = "reviewed"                 # "direct" or "reviewed"
scopes = ["agent.consult", "jobs.read", "proposal.create", "proposal.read"]
ttl_default = "7d"                # seconds, or 90m / 12h / 30d / 2w
ttl_max = "30d"
max_calls_default = 50            # per-pass budget unless the owner overrides at issue time
rate_per_minute = 4
tools = ["docs.search", "tickets.read"]   # allowlist handed to the host for each call
audience = "A contractor on one project. Share only what that project needs."
description = "Shown by `peerpass profiles`."
```

A section named after a built-in profile overrides only the keys you set.

### Scopes

| Scope | Allows |
|---|---|
| `agent.ask` | Direct messages to your agent. Direct profiles only |
| `agent.consult` | Reviewed consultations. Reviewed profiles only |
| `jobs.read` | Reading this pass's own job results |
| `context.read` | Context you publish for this pass (`peerpass owner publish`) |
| `availability.read` | Time offers you publish for this pass |
| `proposal.create` | Dropping a proposal into your inbox |
| `proposal.read` | Reading this pass's own proposal status |

Publishing context or offers is per pass and expires within seven days:

```sh
echo '{"grant_id": "4cef3f18", "valid_until": "2026-12-05T18:00:00-05:00",
       "slots": [{"start": "2026-12-03T18:00:00-05:00", "end": "2026-12-03T20:00:00-05:00"}]}' \
  | peerpass owner publish --json-stdin
```

Slots carry only start and end. Proposals must fit inside a published slot, and land in
`peerpass inbox` without booking or sending anything.

## `[limits]`

| Key | Default |
|---|---|
| `global_concurrency` | 4 jobs in flight |
| `reviewed_concurrency` | 1 reviewed consultation in flight |
| `max_message` | 16000 characters |
| `job_retention` | `24h` |
| `history_window` | 20 released exchanges given to the reasoner |
| `history_retention` | `7d` |
| `proposal_retention` | `7d` |
| `audit_retention` | `30d` (also how long expired pass metadata is kept) |
| `max_bindings` | 5 chat identities per pass |

## `[host]` and `[review_host]`

See [host adapters](host-adapters.md). `review_host` is optional; without it the reviewer call
goes to the same host with no tools.

## `[transports.<name>]`

Any of `discord`, `slack`, `telegram`, `matrix`. Each accepts `enabled = false`,
`owner_ids = [...]`, `wait_seconds` (how long a chat reply waits for your agent before saying
"still working") and its own credentials. See the [transport guides](transports/).
