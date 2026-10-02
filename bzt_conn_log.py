#!/usr/bin/env python3
"""Render the JSON log written by bzt_conn_daemon.py as a table."""

from __future__ import annotations

import argparse
import json
import sys

from bzt_conn_daemon import LOG_FILE
from bzt import netrisk
from bzt import ui

COLOURS = {netrisk.SECURE: ui.GREEN,
           netrisk.WARNING: ui.YELLOW,
           netrisk.DANGER: ui.RED}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-r", "--risky-only", action="store_true")
    args = ap.parse_args()

    if not LOG_FILE.exists():
        ui.fail(f"no log at {LOG_FILE}")
        ui.note("start the collector with: ./bzt_conn_daemon.py --start")
        return 1
    try:
        records = json.loads(LOG_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        ui.fail(f"could not read log: {exc}")
        return 1

    if args.risky_only:
        records = [r for r in records if r.get("security") != netrisk.SECURE]

    stamp = records[0].get("timestamp", "unknown") if records else "n/a"
    ui.header(f"Network audit log  (snapshot: {stamp})")

    table = ui.Table(["Application", "Dir", "Local", "Destination",
                      "Verdict", "Reason"])
    for rec in records:
        dest = rec.get("url") if rec.get("url") not in ("-", None) else rec.get("remote", "-")
        verdict = rec.get("security", netrisk.SECURE)
        table.add([rec.get("app", "?"), rec.get("type", "?"),
                   rec.get("local", "?"), dest, verdict, rec.get("reason", "")],
                  COLOURS.get(verdict, ui.GREY))
    table.show("No connections recorded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
