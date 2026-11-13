# Telegram

The quickest way to manage passes from your phone. Uses the Bot API with long polling, so the
gateway needs no public URL for Telegram.

## 1. Create the bot

1. Talk to @BotFather, run `/newbot`, and copy the token.
2. To use the bot in group chats, run `/setprivacy` and disable privacy mode, so it can see
   mentions and catch any pass pasted into the group.
3. Find your numeric Telegram user id (any user-id bot will tell you).

## 2. Configure

```toml
[transports.telegram]
token_env = "TELEGRAM_BOT_TOKEN"
owner_ids = ["123456789"]
```

```sh
export TELEGRAM_BOT_TOKEN=...
peerpass serve
```

## 3. Use it

In your private chat with the bot:

```
/pass issue delegate "Laptop coding agent" ttl=8h
/pass list
/pass revoke 4cef3f18
/pass reviews
```

Issuing only works in a private chat; in a group the bot tells you to DM it instead.

A visitor sends `/redeem pp_...` in a private chat with the bot. The bot deletes that message,
binds the visitor's account, and from then on routes their DMs, and their `@yourbot` mentions in
groups, through the gateway.
