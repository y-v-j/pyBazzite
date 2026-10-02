#!/usr/bin/env python3
"""Report memory, storage and network health as colour-coded tables.

Rewrite of bazzite_speedtest.py with two corrections that mattered:
memory now uses the kernel's own MemAvailable figure instead of the
free+buffers+cached approximation (which overstates free RAM), and storage is
measured on the writable /var volume rather than on '/', which on an atomic
system is a read-only ostree deployment whose free space is meaningless.
"""

from __future__ import annotations

import argparse
import shutil
import socket
import sys
import time
from pathlib import Path

from bzt import sysutil as su
from bzt import ui

LATENCY_TARGETS = [("Cloudflare", "1.1.1.1", 443),
                   ("Google DNS", "8.8.8.8", 443),
                   ("Quad9", "9.9.9.9", 443)]


def usage_colour(percent: float) -> str:
    if percent >= 90:
        return ui.RED
    if percent >= 75:
        return ui.YELLOW
    return ui.GREEN


def meminfo() -> dict[str, int]:
    values = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            values[key.strip()] = int(rest.replace("kB", "").strip()) * 1024
    except (OSError, ValueError):
        pass
    return values


def memory_table() -> None:
    ui.header("Memory")
    info = meminfo()
    if not info:
        ui.fail("could not read /proc/meminfo")
        return

    total = info.get("MemTotal", 0)
    # MemAvailable is the kernel's own estimate of what a new workload can
    # claim; free+buffers+cached double-counts unreclaimable slab and overstates.
    available = info.get("MemAvailable", info.get("MemFree", 0))
    used = total - available
    percent = (used / total * 100) if total else 0

    swap_total = info.get("SwapTotal", 0)
    swap_used = swap_total - info.get("SwapFree", 0)
    swap_pct = (swap_used / swap_total * 100) if swap_total else 0

    table = ui.Table(["Resource", "Total", "Used", "Available", "Usage", "%"])
    table.add(["RAM", su.human(total), su.human(used), su.human(available),
               ui.bar(percent), f"{percent:.1f}%"], usage_colour(percent))
    if swap_total:
        table.add(["Swap / zram", su.human(swap_total), su.human(swap_used),
                   su.human(swap_total - swap_used), ui.bar(swap_pct),
                   f"{swap_pct:.1f}%"], usage_colour(swap_pct))
    table.show()


def storage_table() -> None:
    ui.header("Storage")
    mounts = ["/", "/var", "/boot", "/var/home"]
    if su.is_atomic():
        print(f"{ui.GREY}atomic system: '/' is a read-only ostree deployment; "
              f"{su.real_storage_mount()} is the volume that actually fills up"
              f"{ui.RESET}")

    table = ui.Table(["Mount", "Filesystem", "Total", "Used", "Free", "Usage", "%"])
    seen = set()
    for mount in mounts:
        path = Path(mount)
        if not path.is_dir():
            continue
        try:
            total, used, free = shutil.disk_usage(mount)
        except OSError:
            continue
        if (total, used) in seen:          # /var and /var/home share a volume
            continue
        seen.add((total, used))
        percent = (used / total * 100) if total else 0
        fstype = su.run(["findmnt", "-no", "FSTYPE", mount], timeout=10).out or "?"
        table.add([mount, fstype, su.human(total), su.human(used),
                   su.human(free), ui.bar(percent), f"{percent:.1f}%"],
                  usage_colour(percent))
    table.show()


def latency_table() -> None:
    ui.header("Network latency")
    table = ui.Table(["Target", "Address", "Round trip", "Status"])
    for name, host, port in LATENCY_TARGETS:
        start = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=3):
                pass
            elapsed = (time.perf_counter() - start) * 1000
            colour = ui.GREEN if elapsed < 50 else (
                ui.YELLOW if elapsed < 150 else ui.RED)
            table.add([name, host, f"{elapsed:.1f} ms", "reachable"], colour)
        except OSError as exc:
            table.add([name, host, ui.DASH, f"unreachable ({exc.__class__.__name__})"],
                      ui.RED)
    table.show()


def bandwidth_table() -> None:
    ui.header("Bandwidth")
    # The old `speedtest` PyPI module is unmaintained and frequently returns
    # HTTP 403, so prefer Ookla's own CLI and treat the module as a fallback.
    if su.has("speedtest"):
        ui.note("running Ookla speedtest (may take ~30s)...")
        res = su.run(["speedtest", "--format=human-readable", "--accept-license",
                      "--accept-gdpr"], timeout=180)
        print(res.out or res.err)
        return
    try:
        import speedtest as speedtest_mod
    except ImportError:
        ui.warn("no bandwidth tool available")
        ui.note("install one with:  rpm-ostree install speedtest-cli")
        ui.note("           or:  pip install --user speedtest-cli")
        return
    ui.note("running speedtest-cli (may take ~30s)...")
    try:
        test = speedtest_mod.Speedtest()
        test.get_best_server()
        down = test.download() / 1_000_000
        up = test.upload() / 1_000_000
        table = ui.Table(["Metric", "Result"])
        table.add(["Ping", f"{test.results.ping:.1f} ms"], ui.GREEN)
        table.add(["Download", f"{down:.2f} Mbps"], ui.GREEN)
        table.add(["Upload", f"{up:.2f} Mbps"], ui.GREEN)
        table.show()
    except Exception as exc:
        ui.fail(f"speedtest failed: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--speedtest", action="store_true",
                    help="also measure download/upload bandwidth (slow)")
    args = ap.parse_args()

    print(f"{ui.BOLD}{ui.CYAN}System resource report{ui.RESET}")
    memory_table()
    storage_table()
    latency_table()
    if args.speedtest:
        bandwidth_table()
    else:
        print(f"\n{ui.GREY}(add --speedtest to measure bandwidth){ui.RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
