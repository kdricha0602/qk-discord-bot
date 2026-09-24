#!/usr/bin/env python3
"""One-shot: register the 6 Quantkernal GLOBAL application commands via Discord REST.

Run locally (do NOT run in CI with a real token):
    DISCORD_APP_ID=... DISCORD_TOKEN=... python3 scripts/register_commands.py

PUTs to /applications/{app_id}/commands, replacing the global command set.
Global commands can take up to ~1 hour to appear in Discord clients.
After registering, set the Interactions Endpoint URL in the Discord developer
portal (General Information) to https://<project>.vercel.app/api/interactions
and click "Save Changes" — Discord verifies the endpoint with a PING.
"""

from __future__ import annotations

import json
import os
import sys

import httpx

LIMIT_OPTION = {
    "type": 4,  # INTEGER
    "name": "limit",
    "description": "How many rows to show (1-25, default 5)",
    "required": False,
    "min_value": 1,
    "max_value": 25,
}

COMMANDS = [
    {
        "name": "status",
        "description": "Scan engine health: heartbeats, leader tier, recent activity",
    },
    {
        "name": "alerts",
        "description": "Recent fail / error / failover events from the audit log",
        "options": [LIMIT_OPTION],
    },
    {
        "name": "scans",
        "description": "Recent scan runs and which tier ran them",
        "options": [LIMIT_OPTION],
    },
    {
        "name": "audit",
        "description": "Recent audit log entries, newest first",
        "options": [LIMIT_OPTION],
    },
    {
        "name": "watermarks",
        "description": "Current scan watermarks (resume state per scan)",
    },
    {
        "name": "help",
        "description": "List Quantkernal commands",
    },
]


def main() -> int:
    app_id = os.environ.get("DISCORD_APP_ID")
    token = os.environ.get("DISCORD_TOKEN")
    if not app_id or not token:
        print("error: set DISCORD_APP_ID and DISCORD_TOKEN in the environment",
              file=sys.stderr)
        return 2
    url = f"https://discord.com/api/v10/applications/{app_id}/commands"
    resp = httpx.put(
        url,
        headers={"Authorization": f"Bot {token}"},
        json=COMMANDS,
        timeout=30,
    )
    print(f"HTTP {resp.status_code}")
    try:
        print(json.dumps(resp.json(), indent=2)[:2000])
    except Exception:
        print(resp.text[:2000])
    resp.raise_for_status()
    print(f"registered {len(COMMANDS)} global commands")
    return 0


if __name__ == "__main__":
    sys.exit(main())
