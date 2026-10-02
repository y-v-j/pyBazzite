#!/usr/bin/env python3
"""Observation-only Bluetooth presence watcher with periodic reporting.

Listens for the advertisements nearby devices already broadcast and records
what it sees. It never connects, pairs, trusts or probes anything: the only
commands issued are scan on/off and local queries against bluetoothd's own
cache, so nothing on the air is altered by running this.

LE mode (the default) is genuinely passive — Bluetooth Low Energy devices
advertise continuously and the adapter only listens. Classic BR/EDR discovery
(--classic) transmits inquiry packets, so it is opt-in.

Devices are keyed by MAC. Modern phones rotate their addresses for privacy,
so one handset may appear as several short-lived entries; that is expected.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from bzt import sysutil as su
from bzt import ui

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME",
                                Path.home() / ".local/state")) / "bzt"
DB_FILE = STATE_DIR / "bluetooth_presence.json"
PID_FILE = STATE_DIR / "btwatch.pid"

RSSI_RE = re.compile(r"RSSI:\s*(-?\d+)")
NAME_RE = re.compile(r"(?:Name|Alias):\s*(.+)")
CLASS_RE = re.compile(r"Icon:\s*(.+)")
MAC_RE = re.compile(r"^([0-9A-F]{2}(?::[0-9A-F]{2}){5})$", re.I)


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_db() -> dict:
    try:
        return json.loads(DB_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"devices": {}, "scans": 0, "last_report": None}


def adapter_ready() -> bool:
    res = su.run(["bluetoothctl", "list"], timeout=15)
    return res.ok and "Controller" in res.out


def device_details(mac: str) -> dict:
    """Query bluetoothd's cached record. Local only — no radio traffic."""
    res = su.run(["bluetoothctl", "info", mac], timeout=15)
    info = {"rssi": None, "name": "", "icon": ""}
    if not res.ok:
        return info
    for line in res.out.splitlines():
        line = line.strip()
        if (m := RSSI_RE.search(line)):
            info["rssi"] = int(m.group(1))
        elif line.startswith("Alias:") and (m := NAME_RE.search(line)):
            info["name"] = m.group(1).strip()
        elif (m := CLASS_RE.search(line)):
            info["icon"] = m.group(1).strip()
    return info


def scan_once(seconds: int, classic: bool) -> list[dict]:
    """Run one listening window and return what was observed."""
    if not su.has("bluetoothctl"):
        ui.fail("bluetoothctl not found (package: bluez)")
        return []
    if not adapter_ready():
        ui.fail("no Bluetooth controller available")
        return []

    su.run(["bluetoothctl", "power", "on"], timeout=20)
    mode = "on" if classic else "le"
    proc = subprocess.Popen(["bluetoothctl", "--timeout", str(seconds),
                             "scan", mode],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proc.wait(timeout=seconds + 15)
    except subprocess.TimeoutExpired:
        proc.terminate()
    finally:
        su.run(["bluetoothctl", "scan", "off"], timeout=15)

    seen = []
    res = su.run(["bluetoothctl", "devices"], timeout=20)
    for line in res.out.splitlines():
        parts = line.split(" ", 2)
        if len(parts) < 2 or not MAC_RE.match(parts[1]):
            continue
        mac = parts[1].upper()
        name = parts[2].strip() if len(parts) > 2 else ""
        detail = device_details(mac)
        seen.append({"mac": mac,
                     "name": detail["name"] or name or "(unnamed)",
                     "rssi": detail["rssi"],
                     "icon": detail["icon"]})
    return seen


def record(db: dict, seen: list[dict]) -> tuple[int, int]:
    stamp = now()
    devices = db.setdefault("devices", {})
    new = 0
    for dev in seen:
        entry = devices.get(dev["mac"])
        if entry is None:
            new += 1
            entry = {"first_seen": stamp, "sightings": 0,
                     "rssi_best": None, "name": dev["name"], "icon": dev["icon"]}
            devices[dev["mac"]] = entry
        entry["last_seen"] = stamp
        entry["sightings"] = entry.get("sightings", 0) + 1
        if dev["name"] and dev["name"] != "(unnamed)":
            entry["name"] = dev["name"]
        if dev["icon"]:
            entry["icon"] = dev["icon"]
        entry["rssi_last"] = dev["rssi"]
        if dev["rssi"] is not None:
            best = entry.get("rssi_best")
            entry["rssi_best"] = dev["rssi"] if best is None else max(best, dev["rssi"])
    db["scans"] = db.get("scans", 0) + 1
    db["last_scan"] = stamp
    return new, len(seen)


def compact(stamp: str | None) -> str:
    """Shorten '2026-08-19 13:00:21' to '08-19 13:00' so columns stay narrow."""
    if not stamp or len(stamp) < 16:
        return stamp or "?"
    return stamp[5:16]


def proximity(rssi) -> tuple[str, str]:
    if rssi is None:
        return "unknown", ui.GREY
    if rssi >= -60:
        return "very close", ui.RED
    if rssi >= -75:
        return "nearby", ui.YELLOW
    return "distant", ui.GREEN


def report(db: dict, since_hours: float | None = None) -> None:
    devices = db.get("devices", {})
    ui.header(f"Bluetooth presence report  ({db.get('scans', 0)} scans, "
              f"last {db.get('last_scan', 'never')})")

    if not devices:
        ui.note("nothing observed yet — run a scan first")
        return

    cutoff = None
    if since_hours:
        cutoff = datetime.now() - timedelta(hours=since_hours)

    rows = []
    for mac, entry in devices.items():
        try:
            last = datetime.strptime(entry.get("last_seen", ""), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            last = None
        if cutoff and last and last < cutoff:
            continue
        rows.append((mac, entry, last))
    rows.sort(key=lambda r: r[1].get("sightings", 0), reverse=True)

    table = ui.Table(["MAC", "Name", "Type", "Seen", "Signal",
                      "Proximity", "First seen", "Last seen"])
    for mac, entry, _last in rows:
        label, colour = proximity(entry.get("rssi_last"))
        rssi = entry.get("rssi_last")
        table.add([mac, entry.get("name", "?"), entry.get("icon") or "—",
                   entry.get("sightings", 0),
                   f"{rssi} dBm" if rssi is not None else "—",
                   label, compact(entry.get("first_seen")),
                   compact(entry.get("last_seen"))],
                  colour)
    table.show("Nothing seen in this window.")

    total_scans = max(1, db.get("scans", 1))
    regulars = [e for _m, e, _l in rows if e.get("sightings", 0) >= total_scans * 0.5]
    onetime = [e for _m, e, _l in rows if e.get("sightings", 0) == 1]

    ui.header("Summary")
    summary = ui.Table(["Category", "Count", "Meaning"])
    summary.add(["Total devices", len(rows), "distinct addresses observed"], ui.CYAN)
    summary.add(["Regulars", len(regulars),
                 "seen in at least half of all scans — likely resident"], ui.GREEN)
    summary.add(["One-off sightings", len(onetime),
                 "seen once — passers-by or rotating addresses"], ui.YELLOW)
    summary.show()


def running_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        return None


def loop(interval_hours: float, seconds: int, classic: bool) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("flag", True))
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("flag", True))

    try:
        while not stop["flag"]:
            db = load_db()
            record(db, scan_once(seconds, classic))
            su.write_json(DB_FILE, db)
            deadline = time.monotonic() + interval_hours * 3600
            while not stop["flag"] and time.monotonic() < deadline:
                time.sleep(1)
    finally:
        PID_FILE.unlink(missing_ok=True)


