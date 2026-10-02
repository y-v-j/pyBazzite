"""Terminal colour and table helpers shared by every tool in the suite.

The tables here render real UTF-8 box-drawing characters and fall back to
plain ASCII when stdout cannot encode them, so output stays readable when
piped, redirected, or viewed under a non-UTF-8 locale.
"""

from __future__ import annotations

import codecs
import os
import re
import shutil
import sys

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _colour_enabled() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


_USE_COLOUR = _colour_enabled()


def _seq(code: str) -> str:
    return code if _USE_COLOUR else ""


RESET = _seq("\033[0m")
BOLD = _seq("\033[1m")
DIM = _seq("\033[2m")
GREY = _seq("\033[90m")
RED = _seq("\033[91m")
GREEN = _seq("\033[92m")
YELLOW = _seq("\033[93m")
BLUE = _seq("\033[94m")
CYAN = _seq("\033[96m")


def _unicode_ok() -> bool:
    enc = (sys.stdout.encoding or "ascii").lower()
    return "utf" in enc


# Typographic characters used in prose throughout the suite. On a terminal
# that cannot encode them, transliterate rather than raising UnicodeEncodeError
# mid-table and leaving a half-drawn frame on screen.
_ASCII_SUBSTITUTES = {
    "\u2014": "-", "\u2013": "-", "\u2026": "...", "\u2588": "#",
    "\u2591": ".", "\u2192": "->", "\u2022": "*", "\u00b0": "deg",
}


def _ascii_fallback(exc):
    chunk = exc.object[exc.start:exc.end]
    return ("".join(_ASCII_SUBSTITUTES.get(ch, "?") for ch in chunk), exc.end)


codecs.register_error("bzt_ascii", _ascii_fallback)

if not _unicode_ok():
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="bzt_ascii")
        except (AttributeError, ValueError):
            pass

BAR_FULL, BAR_EMPTY = ("\u2588", "\u2591") if _unicode_ok() else ("#", ".")
DASH = "\u2014" if _unicode_ok() else "-"


def bar(percent: float, width: int = 20) -> str:
    """A fixed-width usage meter that degrades to ASCII when needed."""
    filled = max(0, min(width, int(round(percent / 100 * width))))
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


if _unicode_ok():
    _B = dict(tl="┌", tm="┬", tr="┐",
              ml="├", mm="┼", mr="┤",
              bl="└", bm="┴", br="┘",
              h="─", v="│", ell="…")
else:
    _B = dict(tl="+", tm="+", tr="+", ml="+", mm="+", mr="+",
              bl="+", bm="+", br="+", h="-", v="|", ell="...")


def visible(text: str) -> int:
    """Length of *text* ignoring ANSI colour sequences."""
    return len(_ANSI_RE.sub("", text))


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - visible(text))


def truncate(text: str, width: int) -> str:
    if visible(text) <= width:
        return text
    plain = _ANSI_RE.sub("", text)
    ell = _B["ell"]
    return plain[: max(0, width - len(ell))] + ell


def header(title: str) -> None:
    width = min(shutil.get_terminal_size((80, 24)).columns, 100)
    print(f"\n{BOLD}{CYAN}{title}{RESET}")
    print(GREY + _B["h"] * width + RESET)


def note(msg: str) -> None:
    print(f"{GREY}[*] {msg}{RESET}")


def ok(msg: str) -> None:
    print(f"{GREEN}[+] {msg}{RESET}")


def warn(msg: str) -> None:
    print(f"{YELLOW}[!] {msg}{RESET}")


def fail(msg: str) -> None:
    print(f"{RED}[-] {msg}{RESET}", file=sys.stderr)


class Table:
    """A self-sizing terminal table that never breaks its own alignment.

    Cells are stored uncoloured and padded before colour is applied, which is
    what keeps columns square even when rows carry different highlights.
    """

    def __init__(self, headers, max_total: int | None = None):
        self.headers = [str(h) for h in headers]
        self.rows: list[list[str]] = []
        self.colours: list[str] = []
        self.max_total = max_total

    def add(self, cells, colour: str = "") -> None:
        self.rows.append(["" if c is None else str(c) for c in cells])
        self.colours.append(colour)

    def _widths(self) -> list[int]:
        n = len(self.headers)
        w = [len(h) for h in self.headers]
        for row in self.rows:
            for i in range(n):
                if i < len(row):
                    w[i] = max(w[i], visible(row[i]))
        term = self.max_total or shutil.get_terminal_size((100, 24)).columns
        overhead = 3 * n + 1
        # Shrink the widest column repeatedly until the table fits the terminal.
        while sum(w) + overhead > term and max(w) > 6:
            w[w.index(max(w))] -= 1
        return w

    def render(self, empty: str = "No data.") -> str:
        w = self._widths()
        h, v = _B["h"], _B["v"]
        top = _B["tl"] + _B["tm"].join(h * (x + 2) for x in w) + _B["tr"]
        mid = _B["ml"] + _B["mm"].join(h * (x + 2) for x in w) + _B["mr"]
        bot = _B["bl"] + _B["bm"].join(h * (x + 2) for x in w) + _B["br"]

        lines = [top]
        head = [f"{BOLD}{CYAN}{pad(truncate(t, x), x)}{RESET}"
                for t, x in zip(self.headers, w)]
        lines.append(v + v.join(f" {c} " for c in head) + v)
        lines.append(mid)

        if not self.rows:
            inner = sum(w) + 3 * len(w) - 1
            lines.append(v + pad(truncate(" " + empty, inner), inner) + v)
        else:
            for row, colour in zip(self.rows, self.colours):
                cells = []
                for i, x in enumerate(w):
                    txt = pad(truncate(row[i] if i < len(row) else "", x), x)
                    cells.append(f"{colour}{txt}{RESET}" if colour else txt)
                lines.append(v + v.join(f" {c} " for c in cells) + v)

        lines.append(bot)
        return "\n".join(lines)

    def show(self, empty: str = "No data.") -> None:
        print(self.render(empty))


class StreamTable:
    """A table whose rows are printed as they arrive.

    Used by the live monitors, where the full row set is not known up front so
    the self-sizing Table cannot be used.
    """

    def __init__(self, headers, widths):
        self.headers = list(headers)
        self.widths = list(widths)

    def _rule(self, left: str, mid: str, right: str) -> str:
        return left + mid.join(_B["h"] * (w + 2) for w in self.widths) + right

    def start(self) -> None:
        v = _B["v"]
        print(self._rule(_B["tl"], _B["tm"], _B["tr"]))
        cells = [f"{BOLD}{CYAN}{pad(truncate(h, w), w)}{RESET}"
                 for h, w in zip(self.headers, self.widths)]
        print(v + v.join(f" {c} " for c in cells) + v)
        print(self._rule(_B["ml"], _B["mm"], _B["mr"]))

    def row(self, cells, colour: str = "") -> None:
        v = _B["v"]
        out = []
        for cell, width in zip(cells, self.widths):
            text = pad(truncate(str(cell), width), width)
            out.append(f"{colour}{text}{RESET}" if colour else text)
        print(v + v.join(f" {c} " for c in out) + v, flush=True)

    def end(self) -> None:
        print(self._rule(_B["bl"], _B["bm"], _B["br"]))
