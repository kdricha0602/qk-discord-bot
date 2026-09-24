# Quantkernal Discord Bot

Ask your scan engine questions from Discord; get answers back as embeds.
Read-only — the bot only runs `SELECT`s against the Supabase Postgres that
backs the engine. (Alerts already flow the other way: Grafana → Discord via
webhook. This bot is the reverse direction: Discord → answers.)

## Commands

| Command | What it answers |
|---|---|
| `/status` | Engine health: heartbeat per tier, leader tier (👑), heartbeat ages, audit events in the last hour |
| `/alerts [limit]` | Recent fail / error / failover events from `audit_log`, newest first |
| `/scans [limit]` | Recent `scan_results` runs and which tier ran them |
| `/audit [limit]` | Recent `audit_log` entries, newest first |
| `/watermarks` | Current `scan_watermark` rows (resume state per scan) |
| `/help` | This list, in Discord |

`limit` defaults to 5, max 25. Until the engine deploys and writes data,
every command replies with a friendly "no data yet" instead of an error.

## Setup

### 1. Create the Discord application

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) → **New Application**, name it **Quantkernal**.
2. Open the **Bot** tab → **Reset Token** → copy the token. **Never paste it publicly** — treat it like a password.

### 2. Invite the bot to your server

1. In the same application, open **OAuth2 → URL Generator**.
2. Scopes: check **bot** and **applications.commands**.
3. Bot permissions: check **Send Messages**, **View Channel**, **Embed Links**.
4. Open the generated URL at the bottom and invite the bot to your server.

### 3. Deploy on Render

1. Push this folder to your repo (it deploys from `render.yaml`).
2. In Render: **New → Background Worker** → select the repo. The blueprint
   names it `quantkernal-discord-bot` on the free plan.
3. In the service's **Environment** tab, set:
   - `DISCORD_TOKEN` — the bot token from step 1 (paste it here, nowhere else)
   - `DATABASE_URL` — your Supabase **session pooler** postgres URL
     (`aws-0-ca-central-1.pooler.supabase.com:5432`, same one Grafana uses)
   - `DISCORD_GUILD_ID` — optional; your server's id for instant command sync
4. **Deploy.**

### 4. Verify

In Discord, type `/` — you should see `status`, `alerts`, `scans`, `audit`,
`watermarks`, `help` appear. Run `/status`. (Without `DISCORD_GUILD_ID`,
global command sync can take up to ~1 hour the first time.)

To find your server id for `DISCORD_GUILD_ID`: Discord settings → Advanced →
enable Developer Mode, then right-click your server → Copy Server ID.

## Config check

```bash
python bot.py --check-config
```

Prints which env vars are set (values redacted) and exits non-zero if
`DISCORD_TOKEN` or `DATABASE_URL` is missing.

## Notes

- Config is **env vars only** — no secrets in code, files, or chat.
- No privileged intents: the bot uses slash commands exclusively, so it never
  reads message content.
- One shared `asyncpg` pool; every command defers the interaction first (so
  slow queries don't time out) and returns a compact mobile-friendly embed.
- A per-command `try/except` turns query failures into a friendly error embed
  and logs the traceback server-side.
- `Dockerfile` uses `python:3.11-slim` and runs `bot.py`.