TIMER_UNIT = """[Unit]
Description=Bazzite Bluetooth presence scan

[Service]
Type=oneshot
ExecStart={python} {script} --once --seconds {seconds}{classic}
"""

TIMER_TIMER = """[Unit]
Description=Run the Bluetooth presence scan every {hours} hours

[Timer]
OnBootSec=5min
OnUnitActiveSec={hours}h
Persistent=true

[Install]
WantedBy=timers.target
"""


def install_timer(hours: float, seconds: int, classic: bool) -> int:
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    (unit_dir / "bzt-btwatch.service").write_text(TIMER_UNIT.format(
        python=sys.executable, script=script, seconds=seconds,
        classic=" --classic" if classic else ""))
    (unit_dir / "bzt-btwatch.timer").write_text(
        TIMER_TIMER.format(hours=hours))

    ui.ok(f"wrote {unit_dir}/bzt-btwatch.service and .timer")
    su.run(["systemctl", "--user", "daemon-reload"], timeout=30)
    res = su.run(["systemctl", "--user", "enable", "--now", "bzt-btwatch.timer"],
                 timeout=30)
    if res.ok:
        ui.ok(f"timer enabled — scanning every {hours}h")
        print(f"  {ui.GREY}status:  systemctl --user list-timers bzt-btwatch.timer")
        print(f"  report:  {script} --report")
        print(f"  stop:    systemctl --user disable --now bzt-btwatch.timer{ui.RESET}")
    else:
        ui.fail(f"could not enable timer: {res.err or res.out}")
    return 0 if res.ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run one scan and exit")
    ap.add_argument("--report", action="store_true", help="print the report and exit")
    ap.add_argument("--start", action="store_true",
                    help="run continuously in the background")
    ap.add_argument("--stop", action="store_true", help="stop the background watcher")
    ap.add_argument("--status", action="store_true", help="is the watcher running?")
    ap.add_argument("--install-timer", action="store_true",
                    help="install a systemd user timer (recommended over --start)")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="hours between scans (default: 2)")
    ap.add_argument("--seconds", type=int, default=30,
                    help="length of each listening window (default: 30)")
    ap.add_argument("--classic", action="store_true",
                    help="also run BR/EDR inquiry (transmits; not passive)")
    ap.add_argument("--since", type=float, default=None,
                    help="report only devices seen in the last N hours")
    args = ap.parse_args()

    if args.status:
        pid = running_pid()
        ui.ok(f"watcher running (pid {pid})") if pid else ui.note("watcher not running")
        return 0

    if args.stop:
        pid = running_pid()
        if not pid:
            ui.note("watcher not running")
            return 0
        os.kill(pid, signal.SIGTERM)
        ui.ok(f"stopped pid {pid}")
        return 0

    if args.install_timer:
        return install_timer(args.interval, args.seconds, args.classic)

    if args.report:
        report(load_db(), args.since)
        return 0

    if args.start:
        if running_pid():
            ui.warn("watcher already running")
            return 1
        print(f"[+] watching every {args.interval}h, {args.seconds}s per scan")
        su.daemonize()
        loop(args.interval, args.seconds, args.classic)
        return 0

    # Default: one scan, then show the report.
    mode = "classic + LE" if args.classic else "LE (passive)"
    ui.note(f"listening for {args.seconds}s in {mode} mode — not connecting to anything")
    db = load_db()
    new, total = record(db, scan_once(args.seconds, args.classic))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    su.write_json(DB_FILE, db)
    ui.ok(f"observed {total} device(s), {new} new")
    if not args.once:
        report(db, args.since)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
