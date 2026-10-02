#!/usr/bin/env python3
"""Audit live network connections and flag risky or unencrypted endpoints.

Rewrite of bazzite_firewall.py. Listening sockets are now assessed on the port
they expose, which the original never checked — it only ever looked at the
remote port, so a host listening on SMB/445 was reported as secure.
"""

from __future__ import annotations

import argparse
import sys

try:
    import psutil
except ImportError:
    print("\033[91m[-] psutil is required: pip install --user psutil\033[0m",
          file=sys.stderr)
    sys.exit(1)

from bzt import netrisk
from bzt import sysutil as su
from bzt import ui

COLOURS = {netrisk.SECURE: ui.GREEN,
           netrisk.WARNING: ui.YELLOW,
           netrisk.DANGER: ui.RED}


def process_name(pid) -> str:
    if not pid:
        return "system/kernel"
    try:
        return psutil.Process(pid).name()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return "unknown"


def collect() -> list[dict]:
    try:
        conns = psutil.net_connections(kind="inet")
    except psutil.AccessDenied:
        ui.fail("access denied reading the socket table")
        ui.note("re-run with sudo to inspect connections owned by other users")
        sys.exit(1)

    rows = []
    for conn in conns:
        if conn.status not in ("ESTABLISHED", "LISTEN"):
            continue
        local_ip, local_port = conn.laddr
        remote_ip, remote_port = ("-", "-")
        if conn.raddr:
            remote_ip, remote_port = conn.raddr

        app = process_name(conn.pid)
        verdict, reason = netrisk.assess(conn.status, local_ip, local_port,
                                         remote_port, app)
        rows.append({
            "app": app,
            "dir": "listening" if conn.status == "LISTEN" else "outbound",
            "local": f"{local_ip}:{local_port}",
            "remote": "-" if remote_ip == "-" else f"{remote_ip}:{remote_port}",
            "verdict": verdict,
            "reason": reason,
        })
    order = {netrisk.DANGER: 0, netrisk.WARNING: 1, netrisk.SECURE: 2}
    return sorted(rows, key=lambda r: (order[r["verdict"]], r["app"]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-r", "--risky-only", action="store_true",
                    help="hide connections judged secure")
    args = ap.parse_args()

    ui.header("Live connection audit")
    if not su.is_root():
        ui.note("running unprivileged — other users' sockets are not visible")

    rows = collect()
    if args.risky_only:
        rows = [r for r in rows if r["verdict"] != netrisk.SECURE]

    table = ui.Table(["Application", "Direction", "Local", "Remote",
                      "Verdict", "Assessment"])
    for row in rows:
        table.add([row["app"], row["dir"], row["local"], row["remote"],
                   row["verdict"], row["reason"]], COLOURS[row["verdict"]])
    table.show("No active connections.")

    counts = {v: sum(1 for r in rows if r["verdict"] == v)
              for v in (netrisk.DANGER, netrisk.WARNING, netrisk.SECURE)}
    print(f"\n  {ui.RED}danger {counts[netrisk.DANGER]}{ui.RESET}   "
          f"{ui.YELLOW}warning {counts[netrisk.WARNING]}{ui.RESET}   "
          f"{ui.GREEN}secure {counts[netrisk.SECURE]}{ui.RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
