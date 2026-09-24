#!/usr/bin/env python3
"""Quantkernal — Discord slash-command bot for scan-engine ops.

Ask the scan engine questions from Discord; get answers back as embeds.
Read-only: every command is a SELECT against the Supabase Postgres that
backs the engine. Tables are empty until the engine deploys — every command
handles that with a friendly "no data yet" message instead of an error.

Slash commands only (no prefix commands, no MESSAGE_CONTENT privileged
intent needed).

Environment:
    DISCORD_TOKEN     bot token from the Discord developer portal (required)
    DATABASE_URL      Supabase session-pooler postgres URL (required)
    DISCORD_GUILD_ID  optional guild id -> commands sync instantly there;
                      without it, global sync can take up to ~1 hour

Usage:
    python bot.py                 # run the bot
    python bot.py --check-config  # verify env vars (values redacted)
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import timezone

import asyncpg
import discord
from discord import app_commands

log = logging.getLogger("quantkernal")

REQUIRED_ENV = ("DISCORD_TOKEN", "DATABASE_URL")
MAX_LIMIT = 25


# ---------------------------------------------------------------- helpers

def ts_rel(dt) -> str:
    """Discord relative timestamp, or an em dash when null."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:R>"


def trunc(s, n: int = 200) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def empty_embed(title: str) -> discord.Embed:
    return discord.Embed(
        title=title,
        description="No data yet — the engine hasn't deployed or hasn't written anything.",
        color=discord.Color.orange(),
    )


def error_embed() -> discord.Embed:
    return discord.Embed(
        title="Something went wrong",
        description="The query failed. Try again in a bit — the details are in the server logs.",
        color=discord.Color.red(),
    )


def clamp_limit(limit: int) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 5
    return max(1, min(limit, MAX_LIMIT))


# ---------------------------------------------------------------- bot

class QuantkernalBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())  # no privileged intents
        self.tree = app_commands.CommandTree(self)
        self.pool: asyncpg.Pool | None = None

    async def setup_hook(self) -> None:
        try:
            self.pool = await asyncpg.create_pool(
                os.environ["DATABASE_URL"], min_size=1, max_size=5, command_timeout=20
            )
        except Exception:
            log.exception("could not open Postgres pool — check DATABASE_URL")
            raise
        gid = os.environ.get("DISCORD_GUILD_ID")
        if gid:
            guild = discord.Object(id=int(gid))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("synced %d commands to guild %s", len(synced), gid)
        else:
            synced = await self.tree.sync()
            log.info("synced %d commands globally (can take ~1h to appear)", len(synced))

    async def on_ready(self) -> None:
        log.info("logged in as %s", self.user)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
        await super().close()


bot = QuantkernalBot()


# ---------------------------------------------------------------- commands

