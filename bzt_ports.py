#!/usr/bin/env python3
"""Audit listening TCP/UDP sockets and how far each one is exposed.

Merges bazzite_port_recon.py and bazzite_port_recon_02.py. Loopback detection
now covers all of 127.0.0.0/8, so systemd-resolved on 127.0.0.53 is correctly
reported as local rather than as a LAN-facing service.
"""

from __future__ import annotations

import argparse
import ipaddress
import socket
import sys

from bzt import sysutil as su
from bzt import ui

# Ports whose names the system services database usually lacks.
FALLBACK_NAMES = {
    53: "DNS", 323: "chrony-NTP", 631: "CUPS printing", 1716: "KDE Connect",
    3000: "node/react dev", 5000: "flask/podman", 5353: "mDNS (Avahi)",
    5355: "LLMNR", 5432: "PostgreSQL", 6379: "Redis", 8000: "dev server",
    8080: "HTTP-alt", 27017: "MongoDB",
}


def service_name(port: int, proto: str) -> str:
    try:
        return socket.getservbyport(port, proto).upper()
    except OSError:
        return FALLBACK_NAMES.get(port, "unknown")


def classify(addr: str) -> tuple[str, str]:
    """Map a bind address to an exposure verdict and colour."""
    # ss appends a scope to link-local and some loopback binds ("127.0.0.53%lo").
    addr = addr.split("%", 1)[0]
    if addr in ("*", "0.0.0.0", "::"):
        return "EXPOSED (all interfaces)", ui.RED
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return "unknown", ui.YELLOW
    if ip.is_loopback:                      # covers 127.0.0.0/8 and ::1
        return "local only", ui.GREEN
    if ip.is_link_local:
        return "link-local", ui.YELLOW
    return "LAN interface", ui.YELLOW


def parse_sockets(with_process: bool) -> list[dict]:
    if not su.has("ss"):
        ui.fail("the 'ss' utility is required (package: iproute2)")
        sys.exit(1)

    # -p adds the owning process but only reveals other users' when root.
    flags = "-tulnpH" if with_process else "-tulnH"
    res = su.run(["ss", flags], timeout=30)
    if not res.ok:
        ui.fail(f"ss failed: {res.err}")
        sys.exit(1)

    seen, rows = set(), []
    for line in res.out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        proto = parts[0].lower()
        local = parts[4]

        if local.startswith("["):                      # [::1]:631
            addr, _, port_s = local.rpartition("]:")
            addr = addr.lstrip("[")
        elif ":" in local:
            addr, _, port_s = local.rpartition(":")
        else:
            addr, port_s = "*", local
        try:
            port = int(port_s)
        except ValueError:
            continue

        process = ""
        if with_process and len(parts) > 6:
            chunk = " ".join(parts[6:])
            if '"' in chunk:
                process = chunk.split('"')[1]

        key = (proto, port, addr)
        if key in seen:
            continue
        seen.add(key)
        exposure, colour = classify(addr)
        rows.append({"proto": proto.upper(), "port": port, "addr": addr,
                     "service": service_name(port, proto),
                     "process": process or "-",
                     "exposure": exposure, "colour": colour})
    return sorted(rows, key=lambda r: (r["proto"], r["port"]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-e", "--exposed-only", action="store_true",
                    help="show only sockets reachable from outside this machine")
    args = ap.parse_args()

    ui.header("Listening sockets")
    if not su.is_root():
        ui.note("running unprivileged — process names for other users are hidden")

    rows = parse_sockets(with_process=True)
    if args.exposed_only:
        rows = [r for r in rows if r["colour"] != ui.GREEN]

    table = ui.Table(["Proto", "Port", "Bind address", "Service",
                      "Process", "Exposure"])
    for row in rows:
        table.add([row["proto"], row["port"], row["addr"], row["service"],
                   row["process"], row["exposure"]], row["colour"])
    table.show("No listening sockets.")

    exposed = [r for r in rows if r["exposure"].startswith("EXPOSED")]
    print()
    if exposed:
        ui.warn(f"{len(exposed)} socket(s) listening on every interface:")
        for row in exposed:
            print(f"    - {row['proto']}/{row['port']} ({row['service']}) "
                  f"via {row['process']}")
        print(f"{ui.GREY}    Close one with: ./bzt_firewall.py --close "
              f"{exposed[0]['port']}/{exposed[0]['proto'].lower()}{ui.RESET}")
    else:
        ui.ok("Nothing is listening on a public interface.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
