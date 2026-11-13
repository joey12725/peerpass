# Threat model

PeerPass assumes the visiting agent is a stranger with a valid pass: possibly buggy, possibly
manipulated by its own inputs, possibly adversarial. Each layer below answers one question about
that visitor, and each answer is enforced in code rather than requested in a prompt.

## Assets

1. Your agent's credentials, model choice, tools and private context.
2. The ability to issue, extend or revoke access.
3. Other visitors' questions, answers and job results.
4. Your owner review queue and audit history.

## Layers

### 1. Who are you? Pass authentication

- Passes are 256 bits from `secrets.token_urlsafe`, prefixed (`pp_` by default) so leaks are easy
  to grep for and to catch with secret scanners.
- Only the SHA-256 digest is stored. A copy of the database does not contain usable passes.
- Every request re-checks expiry and revocation. There is no session cache to outlive a revoke.
- Passes only travel in the `Authorization` header. Query strings are rejected outright, the
  client refuses redirects, and plain HTTP is accepted only for loopback addresses.
- Browser `Origin` headers are rejected, so a web page cannot ride a visitor's network position.

### 2. What may you do? Scopes from the profile

- Scopes, mode, lifetime ceiling, rate limit and tool allowlist come from the profile the owner
  chose at issue time. The request body accepts exactly `message` and `request_id`; any other
  field is a 400.
- Jobs and proposals are looked up by `(id, grant)`. A pass cannot read another pass's work, and
  a guessed id returns the same 404 as a missing one.
- Owner operations do not exist on the HTTP listener at all. They live on a Unix socket, the local
  CLI and private chat commands from configured owner ids.

### 3. How much? Budgets and limits

- A per-pass request budget, persisted across restarts.
- A rolling per-minute rate limit computed from the audit log, so restarting the process does not
  reset it.
- One active job per pass, a global concurrency cap, and a separate cap for reviewed consultations.
- Bounded request bodies, bounded connection threads, socket timeouts, and bounded host replies.
- `request_id` makes retries idempotent, so a flaky network never runs a request twice.

### 4. What does your agent see? Server-written requests

- The gateway writes the system framing and picks the session id. The visitor's text is only ever
  the user turn, or a field inside a JSON envelope in reviewed mode.
- The tool allowlist for the call travels with the request. Host integrations that run their own
  tool loop call `peerpass.review.guard_tools()` before each batch.
- The visitor's pass is never forwarded. Your agent authenticates the gateway with its own
  credential, configured by environment variable.

### 5. What gets out? Reviewed release

For `reviewed` profiles, private reasoning and public release are separate steps:

- The reasoner can use private context and the profile's read tools.
- An independent reviewer call, with no tools and no private context, must return the identical
  answer. It can only make the decision stricter.
- A credential-shape filter catches passes, common API keys, bearer tokens, JWTs and private keys
  in any released text, including owner-written releases.
- Anything uncertain, malformed or failed is held for the owner. A reviewed pass never falls back
  to direct mode.
- History given to the reasoner contains only released answers, never drafts or review notes.

The reviewer is a model, and so is the reasoner. Treat reviewed mode as a strong default for
minimal disclosure, backed by the hard limits above (tool allowlist, no fallback, owner hold), and
choose the read tools you hand a profile accordingly.

### 6. What if a pass leaks?

- Revoke by id prefix from the CLI or chat; the next request fails.
- A live pass posted in a shared chat channel is revoked by the router on sight.
- If the private reply carrying a new pass cannot be delivered, that pass is revoked before the
  error is reported.
- Chat bindings let visitors stop sending the token at all after one `redeem` in a DM; the router
  deletes the redeem message where the platform allows.

### 7. What do you keep? Retention and audit

- The visitor's message is cleared from the job row as soon as it is dispatched.
- Results disappear at expiry or revocation and are deleted after `job_retention`.
- The audit log holds ids, actions and timestamps, never message content, and is pruned on a
  schedule.
- Restarts mark in-flight jobs `interrupted`; nothing is ever replayed automatically.
- The server logs no URLs, bodies, tokens or error details.

## Deployment hardening

The Docker image runs as UID 10001 with a read-only root filesystem, `cap_drop: ALL`,
`no-new-privileges`, and memory and PID limits. The compose file binds the port to localhost so
you choose the TLS terminator (Caddy, nginx, a Cloudflare or Tailscale tunnel). Secrets come from
environment variables; the TOML config only names them.

## Choosing a profile

| Visitor | Profile | Why |
|---|---|---|
| Your own coding agent for an afternoon | `delegate` | Direct access is the point, and you control the caller |
| A coworker's or team agent | `colleague` | Reviewed answers, assume the whole team reads them |
| A friend's assistant | `friend` | Reviewed answers scoped to the relationship |
| A contractor's tool for one project | custom reviewed profile | Narrow tools, short lifetime, small budget |

`delegate` passes give the visitor whatever your agent can do in its normal mode, so issue them
only to agents you run.