@bot.tree.command(name="status", description="Scan engine health: heartbeats, leader tier, recent activity")
async def status_cmd(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    try:
        rows = await bot.pool.fetch(
            "SELECT tier, last_beat, is_leader FROM tier_heartbeat ORDER BY tier"
        )
        recent = await bot.pool.fetchval(
            "SELECT COUNT(*) FROM audit_log WHERE ts > now() - interval '1 hour'"
        )
        if not rows:
            emb = empty_embed("Scan Engine Status")
        else:
            emb = discord.Embed(title="Scan Engine Status", color=discord.Color.green())
            for r in rows:
                label = f"{r['tier']}" + (" 👑 leader" if r["is_leader"] else "")
                emb.add_field(
                    name=label,
                    value=f"last beat {ts_rel(r['last_beat'])}",
                    inline=True,
                )
            emb.add_field(
                name="Audit events (last hour)",
                value=str(recent or 0),
                inline=False,
            )
        await interaction.followup.send(embed=emb)
    except Exception:
        log.exception("/status failed")
        await interaction.followup.send(embed=error_embed())


@bot.tree.command(name="alerts", description="Recent fail / error / failover events from the audit log")
@app_commands.describe(limit="How many rows to show (1-25, default 5)")
async def alerts_cmd(interaction: discord.Interaction, limit: int = 5) -> None:
    await interaction.response.defer()
    try:
        limit = clamp_limit(limit)
        rows = await bot.pool.fetch(
            """
            SELECT ts, tier, event, detail FROM audit_log
            WHERE event ILIKE '%fail%' OR event ILIKE '%error%' OR event ILIKE '%failover%'
            ORDER BY ts DESC LIMIT $1
            """,
            limit,
        )
        if not rows:
            emb = empty_embed("Recent Alerts")
        else:
            emb = discord.Embed(title="Recent Alerts", color=discord.Color.orange())
            for r in rows:
                emb.add_field(
                    name=f"⚠ {trunc(r['event'], 200)}",
                    value=f"{r['tier']} · {ts_rel(r['ts'])}\n{trunc(r['detail'])}",
                    inline=False,
                )
        await interaction.followup.send(embed=emb)
    except Exception:
        log.exception("/alerts failed")
        await interaction.followup.send(embed=error_embed())


@bot.tree.command(name="scans", description="Recent scan runs and which tier ran them")
@app_commands.describe(limit="How many rows to show (1-25, default 5)")
async def scans_cmd(interaction: discord.Interaction, limit: int = 5) -> None:
    await interaction.response.defer()
    try:
        limit = clamp_limit(limit)
        rows = await bot.pool.fetch(
            "SELECT id, scan_name, ran_at, ran_on_tier FROM scan_results "
            "ORDER BY ran_at DESC LIMIT $1",
            limit,
        )
        if not rows:
            emb = empty_embed("Recent Scans")
        else:
            emb = discord.Embed(title="Recent Scans", color=discord.Color.blurple())
            for r in rows:
                emb.add_field(
                    name=f"#{r['id']} {trunc(r['scan_name'], 200)}",
                    value=f"ran {ts_rel(r['ran_at'])} · tier {r['ran_on_tier']}",
                    inline=False,
                )
        await interaction.followup.send(embed=emb)
    except Exception:
        log.exception("/scans failed")
        await interaction.followup.send(embed=error_embed())


@bot.tree.command(name="audit", description="Recent audit log entries, newest first")
@app_commands.describe(limit="How many rows to show (1-25, default 5)")
async def audit_cmd(interaction: discord.Interaction, limit: int = 5) -> None:
    await interaction.response.defer()
    try:
        limit = clamp_limit(limit)
        rows = await bot.pool.fetch(
            "SELECT ts, tier, event, detail FROM audit_log ORDER BY ts DESC LIMIT $1",
            limit,
        )
        if not rows:
            emb = empty_embed("Audit Log")
        else:
            emb = discord.Embed(title="Audit Log", color=discord.Color.blurple())
            for r in rows:
                emb.add_field(
                    name=trunc(r["event"], 200),
                    value=f"{r['tier']} · {ts_rel(r['ts'])}\n{trunc(r['detail'])}",
                    inline=False,
                )
        await interaction.followup.send(embed=emb)
    except Exception:
        log.exception("/audit failed")
        await interaction.followup.send(embed=error_embed())


@bot.tree.command(name="watermarks", description="Current scan watermarks (resume state per scan)")
async def watermarks_cmd(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    try:
        # Column shape intentionally not assumed: render whatever comes back.
        rows = await bot.pool.fetch("SELECT * FROM scan_watermark ORDER BY 1")
        if not rows:
            emb = empty_embed("Scan Watermarks")
        else:
            emb = discord.Embed(title="Scan Watermarks", color=discord.Color.blurple())
            for i, r in enumerate(rows[:10]):
                lines = [f"{k}: {trunc(v, 120)}" for k, v in r.items()]
                emb.add_field(
                    name=f"row {i + 1}",
                    value="\n".join(lines)[:1024] or "—",
                    inline=False,
                )
        await interaction.followup.send(embed=emb)
    except Exception:
        log.exception("/watermarks failed")
        await interaction.followup.send(embed=error_embed())


COMMANDS_HELP = [
    ("/status", "Engine health: heartbeats per tier, leader tier, recent activity."),
    ("/alerts [limit]", "Recent fail / error / failover events from the audit log."),
    ("/scans [limit]", "Recent scan runs and which tier ran them."),
    ("/audit [limit]", "Recent audit log entries, newest first."),
    ("/watermarks", "Current scan watermarks (resume state per scan)."),
    ("/help", "This list."),
]


@bot.tree.command(name="help", description="List Quantkernal commands")
async def help_cmd(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    emb = discord.Embed(title="Quantkernal commands", color=discord.Color.greyple())
    for name, desc in COMMANDS_HELP:
        emb.add_field(name=name, value=desc, inline=False)
    emb.set_footer(text="Read-only — answers come straight from the scan engine database.")
    await interaction.followup.send(embed=emb)


# ---------------------------------------------------------------- entrypoint

def check_config() -> int:
    print("Quantkernal config check")
    ok = True
    for var in REQUIRED_ENV:
        val = os.environ.get(var)
        if val:
            print(f"  {var}: set ({len(val)} chars, value redacted)")
        else:
            print(f"  {var}: MISSING")
            ok = False
    gid = os.environ.get("DISCORD_GUILD_ID")
    if gid:
        print("  DISCORD_GUILD_ID: set (commands sync instantly to that guild)")
    else:
        print("  DISCORD_GUILD_ID: not set (global sync; can take ~1h to appear)")
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    args = sys.argv[1:] if argv is None else argv
    if "--check-config" in args:
        sys.exit(check_config())
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        print(
            f"error: missing required env vars: {', '.join(missing)}",
            file=sys.stderr,
        )
        print("hint: set them in your shell or Render dashboard; see README.md", file=sys.stderr)
        sys.exit(2)
    bot.run(os.environ["DISCORD_TOKEN"], log_handler=None)


if __name__ == "__main__":
    main()
