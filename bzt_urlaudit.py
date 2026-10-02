#!/usr/bin/env python3
"""Audit a website's security posture, trackers and privacy signals.

Rewrite of bazzite_url_checker.py, whose BeautifulSoup import sat outside the
dependency guard: a missing bs4 crashed with a raw ImportError before the
helpful "please install" message could ever print.
"""

from __future__ import annotations

import argparse
import re
import socket
import sys
import urllib.parse

MISSING = []
try:
    import requests
except ImportError:
    MISSING.append("requests")
try:
    from bs4 import BeautifulSoup
except ImportError:
    MISSING.append("beautifulsoup4")

if MISSING:
    print(f"\033[91m[-] Missing dependencies: {', '.join(MISSING)}\033[0m",
          file=sys.stderr)
    print(f"    pip install --user {' '.join(MISSING)}", file=sys.stderr)
    sys.exit(1)

from bzt import ui

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

TRACKERS = {
    "Google Analytics": ("google-analytics.com", "googletagmanager.com", "gtag("),
    "Facebook Pixel": ("connect.facebook.net", "fbevents.js"),
    "Hotjar": ("hotjar.com",),
    "TikTok Pixel": ("analytics.tiktok.com",),
    "Mixpanel": ("mixpanel.com",),
    "Segment": ("cdn.segment.com",),
    "Microsoft Clarity": ("clarity.ms",),
}

PAYWALL_SIGNATURES = ("paywall", "tinypass", "piano-id", "swg-button",
                      "premium-content", "subscribe-meta", "meteredContent")


def hosting(domain: str):
    try:
        ip = socket.gethostbyname(domain)
    except OSError:
        return "unresolved", "—", "—"
    try:
        geo = requests.get(f"https://ip-api.com/json/{ip}", timeout=4).json()
        if geo.get("status") == "success":
            return ip, f"{geo.get('city', '?')}, {geo.get('country', '?')}", \
                   geo.get("isp", "unknown")
    except Exception:
        pass
    return ip, "lookup failed", "unknown"


def audit(url: str, do_geo: bool) -> int:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    domain = urllib.parse.urlparse(url).netloc

    ui.note(f"fetching {url}")
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=10,
                            allow_redirects=True)
    except requests.RequestException as exc:
        ui.fail(f"could not reach the site: {exc}")
        return 1
    soup = BeautifulSoup(resp.text, "html.parser")
    body = resp.text.lower()

    ip, location, isp = ("—", "—", "—")
    if do_geo:
        ip, location, isp = hosting(domain)

    https = resp.url.startswith("https://")
    csp = resp.headers.get("Content-Security-Policy")
    hsts = resp.headers.get("Strict-Transport-Security")
    xfo = resp.headers.get("X-Frame-Options")

    cookies = resp.cookies.get_dict()
    passwords = soup.find_all("input", {"type": "password"})
    login_forms = soup.find_all("form", action=re.compile(
        r"(login|signin|authorize)", re.I))

    found_trackers = [name for name, sigs in TRACKERS.items()
                      if any(sig in body for sig in sigs)]
    paywall = next((s for s in PAYWALL_SIGNATURES if s.lower() in body), None)

    permissions = resp.headers.get("Permissions-Policy") or \
        resp.headers.get("Feature-Policy") or ""
    sensors = [s for s in ("camera", "microphone", "geolocation")
               if s in permissions]
    if "geolocation.getcurrentposition" in body:
        sensors.append("geolocation (JS)")

    if not https:
        verdict, vcolour = "UNSAFE — no encryption", ui.RED
    elif csp and hsts:
        verdict, vcolour = "SOLID", ui.GREEN
    elif csp or hsts:
        verdict, vcolour = "REASONABLE", ui.YELLOW
    else:
        verdict, vcolour = "WEAK HEADERS", ui.YELLOW

    ui.header(f"Security audit: {domain}")
    good, bad = ui.GREEN, ui.RED

    table = ui.Table(["Check", "Result", "Notes"])
    table.add(["Final URL", resp.url, f"HTTP {resp.status_code}"],
              good if resp.ok else bad)
    table.add(["Overall verdict", verdict, "based on transport and headers"], vcolour)
    table.add(["Encryption (HTTPS)", "yes" if https else "no",
               "traffic is encrypted" if https else "sent in clear text"],
              good if https else bad)
    table.add(["HSTS", "set" if hsts else "missing",
               "forces future visits to HTTPS"], good if hsts else ui.YELLOW)
    table.add(["Content-Security-Policy", "set" if csp else "missing",
               "limits where scripts may load from"], good if csp else ui.YELLOW)
    table.add(["X-Frame-Options", xfo or "missing",
               "clickjacking protection"], good if xfo else ui.YELLOW)
    if do_geo:
        table.add(["Server IP", ip, location], ui.CYAN)
        table.add(["Hosting provider", isp, ""], ui.CYAN)
    table.show()

    ui.header("Privacy and content")
    privacy = ui.Table(["Check", "Result", "Notes"])
    privacy.add(["Cookies set on load", str(len(cookies)),
                 ", ".join(cookies) or "none"],
                ui.YELLOW if cookies else good)
    privacy.add(["Trackers detected", str(len(found_trackers)),
                 ", ".join(found_trackers) or "none found"],
                ui.YELLOW if found_trackers else good)
    privacy.add(["Paywall", "likely" if paywall else "none detected",
                 f"signature: {paywall}" if paywall else ""],
                ui.YELLOW if paywall else good)
    privacy.add(["Login form", "yes" if (passwords or login_forms) else "no",
                 f"{len(passwords)} password field(s)"],
                ui.CYAN if passwords else ui.GREY)
    privacy.add(["Sensor access", ", ".join(sensors) or "none requested",
                 "camera/mic/location"],
                ui.RED if any(s in sensors for s in ("camera", "microphone")) else good)
    privacy.add(["Page size", f"{len(resp.content) / 1024:.1f} KB",
                 "transferred on first load"], ui.GREY)
    privacy.show()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url", help="site to audit, e.g. github.com")
    ap.add_argument("--no-geo", action="store_true",
                    help="skip the third-party IP geolocation lookup")
    args = ap.parse_args()
    return audit(args.url, do_geo=not args.no_geo)


if __name__ == "__main__":
    sys.exit(main())
