#!/usr/bin/env python3
"""Inventory physical hardware and report which kernel driver owns each device.

Merges the near-identical bazzite_hardware.py and bazzite_hardware_02.py into
one tool, and fixes the lspci fallback parser that mangled device names.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from bzt import sysutil as su
from bzt import ui

# "00:02.0 VGA compatible controller [0300]: Intel Corp. UHD [8086:9a49] (rev 01)"
LSPCI_RE = re.compile(
    r"^(?P<slot>\S+)\s+(?P<cls>.+?)\s*\[(?P<clsid>[0-9a-fA-F]{4})\]:\s*(?P<name>.+)$")

GPU_TERMS = ("vga", "3d controller", "display controller")
AV_TERMS = ("audio", "multimedia", "sound")


def read(path) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def os_profile() -> dict[str, str]:
    info: dict[str, str] = {}
    for line in (read("/etc/os-release") or "").splitlines():
        key, _, val = line.partition("=")
        val = val.strip('"')
        if key == "NAME":
            info["OS Name"] = val
        elif key == "VERSION":
            info["Version"] = val
        elif key == "VARIANT":
            info["Variant"] = val
    info.setdefault("OS Name", "Linux")
    info.setdefault("Version", "unknown")

    desktop = (os.environ.get("XDG_CURRENT_DESKTOP")
               or os.environ.get("DESKTOP_SESSION") or "unknown")
    upper = desktop.upper()
    for token, label in (("KDE", "KDE Plasma"), ("GNOME", "GNOME"),
                         ("XFCE", "XFCE"), ("SWAY", "Sway")):
        if token in upper:
            desktop = label
            break
    info["Desktop"] = desktop
    info["Session"] = (os.environ.get("XDG_SESSION_TYPE") or "unknown").capitalize()
    info["Atomic (ostree)"] = "yes" if su.is_atomic() else "no"
    kernel = su.run(["uname", "-r"])
    info["Kernel"] = kernel.out if kernel.ok else "unknown"
    return info


def cpu() -> dict[str, str]:
    model = None
    for line in (read("/proc/cpuinfo") or "").splitlines():
        if "model name" in line:
            model = line.split(":", 1)[1].strip()
            break
    if not model:
        res = su.run(["lscpu"])
        for line in res.out.splitlines():
            if "Model name:" in line:
                model = line.split(":", 1)[1].strip()
                break
    cores = os.cpu_count() or "?"
    return {"Category": "Processor (CPU)",
            "Device": f"{model or 'Unknown CPU'} ({cores} threads)",
            "Driver": "kernel scheduler", "Status": "Active"}


def motherboard() -> dict[str, str]:
    vendor = read("/sys/class/dmi/id/board_vendor") or read("/sys/class/dmi/id/sys_vendor")
    name = read("/sys/class/dmi/id/board_name") or read("/sys/class/dmi/id/product_name")
    board = " ".join(p for p in (vendor, name) if p) or "Unknown board"
    return {"Category": "Motherboard", "Device": board,
            "Driver": "ACPI / DMI firmware", "Status": "Active"}


def pci_devices(show_all: bool) -> list[dict[str, str]]:
    if not su.has("lspci"):
        return []
    res = su.run(["lspci", "-nnk"])
    if not res.ok:
        return []

    # lspci -nnk separates devices by indentation, not blank lines: each
    # device header starts at column 0 and its details are tab-indented.
    blocks: list[list[str]] = []
    for line in res.out.splitlines():
        if line and not line[0].isspace():
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)

    out = []
    for lines in blocks:
        if not lines:
            continue
        match = LSPCI_RE.match(lines[0])
        if match:
            cls, name = match.group("cls").lower(), match.group("name").strip()
        else:
            # Fallback splits on the class terminator, not the first colon —
            # splitting on ':' alone chopped the PCI slot in half.
            head, _, tail = lines[0].partition("]: ")
            cls, name = head.lower(), (tail or lines[0]).strip()

        if any(t in cls for t in GPU_TERMS):
            category = "Graphics (GPU)"
        elif any(t in cls for t in AV_TERMS):
            category = "Audio / Multimedia"
        elif "network" in cls or "ethernet" in cls:
            category = "Network Controller"
        elif show_all:
            category = "PCI Device"
        else:
            continue

        driver, status = "No driver bound", "Missing driver"
        for line in lines[1:]:
            stripped = line.strip()
            if stripped.startswith("Kernel driver in use:"):
                driver = stripped.split(":", 1)[1].strip()
                status = "Active"
                break
            if stripped.startswith("Kernel modules:"):
                driver = f"available: {stripped.split(':', 1)[1].strip()}"
                status = "Unbound"
        out.append({"Category": category, "Device": name,
                    "Driver": driver, "Status": status})
    return out


def displays() -> list[dict[str, str]]:
    found = []
    drm = Path("/sys/class/drm")
    if not drm.is_dir():
        return found
    for entry in sorted(drm.iterdir()):
        if "-" not in entry.name or not entry.name.startswith("card"):
            continue
        if read(entry / "status") != "connected":
            continue
        modes = read(entry / "modes")
        best = modes.splitlines()[0] if modes else "mode unknown"
        found.append({"Category": "Display",
                      "Device": f"{entry.name.split('-', 1)[1]} ({best})",
                      "Driver": "DRM/KMS", "Status": "Active"})
    return found


def usb_devices() -> list[dict[str, str]]:
    if not su.has("lsusb"):
        return []
    res = su.run(["lsusb"])
    out = []
    for line in res.out.splitlines():
        _, _, desc = line.partition(": ")
        name = re.sub(r"^ID\s+[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\s*", "", desc).strip()
        if not name or "root hub" in name.lower():
            continue
        out.append({"Category": "USB Device", "Device": name,
                    "Driver": "usbcore (autoloaded)", "Status": "Active"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-a", "--all", action="store_true",
                    help="include every PCI device, not just GPU/audio/network")
    args = ap.parse_args()

    ui.header("Operating system profile")
    profile = ui.Table(["Property", "Value"])
    for key, val in os_profile().items():
        profile.add([key, val])
    profile.show()

    devices = [cpu(), motherboard()]
    devices += pci_devices(args.all)
    devices += displays()
    devices += usb_devices()

    ui.header("Hardware and driver bindings")
    table = ui.Table(["Category", "Device", "Kernel Driver", "Status"])
    for dev in devices:
        colour = {"Active": ui.GREEN, "Unbound": ui.YELLOW}.get(dev["Status"], ui.RED)
        table.add([dev["Category"], dev["Device"], dev["Driver"], dev["Status"]], colour)
    table.show()

    unbound = [d for d in devices if d["Status"] != "Active"]
    if unbound:
        ui.warn(f"{len(unbound)} device(s) without an active driver:")
        for dev in unbound:
            print(f"    - {dev['Category']}: {dev['Device']}")
    else:
        ui.ok("Every detected device has an active kernel driver.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
