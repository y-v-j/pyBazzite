#!/usr/bin/env python3
"""Report installed environments, Flatpaks, network profiles and deployments.

Rewrite of bazzite_details.py. Deployment state now comes from
`rpm-ostree status --json` instead of scraping bullet characters out of the
human-readable output, which broke on any non-UTF-8 terminal.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from bzt import sysutil as su
from bzt import ui

# nmcli terse output escapes a literal ':' inside a value as '\:'.
UNESCAPED_COLON = re.compile(r"(?<!\\):")


def split_terse(line: str) -> list[str]:
    return [f.replace("\\:", ":") for f in UNESCAPED_COLON.split(line)]


def conda_section(show_packages: bool) -> None:
    ui.header("Conda environments")
    if not su.has("conda"):
        ui.note("conda is not on PATH — skipping")
        return

    res = su.run(["conda", "env", "list", "--json"], timeout=60)
    if not res.ok:
        ui.warn(f"could not list environments: {res.err or res.rc}")
        return
    try:
        envs = json.loads(res.out).get("envs", [])
    except json.JSONDecodeError:
        ui.warn("could not parse conda output")
        return

    # The active environment is whatever CONDA_PREFIX points at. The old code
    # looked for a "conda_prefix" key that `conda env list` never emits.
    active = os.environ.get("CONDA_PREFIX", "")
    table = ui.Table(["", "Environment", "Packages", "Path"])
    counts: dict[str, list] = {}
    for env in envs:
        name = os.path.basename(env) or env
        # Query by prefix, not by name: path-only environments have no name.
        pkgs = su.run(["conda", "list", "-p", env, "--json"], timeout=120)
        entries = []
        if pkgs.ok:
            try:
                entries = json.loads(pkgs.out)
            except json.JSONDecodeError:
                entries = []
        counts[env] = entries
        table.add(["*" if env == active else "", name, str(len(entries)), env],
                  ui.GREEN if env == active else "")
    table.show("No conda environments found.")

    if not show_packages:
        print(f"{ui.GREY}  (re-run with --packages to list package contents){ui.RESET}")
        return

    for env, entries in counts.items():
        if not entries:
            continue
        ui.header(f"Packages in {os.path.basename(env)}")
        pkg_table = ui.Table(["Package", "Version", "Installer", "Channel"])
        for pkg in entries:
            channel = pkg.get("channel", "unknown")
            installer = "pip/uv" if channel in ("pypi", "<pip>") else "conda"
            pkg_table.add([pkg.get("name", "?"), pkg.get("version", "?"),
                           installer, channel])
        pkg_table.show()


def flatpak_section() -> None:
    ui.header("Flatpak applications")
    if not su.has("flatpak"):
        ui.note("flatpak is not installed — skipping")
        return
    res = su.run(["flatpak", "list",
                  "--columns=application,version,installation,size"], timeout=60)
    table = ui.Table(["Application ID", "Version", "Scope", "Size"])
    for line in res.out.splitlines():
        parts = line.split("\t")
        # A missing version yields an empty field rather than a dropped one,
        # so pad to the requested column count instead of guessing positions.
        parts += [""] * (4 - len(parts))
        app, version, scope, size = parts[:4]
        table.add([app, version or "n/a", scope or "n/a", size or "n/a"])
    table.show("No Flatpaks installed.")


def network_section() -> None:
    ui.header("Stored network connections")
    if not su.has("nmcli"):
        ui.note("nmcli is not available — skipping")
        return
    res = su.run(["nmcli", "-t", "-f", "NAME,UUID,TYPE,DEVICE",
                  "connection", "show"], timeout=30)
    table = ui.Table(["Connection", "Type", "Status", "UUID"])
    for line in res.out.splitlines():
        fields = split_terse(line)
        if len(fields) < 4:
            continue
        name, uuid, ctype, device = fields[0], fields[1], fields[2], fields[3]
        status = f"active on {device}" if device else "inactive"
        table.add([name, ctype, status, uuid], ui.GREEN if device else ui.GREY)
    table.show("No saved connections.")


def deployment_section() -> None:
    ui.header("rpm-ostree deployments")
    if not su.has("rpm-ostree"):
        ui.note("not an rpm-ostree system — skipping")
        return
    res = su.run(["rpm-ostree", "status", "--json"], timeout=60)
    if not res.ok:
        ui.warn(f"rpm-ostree status failed: {res.err or res.rc}")
        return
    try:
        deployments = json.loads(res.out).get("deployments", [])
    except json.JSONDecodeError:
        ui.warn("could not parse rpm-ostree JSON")
        return

    table = ui.Table(["Role", "Version", "Base checksum", "Pinned"])
    for dep in deployments:
        booted = dep.get("booted", False)
        role = "Booted" if booted else "Rollback / previous"
        checksum = (dep.get("checksum") or "")[:12]
        table.add([role, dep.get("version", "unknown"), checksum,
                   "yes" if dep.get("pinned") else "no"],
                  ui.GREEN if booted else ui.GREY)
    table.show("No deployments reported.")


def distrobox_section() -> None:
    ui.header("Distrobox containers")
    if not su.has("distrobox"):
        ui.note("distrobox is not installed — skipping")
        return
    res = su.run(["distrobox", "list", "--no-color"], timeout=60)
    table = ui.Table(["ID", "Name", "Status", "Image"])
    for line in res.out.splitlines()[1:]:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 4:
            table.add(parts[:4],
                      ui.GREEN if "Up" in parts[2] else ui.GREY)
    table.show("No containers found.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--packages", action="store_true",
                    help="also list every package inside each conda environment")
    ap.add_argument("--only", default="all",
                    help="comma-separated: conda,flatpak,network,deployments,distrobox")
    args = ap.parse_args()

    sections = {
        "conda": lambda: conda_section(args.packages),
        "flatpak": flatpak_section,
        "network": network_section,
        "deployments": deployment_section,
        "distrobox": distrobox_section,
    }
    wanted = list(sections) if args.only == "all" else [
        s.strip() for s in args.only.split(",") if s.strip()]

    print(f"{ui.BOLD}{ui.CYAN}Bazzite system inventory{ui.RESET}")
    for name in wanted:
        if name not in sections:
            ui.fail(f"unknown section: {name}")
            return 2
        sections[name]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
