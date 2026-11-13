# Matrix

Works with any homeserver through the client-server API. A room with exactly two members (the bot
and one other account) counts as a private chat.

## 1. Create the bot account

Register an account for the gateway (for example `@atlas-gate:example.org`) and get an access
token:

```sh
curl -s https://matrix.example.org/_matrix/client/v3/login -d '{
  "type": "m.login.password", "identifier": {"type": "m.id.user", "user": "atlas-gate"},
  "password": "...", "initial_device_display_name": "peerpass"}'
```

Keep the `access_token` from the response.

## 2. Configure

```toml
[transports.matrix]
homeserver = "https://matrix.example.org"
user_id = "@atlas-gate:example.org"
access_token_env = "MATRIX_ACCESS_TOKEN"
owner_ids = ["@alex:example.org"]
auto_join = true
```

The bot reads plain-text room events, so create its DM rooms with encryption turned off.

## 3. Use it

Invite the bot to a direct chat. It joins automatically. Then:

```
!pass issue friend "Sam's assistant" ttl=14d
!pass list
!pass revoke 4cef3f18
```

(`/pass` works too in clients that send unknown slash commands as text.)

Visitors invite the bot to their own direct chat and send `redeem pp_...`; the bot redacts that
message and binds their account. In shared rooms they address the bot with `atlas-gate: ...` or a
mention. Messages from before the gateway started are never acted on.
