#!/usr/bin/env python3
"""Check and tighten this machine's firewall, in plain English.

Run it with no arguments for a guided checkup: it explains what each opening
in your firewall is for, whether it matters, and offers to close the ones you
do not need. Nothing is changed without you saying yes.

Rewrite of bazzite_block_ports.py, which called `--remove-port` and announced
"blocked". That only withdraws an explicit *allow* rule: if the port was never
opened the command fails, and traffic arriving via an allowed *service* still
gets through. This tool tells the two apart and can add a real drop rule.
"""

from __future__ import annotations

import argparse
import sys

from bzt import sysutil as su
from bzt import ui

# Plain-English notes for the things people actually find on a desktop.
# risk: "fine" (leave it), "review" (depends on you), "risky" (close it).
SERVICE_INFO = {
    "ssh": ("Remote login", "Lets you log into this computer from another "
            "machine over the network. Close it unless you actually do that.",
            "review"),
    "mdns": ("Local device discovery", "How printers, speakers and file shares "
             "find each other on your home network. Handy at home, worth "
             "closing on public Wi-Fi.", "fine"),
    "dhcpv6-client": ("Automatic network setup", "Lets your router hand this "
                      "machine an IPv6 address. Normal; leave it on.", "fine"),
    "samba": ("Windows file sharing", "Shares your folders with Windows PCs. "
              "Historically a common target for malware.", "risky"),
    "samba-client": ("Windows file sharing (client)", "Lets you browse Windows "
                     "shares. Usually safe to close if you don't use them.",
                     "review"),
    "kdeconnect": ("Phone pairing (KDE Connect)", "Links your phone to your "
                   "desktop for notifications and file transfer. Only needed "
                   "if you use that feature.", "review"),
    "cockpit": ("Web admin panel", "A browser-based control panel for this "
                "machine. Close it unless you deliberately set it up.", "risky"),
    "http": ("Unencrypted web server", "Serves web pages from this machine "
             "without encryption.", "risky"),
    "https": ("Web server", "Serves web pages from this machine.", "review"),
}

PORT_INFO = {
    22: ("Remote login (SSH)", "Remote shell access to this machine.", "review"),
    80: ("Unencrypted web server", "Serves web pages without encryption.", "risky"),
    139: ("Windows file sharing", "Legacy Windows networking.", "risky"),
    445: ("Windows file sharing (SMB)", "A classic worm and ransomware target.",
          "risky"),
    631: ("Printing (CUPS)", "Printer sharing. Fine locally; rarely needed "
          "from the network.", "review"),
    1716: ("Phone pairing (KDE Connect)", "Links your phone to your desktop.",
           "review"),
    3389: ("Remote desktop (RDP)", "Full graphical remote control. Heavily "
           "brute-forced when exposed.", "risky"),
    5353: ("Local device discovery (mDNS)", "Finds printers and media devices "
           "on your network.", "fine"),
    5355: ("Legacy name lookup (LLMNR)", "An old Windows name-resolution "
           "protocol. Almost nobody needs it, and it can leak credentials.",
           "risky"),
}

RISK_COLOUR = {"fine": ui.GREEN, "review": ui.YELLOW, "risky": ui.RED}
RISK_LABEL = {"fine": "Normal", "review": "Your call", "risky": "Worth closing"}


def describe_service(name: str):
    return SERVICE_INFO.get(name, (name, "An allowed service. If you do not "
                                   "recognise the name, closing it is usually "
                                   "safe — you can undo it.", "review"))


def describe_port(port: int, proto: str):
    return PORT_INFO.get(port, (f"Port {port}/{proto}",
                                "Something is allowed through on this port. If "
                                "you do not recognise it, closing it is usually "
                                "safe — you can undo it.", "review"))


# --------------------------------------------------------------------------
def require_firewalld() -> None:
    if not su.has("firewall-cmd"):
        ui.fail("This system has no firewalld installed.")
        ui.note("Install it with: rpm-ostree install firewalld")
        sys.exit(1)
    res = su.run(["firewall-cmd", "--state"], timeout=15)
    if not res.ok:
        ui.fail("Your firewall is currently switched OFF.")
        ui.note("Turn it on with: sudo systemctl enable --now firewalld")
        sys.exit(1)


def default_zone() -> str:
    res = su.run(["firewall-cmd", "--get-default-zone"], timeout=15)
    return res.out.strip() if res.ok and res.out else "public"


