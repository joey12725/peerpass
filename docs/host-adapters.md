# Host adapters

A host adapter delivers one request to your agent and returns its reply as text. By the time it
runs, the gateway has authenticated the pass, charged its budget and written everything except the
visitor's message.

```python
@dataclass(frozen=True)
class HostRequest:
    mode: str  # "direct", "reasoner" or "reviewer"
    system: str  # gateway-written instructions
    message: str  # visitor text (direct) or a JSON envelope (reasoner / reviewer)
    session_id: str  # "peerpass-<grant id>", stable per pass
    grant: dict  # id, label, profile, purpose
    tools: frozenset  # tool allowlist for this call; empty means none
```

An adapter is any callable `HostRequest -> str`. Raise anything on failure: the visitor sees a
generic message and, for reviewed passes, the request is held for the owner.

In `reasoner` and `reviewer` modes the reply must be the JSON decision described in
[architecture](architecture.md#direct-and-reviewed-modes). Models that support JSON mode get it
switched on automatically by the OpenAI-compatible adapter.

## `openai`: any chat completions endpoint

```toml
[host]
adapter = "openai"
base_url = "http://127.0.0.1:11434/v1"   # Ollama, vLLM, LM Studio, OpenRouter, OpenAI...
model = "llama3.1"
api_key_env = "HOST_API_KEY"             # optional
session_header = "X-Session-Id"          # optional: send the per-pass session id
json_mode = true                         # response_format=json_object for reviewed calls
timeout = 300
extra_body = { temperature = 0.2 }       # merged into every request
```

Many agent runtimes expose their full agent (memory, tools, persona) behind a chat completions
endpoint. Point `base_url` there and visitors talk to the agent itself, not a bare model.

## `webhook`: signed JSON to your own service

```toml
[host]
adapter = "webhook"
url = "https://agent.internal.example/peerpass"
secret_env = "PEERPASS_WEBHOOK_SECRET"
```

Request body:

```json
{"version": 1, "mode": "direct", "system": "...", "message": "...",
 "session_id": "peerpass-4cef...", "grant": {"id": "...", "label": "...", "profile": "...", "purpose": "..."},
 "tools": ["docs.search"]}
```

Headers: `X-PeerPass-Timestamp` (unix seconds) and
`X-PeerPass-Signature: v1=<hex HMAC-SHA256(secret, "<timestamp>.<raw body>")>`. Verify with
`peerpass.hosts.webhook.verify_signature(secret, ts, body, signature)`, which also enforces a
five-minute replay window. Reply with `{"reply": "..."}`.
[`examples/webhook_host.py`](../examples/webhook_host.py) is a complete receiver in 50 lines.

## `mcp`: an agent exposed as an MCP server

```toml
[host]
adapter = "mcp"
command = ["my-agent", "mcp"]     # started fresh for every request
tool = "ask"                      # tool to call
message_arg = "message"           # argument that receives the visitor message
system_arg = "instructions"       # argument that receives the gateway framing ("" to omit)
pass_env = ["MY_AGENT_API_KEY"]   # environment variables to pass through; nothing else is inherited
```

## `python`: in-process

```toml
[host]
adapter = "python"
target = "my_package.peerpass_host:make_host"   # called with the remaining keys as kwargs
```

```python
from peerpass.hosts import HostRequest
from peerpass.review import guard_tools


def make_host(**settings):
    agent = MyAgent(**settings)

    def host(request: HostRequest) -> str:
        def on_tool_calls(names):
            guard_tools(names, request.tools)  # raise before running anything off-list

        return agent.run(
            system=request.system,
            user=request.message,
            session=request.session_id,
            tools=request.tools,
            before_tools=on_tool_calls,
        )

    return host
```

## `echo`

Replies without a model. `peerpass init` starts with it so the whole flow works on a fresh
machine; it is also what the Docker smoke test uses.
