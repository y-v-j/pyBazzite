#!/usr/bin/env python3
"""Inventory devices on your own LAN and nearby Bluetooth radios.

Merges bazzite_recon.py and bazzite_recon_02.py. The jeepney D-Bus dependency
is gone — bluetoothctl ships with BlueZ and is more reliable than hand-rolled
ObjectManager calls. Vendor lookup is now opt-in and cached, because it sends
every MAC address you find to a third-party API and is rate-limited to roughly
one request per second, so the old per-device calls mostly returned nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bzt import sysutil as su
from bzt import ui

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "bzt/oui.json"


def load_cache() -> dict[str, str]:
    try:
        return json.loads(CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(data: dict[str, str]) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


def lookup_vendors(macs: list[str], cache: dict[str, str]) -> dict[str, str]:
    """Resolve MAC prefixes to vendors, honouring the API's rate limit."""
    todo = []
    for mac in macs:
        prefix = mac.upper().replace(":", "").replace("-", "")[:6]
        if prefix and prefix not in cache:
            todo.append(prefix)
    todo = sorted(set(todo))
    if todo:
        ui.note(f"looking up {len(todo)} vendor prefix(es) — ~1s each")
    for prefix in todo:
        try:
            req = urllib.request.Request(
                f"https://api.macvendors.com/{prefix}",
                headers={"User-Agent": "bzt-recon/1.0"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                cache[prefix] = resp.read().decode("utf-8", "replace").strip()
        except Exception:
            cache[prefix] = "unknown"
        time.sleep(1.1)          # free tier allows about one call per second
    save_cache(cache)
    return cache


def vendor_of(mac: str, cache: dict[str, str]) -> str:
    prefix = mac.upper().replace(":", "").replace("-", "")[:6]
    return cache.get(prefix, "not looked up")


def local_subnet() -> tuple[str | None, str | None]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
        octets = ip.split(".")
        return ip, ".".join(octets[:3]) + "."
    except OSError:
        return None, None


def ping(ip: str) -> None:
    subprocess.run(["ping", "-c", "1", "-W", "1", ip],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def scan_lan(sweep: bool) -> list[dict]:
    local_ip, base = local_subnet()
    if not base:
        ui.warn("no active network interface found")
        return []

    if sweep:
        ui.note(f"pinging {base}0/24 to populate the ARP table...")
        targets = [f"{base}{i}" for i in range(1, 255) if f"{base}{i}" != local_ip]
        with ThreadPoolExecutor(max_workers=64) as pool:
            list(pool.map(ping, targets))
    else:
        ui.note("reading the existing ARP table (use --sweep to probe actively)")

    res = su.run(["ip", "neigh", "show"], timeout=30)
    devices = []
    for line in res.out.splitlines():
        if "lladdr" not in line or any(s in line for s in ("FAILED", "INCOMPLETE")):
            continue
        parts = line.split()
        ip = parts[0]
        mac = parts[parts.index("lladdr") + 1].upper()
        state = parts[-1]
        try:
            hostname = socket.gethostbyaddr(ip)[0]
        except OSError:
            hostname = "—"
        devices.append({"kind": "LAN", "id": ip, "mac": mac,
                        "name": hostname, "state": state})
    return devices


def scan_bluetooth(seconds: int) -> list[dict]:
    if not su.has("bluetoothctl"):
        ui.warn("bluetoothctl not found — skipping Bluetooth")
        return []

    su.run(["bluetoothctl", "power", "on"], timeout=20)
    connected = set()
    res = su.run(["bluetoothctl", "devices", "Connected"], timeout=20)
    for line in res.out.splitlines():
        parts = line.split(" ", 2)
        if len(parts) >= 2:
            connected.add(parts[1].upper())

    paired = set()
    res = su.run(["bluetoothctl", "devices", "Paired"], timeout=20)
    for line in res.out.splitlines():
        parts = line.split(" ", 2)
        if len(parts) >= 2:
            paired.add(parts[1].upper())

    if seconds > 0:
        ui.note(f"scanning for nearby Bluetooth devices ({seconds}s)...")
        proc = subprocess.Popen(["bluetoothctl", "scan", "on"],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            time.sleep(seconds)
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            su.run(["bluetoothctl", "scan", "off"], timeout=15)

    devices = []
    res = su.run(["bluetoothctl", "devices"], timeout=20)
    for line in res.out.splitlines():
        parts = line.split(" ", 2)
        if len(parts) < 3:
            continue
        mac, name = parts[1].upper(), parts[2].strip()
        if mac in connected:
            state = "connected"
        elif mac in paired:
            state = "paired"
        else:
            state = "in range"
        devices.append({"kind": "Bluetooth", "id": state, "mac": mac,
                        "name": name, "state": state})
    return devices


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sweep", action="store_true",
                    help="actively ping every address on your subnet")
    ap.add_argument("--bt-seconds", type=int, default=0,
                    help="seconds to scan for Bluetooth devices (0 = cached only)")
    ap.add_argument("--vendors", action="store_true",
                    help="resolve hardware vendors (sends MAC prefixes to "
                         "api.macvendors.com)")
    ap.add_argument("--no-bluetooth", action="store_true")
    args = ap.parse_args()

    print(f"{ui.BOLD}{ui.CYAN}Local network and radio inventory{ui.RESET}")

    devices = scan_lan(args.sweep)
    if not args.no_bluetooth:
        devices += scan_bluetooth(args.bt_seconds)

    cache = load_cache()
    if args.vendors and devices:
        cache = lookup_vendors([d["mac"] for d in devices], cache)

    ui.header("Discovered devices")
    table = ui.Table(["Interface", "Address / State", "MAC", "Name", "Vendor"])
    colours = {"connected": ui.GREEN, "paired": ui.BLUE, "in range": ui.CYAN}
    for dev in devices:
        colour = colours.get(dev["state"], ui.GREEN if dev["kind"] == "LAN" else ui.GREY)
        table.add([dev["kind"], dev["id"], dev["mac"], dev["name"],
                   vendor_of(dev["mac"], cache)], colour)
    table.show("No devices found.")

    lan = sum(1 for d in devices if d["kind"] == "LAN")
    bt = len(devices) - lan
    print(f"\n  {ui.GREEN}LAN devices: {lan}{ui.RESET}   "
          f"{ui.CYAN}Bluetooth devices: {bt}{ui.RESET}")
    if not args.vendors:
        print(f"{ui.GREY}  (add --vendors to identify hardware makers){ui.RESET}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
