# Slack

A `/pass` slash command for the owner, and DMs or mentions for visiting agents, over Slack's
Events API. No Slack SDK needed: requests are verified with your app's signing secret using the
standard library.

## 1. Create the app

Create a Slack app from this manifest, replacing the host name with your gateway's `public_url`:

```yaml
display_information:
  name: Atlas Gate
features:
  app_home:
    messages_tab_enabled: true
    messages_tab_read_only_enabled: false
  bot_user:
    display_name: atlas-gate
  slash_commands:
    - command: /pass
      url: https://atlas.example.com/slack/commands
      description: Issue, list or revoke agent access passes
      usage_hint: issue friend "Sam's assistant" ttl=14d
oauth_config:
  scopes:
    bot: [chat:write, commands, im:history, app_mentions:read]
settings:
  event_subscriptions:
    request_url: https://atlas.example.com/slack/events
    bot_events: [message.im, app_mention]
```

Install it to your workspace, then copy the **Bot User OAuth Token** (`xoxb-...`) and the
**Signing Secret**. Your own member id is under your profile's "Copy member ID".

## 2. Configure

```toml
[transports.slack]
bot_token_env = "SLACK_BOT_TOKEN"
signing_secret_env = "SLACK_SIGNING_SECRET"
owner_ids = ["U00000OWNER"]
```

The gateway must be reachable over HTTPS at `public_url`; Slack verifies the events URL when you
save the manifest, and the gateway answers the challenge automatically.

## 3. Use it

```
/pass issue colleague "Priya's PM agent" ttl=30d purpose="Sprint planning questions"
/pass list
/pass revoke 4cef3f18
```

`/pass` responses are ephemeral. Anyone not in `owner_ids` gets a private refusal.

A visiting agent DMs the bot `redeem pp_...` once, then talks to it by DM or by `@atlas-gate`
mention in a channel. Replies to a mention go into the same thread. Other Slack bots can be
visitors too: their bot id becomes the bound identity.
