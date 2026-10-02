#!/usr/bin/env python3
"""Watch the air for deauthentication attacks and rogue access points.

Rewrite of bazzite_wifi_guard.py. That version imported scapy unguarded, never
checked that the interface was actually in monitor mode — on a normal managed
interface it silently sees no 802.11 frames at all and looks like a quiet
network — and printed every beacon, flooding the terminal within seconds.

This monitors your own network. Put the adapter in monitor mode first:
    sudo ip link set wlan0 down
    sudo iw dev wlan0 set type monitor
    sudo ip link set wlan0 up
and restore it afterwards with `set type managed`.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

try:
    from scapy.all import Dot11, Dot11Beacon, Dot11Deauth, Dot11Elt, sniff
except ImportError:
    print("\033[91m[-] scapy is required: pip install --user scapy\033[0m",
          file=sys.stderr)
    sys.exit(1)

from bzt import ui

ARPHRD_IEEE80211_RADIOTAP = 803


def in_monitor_mode(iface: str) -> bool:
    """A managed interface never surfaces 802.11 management frames."""
    try:
        return int(Path(f"/sys/class/net/{iface}/type").read_text().strip()) \
            == ARPHRD_IEEE80211_RADIOTAP
    except (OSError, ValueError):
        return False


def interface_exists(iface: str) -> bool:
    return Path(f"/sys/class/net/{iface}").exists()


class Guard:
    def __init__(self, table: ui.StreamTable, quiet: bool):
        self.table = table
        self.quiet = quiet
        self.ssid_bssids: dict[str, set[str]] = defaultdict(set)
        self.seen_aps: set[str] = set()
        self.deauth_counts: dict[str, int] = defaultdict(int)
        self.last_deauth_report: dict[str, float] = {}
        self.alerts = 0

    def stamp(self) -> str:
        return time.strftime("%H:%M:%S")

    def handle(self, pkt) -> None:
        if pkt.haslayer(Dot11Deauth):
            self.on_deauth(pkt)
        elif pkt.haslayer(Dot11Beacon):
            self.on_beacon(pkt)

    def on_deauth(self, pkt) -> None:
        src = pkt[Dot11].addr2 or "?"
        dst = pkt[Dot11].addr1 or "?"
        key = f"{src}->{dst}"
        self.deauth_counts[key] += 1
        # Deauth floods arrive in bursts; report once per second per pair
        # rather than printing thousands of identical rows.
        now = time.time()
        if now - self.last_deauth_report.get(key, 0) < 1.0:
            return
        self.last_deauth_report[key] = now
        self.alerts += 1
        self.table.row([self.stamp(), "DEAUTH",
                        f"{src} -> {dst}  (x{self.deauth_counts[key]}) "
                        f"— someone is forcing a disconnect"], ui.RED)

    def on_beacon(self, pkt) -> None:
        bssid = pkt[Dot11].addr3
        if not bssid:
            return
        try:
            ssid = pkt[Dot11Elt].info.decode(errors="ignore") or "<hidden>"
        except Exception:
            ssid = "<hidden>"

        self.ssid_bssids[ssid].add(bssid)
        # Two different radios claiming one network name is the signature of
        # an evil-twin / rogue AP (legitimate mesh setups also do this).
        if len(self.ssid_bssids[ssid]) > 1 and bssid not in self.seen_aps:
            self.alerts += 1
            others = ", ".join(sorted(self.ssid_bssids[ssid] - {bssid}))
            self.table.row([self.stamp(), "ROGUE AP?",
                            f"'{ssid}' now broadcast by {bssid} as well as "
                            f"{others}"], ui.YELLOW)
        elif bssid not in self.seen_aps and not self.quiet:
            self.table.row([self.stamp(), "AP seen",
                            f"'{ssid}'  {bssid}"], ui.GREY)
        self.seen_aps.add(bssid)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("interface", help="wireless interface in monitor mode")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="only show alerts, not every access point discovered")
    ap.add_argument("-t", "--timeout", type=int, default=0,
                    help="stop after N seconds (0 = run until Ctrl+C)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        ui.fail("root is required to capture raw 802.11 frames")
        ui.note(f"try: sudo {sys.argv[0]} {args.interface}")
        return 1
    if not interface_exists(args.interface):
        ui.fail(f"no such interface: {args.interface}")
        return 1
    if not in_monitor_mode(args.interface):
        ui.fail(f"{args.interface} is not in monitor mode — it will not see "
                f"management frames")
        ui.note(f"sudo ip link set {args.interface} down")
        ui.note(f"sudo iw dev {args.interface} set type monitor")
        ui.note(f"sudo ip link set {args.interface} up")
        return 1

    ui.header(f"Wi-Fi guard on {args.interface}")
    print(f"{ui.GREY}watching for deauth frames and rogue access points"
          f"{'  (Ctrl+C to stop)' if not args.timeout else ''}{ui.RESET}\n")

    table = ui.StreamTable(["Time", "Event", "Detail"], [8, 10, 60])
    table.start()
    guard = Guard(table, args.quiet)

    try:
        sniff(iface=args.interface, prn=guard.handle, store=0,
              timeout=args.timeout or None)
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        table.end()
        ui.fail(f"capture failed: {exc}")
        return 1
    finally:
        table.end()

    ui.header("Summary")
    summary = ui.Table(["Metric", "Value"])
    summary.add(["Access points seen", len(guard.seen_aps)], ui.CYAN)
    summary.add(["Network names", len(guard.ssid_bssids)], ui.CYAN)
    summary.add(["Deauth frames", sum(guard.deauth_counts.values())],
                ui.RED if guard.deauth_counts else ui.GREEN)
    summary.add(["Alerts raised", guard.alerts],
                ui.RED if guard.alerts else ui.GREEN)
    summary.show()

    duplicated = {s: b for s, b in guard.ssid_bssids.items() if len(b) > 1}
    if duplicated:
        ui.warn("network names broadcast by more than one radio:")
        for ssid, bssids in duplicated.items():
            print(f"    '{ssid}': {', '.join(sorted(bssids))}")
        print(f"{ui.GREY}    Mesh and extender setups do this legitimately. "
              f"Investigate if you do not run one.{ui.RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
