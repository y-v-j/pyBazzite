#!/usr/bin/env python3
"""List systemd units, or manage them from an interactive terminal UI.

Merges bazzite_services.py (static table) and bazzite_services_02.py (curses
TUI) into one tool: plain listing by default, `--tui` for the interactive view.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from bzt import sysutil as su
from bzt import ui


def fetch(scope: str, state: str | None) -> list[dict]:
    cmd = ["systemctl", "list-units", "--type=service", "--all",
           "--output=json", "--no-pager"]
    if scope == "user":
        cmd.insert(1, "--user")
    if state:
        cmd.append(f"--state={state}")
    res = su.run(cmd, timeout=60)
    if not res.ok or not res.out:
        return []
    try:
        return sorted(json.loads(res.out), key=lambda u: u.get("unit", ""))
    except json.JSONDecodeError:
        return []


def colour_for(unit: dict) -> str:
    active = (unit.get("active") or "").lower()
    sub = (unit.get("sub") or "").lower()
    if active == "failed" or sub == "failed":
        return ui.RED
    if active == "active" or sub == "running":
        return ui.GREEN
    if active in ("activating", "reloading", "deactivating"):
        return ui.YELLOW
    return ui.GREY


def static_view(units: list[dict]) -> None:
    ui.header("systemd services")
    table = ui.Table(["Unit", "Active", "Sub", "Load", "Description"])
    for unit in units:
        table.add([unit.get("unit", "?"), unit.get("active", "?"),
                   unit.get("sub", "?"), unit.get("load", "?"),
                   unit.get("description", "")], colour_for(unit))
    table.show("No matching units.")

    failed = [u for u in units if (u.get("active") or "").lower() == "failed"]
    if failed:
        ui.warn(f"{len(failed)} failed unit(s):")
        for unit in failed:
            print(f"    - {unit.get('unit')}")
    print(f"\n{ui.GREEN}active{ui.RESET}  {ui.YELLOW}transitioning{ui.RESET}  "
          f"{ui.RED}failed{ui.RESET}  {ui.GREY}inactive{ui.RESET}")


# --------------------------------------------------------------------------
# Interactive TUI
# --------------------------------------------------------------------------
def run_tui(scope: str, state: str | None) -> None:
    import curses

    def safe_add(win, y, x, text, attr=0):
        """addstr that clips to the window — curses raises if you overrun it."""
        height, width = win.getmaxyx()
        if y >= height or x >= width:
            return
        win.addstr(y, x, text[: max(0, width - x - 1)], attr)

    def act(stdscr, unit: str, action: str) -> None:
        curses.def_prog_mode()
        curses.endwin()
        print("=" * 60)
        print(f"  systemctl {action} {unit}")
        print("=" * 60)
        cmd = ["systemctl"] + (["--user"] if scope == "user" else []) + [action, unit]
        if scope != "user":
            cmd = su.sudo(cmd)
        # subprocess, not os.system: no shell parses the unit name, and the
        # return code is the real exit status rather than a wait() status word.
        proc = subprocess.run(cmd)
        if proc.returncode == 0:
            print(f"\n[+] {action} succeeded for {unit}")
        else:
            print(f"\n[-] {action} failed (exit {proc.returncode})")
        input("\nPress Enter to return...")
        curses.reset_prog_mode()
        stdscr.refresh()

    def draw(stdscr):
        curses.curs_set(0)
        stdscr.keypad(True)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)
            curses.init_pair(3, curses.COLOR_RED, -1)
            curses.init_pair(4, curses.COLOR_WHITE, -1)
            curses.init_pair(5, curses.COLOR_CYAN, -1)
            curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_CYAN)

        units = fetch(scope, state)
        selected, offset = 0, 0

        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            if height < 10 or width < 60:
                safe_add(stdscr, 0, 0, "Terminal too small — please resize.",
                         curses.A_BOLD)
                stdscr.refresh()
                if stdscr.getch() in (ord("q"), ord("Q")):
                    return
                continue

            safe_add(stdscr, 0, 2, f"BAZZITE SERVICE MANAGER ({scope} scope)",
                     curses.A_BOLD | curses.color_pair(5))
            safe_add(stdscr, 1, 2,
                     "[Up/Down] move  [S]tart  [X] stop  [R]estart  "
                     "[L] reload list  [Q]uit", curses.A_DIM)

            col1 = min(42, max(20, width // 3))
            col2, col3 = 12, 12
            safe_add(stdscr, 3, 2,
                     f"{'Unit'.ljust(col1)} {'Active'.ljust(col2)} "
                     f"{'Sub'.ljust(col3)} Description",
                     curses.A_BOLD | curses.A_UNDERLINE)

            rows = height - 6
            selected = max(0, min(selected, len(units) - 1)) if units else 0
            offset = min(offset, selected)
            if selected >= offset + rows:
                offset = selected - rows + 1

            for idx, unit in enumerate(units[offset:offset + rows]):
                real = offset + idx
                active = (unit.get("active") or "").lower()
                sub = (unit.get("sub") or "").lower()
                if active == "failed" or sub == "failed":
                    pair = curses.color_pair(3)
                elif active == "active" or sub == "running":
                    pair = curses.color_pair(1)
                elif active in ("activating", "reloading"):
                    pair = curses.color_pair(2)
                else:
                    pair = curses.color_pair(4)

                text = (f"{unit.get('unit','?')[:col1].ljust(col1)} "
                        f"{unit.get('active','?')[:col2].ljust(col2)} "
                        f"{unit.get('sub','?')[:col3].ljust(col3)} "
                        f"{unit.get('description','')}")
                if real == selected:
                    safe_add(stdscr, 4 + idx, 1, text.ljust(width - 2),
                             curses.color_pair(6) | curses.A_BOLD)
                else:
                    safe_add(stdscr, 4 + idx, 1, text, pair)

            if units:
                safe_add(stdscr, height - 1, 2,
                         f" {units[selected].get('unit','?')} ", curses.A_REVERSE)
            stdscr.refresh()

            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                return
            if key == curses.KEY_UP:
                selected = max(0, selected - 1)
            elif key == curses.KEY_DOWN:
                selected = min(len(units) - 1, selected + 1) if units else 0
            elif key == curses.KEY_NPAGE:
                selected = min(len(units) - 1, selected + rows) if units else 0
            elif key == curses.KEY_PPAGE:
                selected = max(0, selected - rows)
            elif key in (ord("l"), ord("L")):
                units = fetch(scope, state)
            elif units and key in (ord("s"), ord("S")):
                act(stdscr, units[selected]["unit"], "start")
                units = fetch(scope, state)
            elif units and key in (ord("x"), ord("X")):
                act(stdscr, units[selected]["unit"], "stop")
                units = fetch(scope, state)
            elif units and key in (ord("r"), ord("R")):
                act(stdscr, units[selected]["unit"], "restart")
                units = fetch(scope, state)

    curses.wrapper(draw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tui", action="store_true", help="interactive manager")
    ap.add_argument("--user", dest="scope", action="store_const",
                    const="user", default="system",
                    help="operate on user units instead of system units")
    ap.add_argument("--state", help="filter by state, e.g. failed, running")
    args = ap.parse_args()

    if not su.has("systemctl"):
        ui.fail("systemctl not found")
        return 1

    if args.tui:
        try:
            run_tui(args.scope, args.state)
        except KeyboardInterrupt:
            pass
        print("[+] Exited service manager.")
        return 0

    static_view(fetch(args.scope, args.state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
