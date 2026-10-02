#!/usr/bin/env python3
"""Background daemon that snapshots network connections to a JSON log.

Rewrite of bazzite_monitor_daemon.py with four correctness fixes: it detaches
properly (double fork + setsid), writes the log atomically so the viewer can
never read a half-written file, bounds the reverse-DNS cache, and gives DNS
lookups a timeout instead of blocking the poll loop indefinitely.
"""

from __future__ import annotations

import argparse
import errno
import os
import signal
import socket
import sys
import time
from pathlib import Path

try:
    import psutil
except ImportError:
    print("\033[91m[-] psutil is required: pip install --user psutil\033[0m",
          file=sys.stderr)
    sys.exit(1)

from bzt import netrisk
from bzt import sysutil as su
from bzt import ui

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME",
                                Path.home() / ".local/state")) / "bzt"
LOG_FILE = STATE_DIR / "network_audit.json"
PID_FILE = STATE_DIR / "daemon.pid"

DNS_CACHE: dict[str, str] = {}
DNS_CACHE_MAX = 512
DNS_TIMEOUT = 1.0


def resolve(ip: str) -> str:
    """Reverse-resolve *ip*, with a bounded cache and a hard timeout."""
    if ip in ("127.0.0.1", "::1", "0.0.0.0", "::"):
        return "localhost"
    if ip in DNS_CACHE:
        return DNS_CACHE[ip]
    # gethostbyaddr honours the global default timeout; without it a single
    # unreachable resolver stalls every poll cycle.
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(DNS_TIMEOUT)
    try:
        name = socket.gethostbyaddr(ip)[0]
    except (OSError, socket.herror, socket.gaierror):
        name = ip
    finally:
        socket.setdefaulttimeout(old)
    if len(DNS_CACHE) >= DNS_CACHE_MAX:
        DNS_CACHE.clear()
    DNS_CACHE[ip] = name
    return name


def snapshot(do_dns: bool) -> list[dict]:
    try:
        conns = psutil.net_connections(kind="inet")
    except Exception:
        return []

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    records = []
    for conn in conns:
        if conn.status not in ("ESTABLISHED", "LISTEN"):
            continue
        local_ip, local_port = conn.laddr
        remote_ip, remote_port, domain = "-", "-", "-"
        if conn.raddr:
            remote_ip, remote_port = conn.raddr
            domain = resolve(remote_ip) if do_dns else remote_ip

        app = "system/kernel"
        if conn.pid:
            try:
                app = psutil.Process(conn.pid).name()
            except Exception:
                app = "unknown"

        verdict, reason = netrisk.assess(conn.status, local_ip, local_port,
                                         remote_port, app)
        records.append({
            "timestamp": stamp, "app": app,
            "type": "listening" if conn.status == "LISTEN" else "outbound",
            "local": f"{local_ip}:{local_port}",
            "remote": "-" if remote_ip == "-" else f"{remote_ip}:{remote_port}",
            "url": domain, "security": verdict, "reason": reason,
        })
    return records


def running_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return None if exc.errno == errno.ESRCH else pid
    return pid


def loop(interval: int, do_dns: bool) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    stop = {"flag": False}

    def handler(_sig, _frm):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)

    try:
        while not stop["flag"]:
            try:
                su.write_json(LOG_FILE, snapshot(do_dns))
            except OSError:
                pass
            for _ in range(interval * 10):
                if stop["flag"]:
                    break
                time.sleep(0.1)
    finally:
        PID_FILE.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", action="store_true", help="detach and run in the background")
    ap.add_argument("--stop", action="store_true", help="stop a running daemon")
    ap.add_argument("--status", action="store_true", help="report daemon state")
    ap.add_argument("--once", action="store_true", help="write one snapshot and exit")
    ap.add_argument("-i", "--interval", type=int, default=15,
                    help="seconds between snapshots (default: 15)")
    ap.add_argument("--no-dns", action="store_true",
                    help="skip reverse DNS (faster, and leaks nothing to a resolver)")
    args = ap.parse_args()

    if args.status:
        pid = running_pid()
        if pid:
            ui.ok(f"daemon running (pid {pid}), log: {LOG_FILE}")
        else:
            ui.note("daemon is not running")
        return 0

    if args.stop:
        pid = running_pid()
        if not pid:
            ui.note("daemon is not running")
            return 0
        os.kill(pid, signal.SIGTERM)
        ui.ok(f"sent SIGTERM to pid {pid}")
        return 0

    if args.once:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        su.write_json(LOG_FILE, snapshot(not args.no_dns))
        ui.ok(f"snapshot written to {LOG_FILE}")
        return 0

    if args.start:
        if running_pid():
            ui.warn("daemon already running")
            return 1
        print(f"[+] starting daemon, log: {LOG_FILE}")
        su.daemonize()
        loop(args.interval, not args.no_dns)
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
