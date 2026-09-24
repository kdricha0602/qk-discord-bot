#!/usr/bin/env python3
"""Quantkernal — Discord interactions-webhook bot (Vercel serverless).

Discord POSTs interaction JSON to /api/interactions; we verify the
Ed25519 signature, answer the 6 scan-engine commands via Supabase
PostgREST, and return response JSON. No gateway connection, no bot
token needed at runtime, no persistent process.

Env (set in the Vercel project settings):
    DISCORD_PUBLIC_KEY    application public key (hex) from the Discord dev portal
    SUPABASE_URL          e.g. https://xyz.supabase.co
    SUPABASE_SERVICE_KEY  service_role key — server-side only, never expose

Interactions endpoint URL to register in the Discord dev portal:
    https://<project>.vercel.app/api/interactions
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

import httpx
try:  # PyNaCl >= 1.6 renamed BadSignature -> BadSignatureError
    from nacl.exceptions import BadSignatureError as BadSignature
except ImportError:
    from nacl.exceptions import BadSignature
from nacl.signing import VerifyKey

log = logging.getLogger("quantkernal-webhook")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)

MAX_LIMIT = 25
DB_TIMEOUT = 8.0

# discord.py color values, hardcoded so embeds match bot.py exactly.
COLOR_GREEN = 0x2ECC71    # discord.Color.green()
COLOR_ORANGE = 0xE67E22   # discord.Color.orange()
COLOR_BLURPLE = 0x5865F2  # discord.Color.blurple()
COLOR_RED = 0xE74C3C      # discord.Color.red()
COLOR_GREYPLE = 0x99AAB5  # discord.Color.greyple()

NO_DATA = "No data yet — the engine hasn't deployed or hasn't written anything."

COMMANDS_HELP = [
    ("/status", "Engine health: heartbeats per tier, leader tier, recent activity."),
    ("/alerts [limit]", "Recent fail / error / failover events from the audit log."),
    ("/scans [limit]", "Recent scan runs and which tier ran them."),
    ("/audit [limit]", "Recent audit log entries, newest first."),
    ("/watermarks", "Current scan watermarks (resume state per scan)."),
    ("/help", "This list."),
]


# ---------------------------------------------------------------- helpers

def ts_rel(value) -> str:
    """Discord relative timestamp, or an em dash when null/unparseable."""
    if value is None:
        return "—"
    dt = value
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:R>"


def trunc(s, n: int = 200) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def empty_embed(title: str) -> dict:
    return {"title": title, "description": NO_DATA, "color": COLOR_ORANGE}


def error_embed() -> dict:
    return {
        "title": "Something went wrong",
        "description": "The query failed. Try again in a bit — the details are in the server logs.",
        "color": COLOR_RED,
    }


def clamp_limit(limit) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 5
    return max(1, min(limit, MAX_LIMIT))


def load_config():
    """Return dict of env config, or None when any required var is missing."""
    public_key = os.environ.get("DISCORD_PUBLIC_KEY")
    url = os.environ.get("SUPABASE_URL")
    service_key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not public_key or not url or not service_key:
        return None
    return {"public_key": public_key, "url": url, "service_key": service_key}


def verify_signature(public_key_hex: str, signature_hex: str, timestamp: str, body: bytes) -> bool:
    try:
        vk = VerifyKey(bytes.fromhex(public_key_hex))
        vk.verify(timestamp.encode("utf-8") + body, bytes.fromhex(signature_hex))
        return True
    except (BadSignature, ValueError):
        return False


def supa_get(cfg: dict, table: str, params: dict, extra_headers: dict | None = None):
    """GET one PostgREST resource. Raises on HTTP error."""
    url = cfg["url"].rstrip("/") + "/rest/v1/" + table
    headers = {
        "apikey": cfg["service_key"],
        "Authorization": "Bearer " + cfg["service_key"],
    }
    if extra_headers:
        headers.update(extra_headers)
    with httpx.Client(timeout=DB_TIMEOUT) as client:
        resp = client.get(url, params=params, headers=headers)
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------- commands

def cmd_status(cfg: dict) -> dict:
    # SELECT tier, last_beat, is_leader FROM tier_heartbeat ORDER BY tier
    rows = supa_get(
        cfg,
        "tier_heartbeat",
        {"select": "tier,last_beat,is_leader", "order": "tier.asc"},
    ).json()
    # SELECT COUNT(*) FROM audit_log WHERE ts > now() - interval '1 hour'
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    r = supa_get(
        cfg,
        "audit_log",
        {"select": "ts", "ts": f"gt.{since}", "limit": "1"},
        {"Prefer": "count=exact"},
    )
    cr = r.headers.get("content-range", "")
    try:
        recent = int(cr.split("/")[-1])
    except (ValueError, IndexError):
        recent = len(r.json())
    if not rows:
        return empty_embed("Scan Engine Status")
    fields = []
    for row in rows:
        label = str(row.get("tier")) + (" 👑 leader" if row.get("is_leader") else "")
        fields.append(
            {
                "name": label,
                "value": f"last beat {ts_rel(row.get('last_beat'))}",
                "inline": True,
            }
        )
    fields.append(
        {"name": "Audit events (last hour)", "value": str(recent or 0), "inline": False}
    )
    return {"title": "Scan Engine Status", "color": COLOR_GREEN, "fields": fields}


def cmd_alerts(cfg: dict, limit: int) -> dict:
    # SELECT ts, tier, event, detail FROM audit_log
    # WHERE event ILIKE '%fail%' OR event ILIKE '%error%' OR event ILIKE '%failover%'
    # ORDER BY ts DESC LIMIT n
    rows = supa_get(
        cfg,
        "audit_log",
        {
            "select": "ts,tier,event,detail",
            "or": "(event.ilike.*fail*,event.ilike.*error*,event.ilike.*failover*)",
            "order": "ts.desc",
            "limit": str(limit),
        },
    ).json()
    if not rows:
        return empty_embed("Recent Alerts")
    fields = [
        {
            "name": f"⚠ {trunc(row.get('event'), 200)}",
            "value": f"{row.get('tier')} · {ts_rel(row.get('ts'))}\n{trunc(row.get('detail'))}",
            "inline": False,
        }
        for row in rows
    ]
    return {"title": "Recent Alerts", "color": COLOR_ORANGE, "fields": fields}


def cmd_scans(cfg: dict, limit: int) -> dict:
    # SELECT id, scan_name, ran_at, ran_on_tier FROM scan_results
    # ORDER BY ran_at DESC LIMIT n
    rows = supa_get(
        cfg,
        "scan_results",
        {
            "select": "id,scan_name,ran_at,ran_on_tier",
            "order": "ran_at.desc",
            "limit": str(limit),
        },
    ).json()
    if not rows:
        return empty_embed("Recent Scans")
    fields = [
        {
            "name": f"#{row.get('id')} {trunc(row.get('scan_name'), 200)}",
            "value": f"ran {ts_rel(row.get('ran_at'))} · tier {row.get('ran_on_tier')}",
            "inline": False,
        }
        for row in rows
    ]
    return {"title": "Recent Scans", "color": COLOR_BLURPLE, "fields": fields}


def cmd_audit(cfg: dict, limit: int) -> dict:
    # SELECT ts, tier, event, detail FROM audit_log ORDER BY ts DESC LIMIT n
    rows = supa_get(
        cfg,
        "audit_log",
        {
            "select": "ts,tier,event,detail",
            "order": "ts.desc",
            "limit": str(limit),
        },
    ).json()
    if not rows:
        return empty_embed("Audit Log")
    fields = [
        {
            "name": trunc(row.get("event"), 200),
            "value": f"{row.get('tier')} · {ts_rel(row.get('ts'))}\n{trunc(row.get('detail'))}",
            "inline": False,
        }
        for row in rows
    ]
    return {"title": "Audit Log", "color": COLOR_BLURPLE, "fields": fields}


def cmd_watermarks(cfg: dict) -> dict:
    # SELECT * FROM scan_watermark ORDER BY 1
    # (PostgREST needs a named column for ordering; the column shape is not
    # assumed, so we take the returned order and render the first 10 rows
    # generically, exactly like bot.py.)
    rows = supa_get(cfg, "scan_watermark", {"select": "*"}).json()
    if not rows:
        return empty_embed("Scan Watermarks")
    fields = []
    for i, row in enumerate(rows[:10]):
        lines = [f"{k}: {trunc(v, 120)}" for k, v in row.items()]
        fields.append(
            {
                "name": f"row {i + 1}",
                "value": ("\n".join(lines)[:1024] or "—"),
                "inline": False,
            }
        )
    return {"title": "Scan Watermarks", "color": COLOR_BLURPLE, "fields": fields}


def cmd_help() -> dict:
    return {
        "title": "Quantkernal commands",
        "color": COLOR_GREYPLE,
        "fields": [
            {"name": name, "value": desc, "inline": False} for name, desc in COMMANDS_HELP
        ],
        "footer": {"text": "Read-only — answers come straight from the scan engine database."},
    }


def dispatch(name: str, options: dict, cfg: dict):
    """Return an embed dict for a command name, or None when unknown."""
    if name == "status":
        return cmd_status(cfg)
    if name == "alerts":
        return cmd_alerts(cfg, clamp_limit(options.get("limit", 5)))
    if name == "scans":
        return cmd_scans(cfg, clamp_limit(options.get("limit", 5)))
    if name == "audit":
        return cmd_audit(cfg, clamp_limit(options.get("limit", 5)))
    if name == "watermarks":
        return cmd_watermarks(cfg)
    if name == "help":
        return cmd_help()
    return None


# ---------------------------------------------------------------- routing

def route(method: str, path: str, headers: dict, body: bytes) -> tuple[int, dict]:
    """Pure request router — returns (http_status, json_payload)."""
    headers = {k.lower(): v for k, v in headers.items()}

    if method == "GET":
        return 200, {"ok": True}
    if method != "POST":
        return 405, {"error": "method not allowed"}

    cfg = load_config()
    if cfg is None:
        log.error("missing required env vars (DISCORD_PUBLIC_KEY/SUPABASE_URL/SUPABASE_SERVICE_KEY)")
        return 500, {"error": "server misconfigured"}

    sig = headers.get("x-signature-ed25519")
    ts = headers.get("x-signature-timestamp")
    if not sig or not ts or not verify_signature(cfg["public_key"], sig, ts, body):
        return 401, {"error": "invalid signature"}

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return 400, {"error": "invalid json"}

    itype = payload.get("type")
    if itype == 1:  # PING
        return 200, {"type": 1}
    if itype == 2:  # APPLICATION_COMMAND
        data = payload.get("data") or {}
        name = data.get("name", "")
        options = {o.get("name"): o.get("value") for o in (data.get("options") or [])}
        try:
            embed = dispatch(name, options, cfg)
        except Exception:
            log.exception("command /%s failed", name)
            embed = error_embed()
        if embed is None:
            return 200, {
                "type": 4,
                "data": {"content": "Unknown command — try /help.", "flags": 64},
            }
        return 200, {"type": 4, "data": {"embeds": [embed]}}
    return 400, {"error": "unsupported interaction type"}


# ---------------------------------------------------------------- Vercel entrypoint

class handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        status, payload = route("GET", self.path, dict(self.headers), b"")
        self._send_json(status, payload)

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            status, payload = route("POST", self.path, dict(self.headers), raw)
        except Exception:
            log.exception("unhandled error in interactions handler")
            status, payload = 500, {"error": "internal error"}
        self._send_json(status, payload)

    def log_message(self, fmt, *args) -> None:  # route through logging
        log.info("%s - %s", self.address_string(), fmt % args)