def query(zone: str, what: str) -> list[str]:
    res = su.run(["firewall-cmd", f"--zone={zone}", f"--list-{what}"], timeout=15)
    return res.out.split() if res.ok else []


def openings(zone: str) -> list[dict]:
    """Everything currently allowed in, normalised into one list."""
    items = []
    for name in query(zone, "services"):
        title, why, risk = describe_service(name)
        items.append({"kind": "service", "id": name, "title": title,
                      "why": why, "risk": risk})
    for spec in query(zone, "ports"):
        port_s, _, proto = spec.partition("/")
        try:
            port = int(port_s)
        except ValueError:
            continue
        title, why, risk = describe_port(port, proto or "tcp")
        items.append({"kind": "port", "id": spec, "port": port,
                      "proto": proto or "tcp", "title": title,
                      "why": why, "risk": risk})
    order = {"risky": 0, "review": 1, "fine": 2}
    return sorted(items, key=lambda i: order[i["risk"]])


def ask(question: str, default_no: bool = True) -> bool:
    suffix = "[y/N]" if default_no else "[Y/n]"
    try:
        answer = input(f"{ui.BOLD}{question}{ui.RESET} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return not default_no
    return answer in ("y", "yes")


# --------------------------------------------------------------------------
def apply_close(zone: str, item: dict, apply: bool) -> bool:
    if item["kind"] == "service":
        cmd = su.sudo(["firewall-cmd", f"--zone={zone}",
                       f"--remove-service={item['id']}", "--permanent"])
        undo = f"sudo firewall-cmd --zone={zone} --add-service={item['id']} --permanent"
    else:
        cmd = su.sudo(["firewall-cmd", f"--zone={zone}",
                       f"--remove-port={item['id']}", "--permanent"])
        undo = f"sudo firewall-cmd --zone={zone} --add-port={item['id']} --permanent"

    if not apply:
        print(f"  {ui.GREY}would run: {' '.join(cmd)}{ui.RESET}")
        return False
    res = su.run(cmd, timeout=30)
    if res.ok:
        ui.ok(f"Closed: {item['title']}")
        print(f"  {ui.GREY}undo with: {undo}{ui.RESET}")
        return True
    ui.fail(f"Could not close {item['title']}: {res.err or res.out}")
    return False


def reload_firewall(apply: bool) -> None:
    cmd = su.sudo(["firewall-cmd", "--reload"])
    if not apply:
        print(f"  {ui.GREY}would run: {' '.join(cmd)}{ui.RESET}")
        return
    res = su.run(cmd, timeout=60)
    if res.ok:
        ui.ok("Firewall reloaded — changes are now live.")
    else:
        ui.fail(f"Reload failed: {res.err or res.out}")


# --------------------------------------------------------------------------
def report(zone: str) -> list[dict]:
    """Plain-English summary of what is currently reachable."""
    print(f"\n{ui.BOLD}{ui.CYAN}Firewall checkup{ui.RESET}")
    ui.ok(f"Your firewall is ON (zone: {zone})")

    items = openings(zone)
    if not items:
        ui.ok("Nothing is open to the network. This is as locked down as it gets.")
        return items

    # At-a-glance table first, then the plain-English detail underneath.
    table = ui.Table(["#", "What it is", "Verdict", "Technical name"])
    for idx, item in enumerate(items, 1):
        table.add([idx, item["title"], RISK_LABEL[item["risk"]],
                   f"{item['kind']} {item['id']}"], RISK_COLOUR[item["risk"]])
    print()
    table.show()

    print(f"\n{ui.BOLD}What each one means:{ui.RESET}\n")
    for idx, item in enumerate(items, 1):
        colour = RISK_COLOUR[item["risk"]]
        print(f"  {ui.BOLD}{idx}. {item['title']}{ui.RESET}  "
              f"{colour}({RISK_LABEL[item['risk']]}){ui.RESET}")
        print(f"     {item['why']}")
        print(f"     {ui.GREY}technical: {item['kind']} {item['id']}{ui.RESET}\n")

    risky = [i for i in items if i["risk"] == "risky"]
    if risky:
        ui.warn(f"{len(risky)} of these are worth closing.")
    else:
        ui.ok("Nothing here looks dangerous.")
    return items


def wizard(zone: str, assume_yes: bool) -> int:
    items = report(zone)
    if not items:
        return 0

    print(f"{ui.GREY}Nothing has been changed yet. You will be asked about each "
          f"one.{ui.RESET}\n")
    if not assume_yes and not ask("Go through these one at a time now?",
                                  default_no=False):
        ui.note("No changes made. Re-run any time with: ./bzt_firewall.py")
        return 0

    changed = False
    for item in items:
        if item["risk"] == "fine" and not assume_yes:
            continue
        print(f"\n{ui.BOLD}{item['title']}{ui.RESET} — {item['why']}")
        default_no = item["risk"] != "risky"
        if assume_yes:
            should = item["risk"] == "risky"
        else:
            should = ask(f"Close {item['title']}?", default_no=default_no)
        if should:
            changed |= apply_close(zone, item, apply=True)
        else:
            print(f"  {ui.GREY}left open{ui.RESET}")

    if changed:
        print()
        reload_firewall(apply=True)
        print(f"\n{ui.GREY}Re-run this tool any time to review the result.{ui.RESET}")
    else:
        ui.note("Nothing was changed.")
    return 0


# --------------------------------------------------------------------------
def parse_spec(spec: str) -> tuple[int, str]:
    port_s, _, proto = spec.partition("/")
    proto = (proto or "tcp").lower()
    if proto not in ("tcp", "udp"):
        raise ValueError(f"protocol must be tcp or udp, got {proto!r}")
    return int(port_s), proto


def drop_port(zone: str, port: int, proto: str, apply: bool, undo: bool) -> None:
    """Add or remove an explicit drop rule, covering IPv4 and IPv6."""
    verb = "--remove-rich-rule" if undo else "--add-rich-rule"
    for family in ("ipv4", "ipv6"):
        rule = f'rule family="{family}" port port="{port}" protocol="{proto}" drop'
        cmd = su.sudo(["firewall-cmd", f"--zone={zone}", f"{verb}={rule}",
                       "--permanent"])
        if not apply:
            print(f"  {ui.GREY}would run: {' '.join(cmd)}{ui.RESET}")
            continue
        res = su.run(cmd, timeout=30)
        if res.ok:
            ui.ok(f"{'Removed' if undo else 'Added'} {family} drop rule for "
                  f"{port}/{proto}")
        else:
            ui.fail(f"{family} rule failed: {res.err or res.out}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="With no options at all, you get the guided checkup.")
    ap.add_argument("--check", action="store_true",
                    help="just show the report, never ask or change anything")
    ap.add_argument("--harden", action="store_true",
                    help="close everything rated 'worth closing', no questions")
    ap.add_argument("--zone", default=None, help="firewalld zone (default: your active zone)")
    ap.add_argument("--expert", action="store_true", help="show the expert options")
    # Expert flags, kept from the previous version.
    ap.add_argument("--close", metavar="PORT/PROTO", action="append", default=[],
                    help=argparse.SUPPRESS)
    ap.add_argument("--drop", metavar="PORT/PROTO", action="append", default=[],
                    help=argparse.SUPPRESS)
    ap.add_argument("--undrop", metavar="PORT/PROTO", action="append", default=[],
                    help=argparse.SUPPRESS)
    ap.add_argument("--apply", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.expert:
        print("""Expert options:
  --close PORT/PROTO    withdraw an allow rule      (e.g. --close 1716/tcp)
  --drop  PORT/PROTO    add an explicit drop rule   (e.g. --drop 5355/udp)
  --undrop PORT/PROTO   remove a drop rule you added
  --apply               commit; without it these only preview
  --zone ZONE           operate on a specific zone""")
        return 0

    require_firewalld()
    zone = args.zone or default_zone()

    if args.close or args.drop or args.undrop:
        if not args.apply:
            ui.warn("Preview only — add --apply to commit.")
        try:
            for spec in args.close:
                port, proto = parse_spec(spec)
                item = {"kind": "port", "id": f"{port}/{proto}",
                        "title": f"port {port}/{proto}"}
                apply_close(zone, item, args.apply)
            for spec in args.drop:
                drop_port(zone, *parse_spec(spec), args.apply, undo=False)
            for spec in args.undrop:
                drop_port(zone, *parse_spec(spec), args.apply, undo=True)
        except ValueError as exc:
            ui.fail(str(exc))
            return 2
        reload_firewall(args.apply)
        return 0

    if args.check:
        report(zone)
        print(f"\n{ui.GREY}This was read-only. Run ./bzt_firewall.py with no "
              f"options to act on it.{ui.RESET}")
        return 0

    return wizard(zone, assume_yes=args.harden)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        ui.note("Cancelled — nothing was changed.")
        sys.exit(130)
