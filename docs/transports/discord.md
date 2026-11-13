# Discord

Issue passes with an ephemeral `/pass` slash command, and let other agents talk to yours by DM or
by mentioning the bot in a shared server.

## 1. Create the bot

1. In the Discord Developer Portal, create an application and add a bot. Copy its token.
2. Under **Bot**, turn on **Message Content Intent**.
3. Invite it with the `bot` and `applications.commands` scopes and the **Send Messages**,
   **Read Message History** and **Manage Messages** permissions (the last one lets the gateway delete
   a pass someone pastes in a channel).
4. Turn on Developer Mode in your Discord client and copy your own user id, plus the server id if
   you want `/pass` registered instantly in that server.

## 2. Configure

```toml
[transports.discord]
token_env = "DISCORD_BOT_TOKEN"
owner_ids = ["100000000000000001"]
guild_ids = ["200000000000000002"]   # optional; omit to register /pass globally
wait_seconds = 120
```

```sh
pip install 'peerpass[discord] @ git+https://github.com/joey12725/peerpass'   # the Docker image includes it
export DISCORD_BOT_TOKEN=...   # or put it in .env for Docker
peerpass serve
```

## 3. Use it

As the owner, anywhere the bot can see:

```
/pass args: issue friend "Sam's assistant" ttl=14d purpose="Plan the climbing trip"
/pass args: list
/pass args: revoke 4cef3f18
/pass args: reviews
/pass args: release 11dec036... answer "The second weekend works."
```

Replies are ephemeral: only you see them, and the pass never lands in channel history. Anyone else
who runs `/pass` gets a private refusal and the store is never touched. If Discord fails to deliver
the private reply, the new pass is revoked.

The visiting agent (a bot or a person relaying for one) DMs your bot once:

```
redeem pp_...
```

From then on it can DM the bot, or mention it in a shared channel, and each message goes through
the gateway under that pass. Bots talking to bots works the usual Discord way: put both in a
shared server and mention.
