#!/usr/bin/env python3
"""Watch the journal live for sudo, SSH and desktop login events.

Rewrite of bazzite_auth_monitor.py. The original combined `-u systemd-logind`
with `-t sudo`: journalctl ANDs matches that target different fields, so that
filter could never match a single line and the monitor stayed silent forever.
This version matches only on SYSLOG_IDENTIFIER, which journalctl ORs.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

from bzt import sysutil as su
from bzt import ui

IDENTIFIERS = ["sudo", "sshd", "gdm-password", "systemd-logind", "polkitd", "su"]

RULES = (
    ("SUDO FAILED", re.compile(r"sudo:.*(authentication failure|auth failure|"
                               r"incorrect password|NOT in the sudoers)", re.I), ui.RED),
    ("SUDO USED", re.compile(r"sudo:.*COMMAND="), ui.YELLOW),
    ("SSH FAILED", re.compile(r"Failed (password|publickey) for", re.I), ui.RED),
    ("SSH OK", re.compile(r"Accepted (password|publickey) for", re.I), ui.GREEN),
    ("LOGIN", re.compile(r"(session opened for|New session \d+ of user)", re.I), ui.GREEN),
    ("LOGOUT", re.compile(r"(session closed for|Removed session)", re.I), ui.GREY),
    ("AUTH PROMPT", re.compile(r"polkitd.*(Operator of|Registered Authentication)", re.I), ui.CYAN),
)

USER_RE = re.compile(r"(?:for user\s+|user=|for\s+|user\s+)([A-Za-z0-9._-]+)")
CMD_RE = re.compile(r"COMMAND=(.+)$")


def summarise(line: str, label: str) -> str:
    if label == "SUDO USED":
        who = USER_RE.search(line)
        cmd = CMD_RE.search(line)
        return (f"{who.group(1) if who else '?'} ran "
                f"{cmd.group(1) if cmd else 'a privileged command'}")
    who = USER_RE.search(line)
    if who:
        return f"user {who.group(1)}"
    return line.split(": ", 1)[-1] if ": " in line else line


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-n", "--lines", type=int, default=0,
                    help="show this many past events before following (default: 0)")
    ap.add_argument("--no-follow", action="store_true",
                    help="print the backlog and exit instead of watching")
    args = ap.parse_args()

    if not su.has("journalctl"):
        ui.fail("journalctl is required")
        return 1
    if not su.is_root():
        ui.warn("running unprivileged — you will only see your own events. "
                "Re-run with sudo for system-wide visibility.")

    cmd = ["journalctl", "-n", str(args.lines), "--no-pager"]
    for ident in IDENTIFIERS:
        cmd += ["-t", ident]
    if not args.no_follow:
        cmd.append("-f")

    ui.header("Authentication and privilege monitor")
    print(f"{ui.GREY}watching: {', '.join(IDENTIFIERS)}"
          f"{'' if args.no_follow else '  (Ctrl+C to stop)'}{ui.RESET}\n")

    table = ui.StreamTable(["Time", "Event", "Detail"], [8, 13, 56])
    table.start()
    counts: dict[str, int] = {}

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            for label, pattern, colour in RULES:
                if pattern.search(line):
                    fields = line.split()
                    stamp = fields[2][:8] if len(fields) > 2 and ":" in fields[2] else ""
                    table.row([stamp, label, summarise(line, label)], colour)
                    counts[label] = counts.get(label, 0) + 1
                    break
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        table.end()

    if counts:
        ui.header("Session summary")
        summary = ui.Table(["Event type", "Count"])
        for label, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            colour = next((c for lbl, _p, c in RULES if lbl == label), "")
            summary.add([label, count], colour)
        summary.show()
    else:
        ui.note("No authentication events seen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
