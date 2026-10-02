#!/usr/bin/env python3
"""Deep-clean an atomic Fedora / Bazzite host.

Replaces the overlapping atomicity.py, bazzite_cleaner.py and the maintenance
half of bazzite_security_maintainence.py. Covers package-manager residue,
developer tool caches, browser and DNS caches, recent-file and clipboard
history, orphaned data left behind by uninstalled apps, and dangling files.
Named removable drives (--usb) are swept of the metadata, trash cans and search
indexes that other operating systems leave on them.

Every stage honours --dry-run, which reports exactly what would go and how
much it would reclaim without deleting anything.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import sys
from pathlib import Path

from bzt import sysutil as su
from bzt import ui

# Kept by default: expensive to rebuild and directly costs you frame rate,
# which is the whole point of this platform. Override with --purge-shaders.
SHADER_CACHES = {"mesa_shader_cache", "mesa_shader_cache_db",
                 "radv_builtin_shaders", "nvidia", "nv"}
# Never removed: deleting these breaks running sessions or stored secrets.
NEVER = {"keyring", "flatpak", "gnome-keyring", "dconf"}

STAGES = ("ostree", "flatpak", "podman", "devcache", "journal", "cache",
          "browser", "dns", "recent", "clipboard", "trash", "temp",
          "orphans", "usb", "trim")

# Junk other operating systems scatter across removable media. None of it is
# user data: trash cans, search indexes and per-directory view settings that
# are regenerated on demand.
USB_JUNK_DIRS = {".Trashes", ".Spotlight-V100", ".fseventsd", ".TemporaryItems",
                 ".DocumentRevisions-V100", ".thumbnails", "$RECYCLE.BIN",
                 "RECYCLER", "System Volume Information"}
USB_JUNK_DIR_GLOBS = (".Trash-*",)
USB_JUNK_FILES = {".DS_Store", "Thumbs.db", "ehthumbs.db", "desktop.ini",
                  ".apdisk", ".directory", "IndexerVolumeGuid",
                  "WPSettings.dat", "autorun.inf"}
USB_JUNK_FILE_GLOBS = ("._*", ".fuse_hidden*")
# --deep only: leftovers that are usually junk but could still be wanted.
USB_DEEP_DIR_GLOBS = ("found.[0-9][0-9][0-9]",)
USB_DEEP_FILE_GLOBS = ("*.tmp", "~$*", "*.chk", "*.part", "*.crdownload")

# Refusing to sweep these keeps a mistyped --usb from eating the host.
PROTECTED_MOUNTS = {"/", "/boot", "/boot/efi", "/efi", "/etc", "/home", "/usr",
                    "/var", "/var/home", "/srv", "/opt", "/sysroot", "/run"}


class Cleaner:
    def __init__(self, args):
        self.dry = args.dry_run
        self.deep = args.deep
        self.purge_shaders = args.purge_shaders
        self.usb_names = list(args.usb or [])
        self.user = su.target_user()
        self.home = self.user.home if self.user else Path.home()
        self.freed = 0
        self.usb_freed = 0
        self.dropping_privs = bool(
            self.user and su.is_root() and self.user.uid != os.geteuid())
        self.runtime_dir = Path(f"/run/user/{self.user.uid}") if self.user else None
        self.user_env = self._user_env()

    # ---------------------------------------------------------------- utils
    def _user_env(self) -> dict:
        """Environment for the commands we re-run as the login user.

        Under `sudo`, pam_systemd sets XDG_RUNTIME_DIR=/run/user/0, and
        `runuser` hands the environment it was given straight through. Anything
        user-scoped — just/ujust, rootless podman, wl-copy, the session bus —
        then tries to use root's 0700 runtime directory as an unprivileged user
        and dies with EACCES. Point every session variable back at the real
        user before dropping privileges.
        """
        env = dict(os.environ)
        # Cheap wins regardless of who we are: `brew cleanup` otherwise tries to
        # auto-update first, which is what reports "HTTP status: 000" when it
        # cannot reach the network.
        env.update(HOMEBREW_NO_AUTO_UPDATE="1", HOMEBREW_NO_ANALYTICS="1",
                   HOMEBREW_NO_ENV_HINTS="1")
        if not self.dropping_privs:
            return env
        runtime = str(self.runtime_dir)
        env.update(
            HOME=str(self.home),
            USER=self.user.name,
            LOGNAME=self.user.name,
            XDG_RUNTIME_DIR=runtime,
            XDG_CONFIG_HOME=str(self.home / ".config"),
            XDG_CACHE_HOME=str(self.home / ".cache"),
            XDG_DATA_HOME=str(self.home / ".local/share"),
            XDG_STATE_HOME=str(self.home / ".local/state"),
            DBUS_SESSION_BUS_ADDRESS=f"unix:path={runtime}/bus",
        )
        for stale in ("SUDO_USER", "SUDO_UID", "SUDO_GID", "SUDO_COMMAND",
                      "XDG_SESSION_ID", "XAUTHORITY", "MAIL"):
            env.pop(stale, None)
        # WAYLAND_DISPLAY is relative to XDG_RUNTIME_DIR, so it only resolves
        # once the line above is right; discover it when sudo stripped it.
        if "WAYLAND_DISPLAY" not in env and self.runtime_dir.is_dir():
            sockets = sorted(s.name for s in self.runtime_dir.glob("wayland-*")
                             if not s.name.endswith(".lock"))
            if sockets:
                env["WAYLAND_DISPLAY"] = sockets[0]
        return env

    def has_session(self) -> bool:
        """True when the target user has a live systemd user session.

        Without /run/user/<uid> there is no session bus, no rootless podman
        runtime and no Wayland socket, so the user-scoped steps cannot work.
        """
        return bool(self.runtime_dir and self.runtime_dir.is_dir())

    def do(self, cmd, desc, as_user=False, timeout=900, needs_session=False,
           optional=False):
        if as_user and self.user:
            if needs_session and not self.has_session():
                ui.note(f"{desc}: no session at {self.runtime_dir} — skipping "
                        "(log in as the user, or run this without sudo)")
                return False
            cmd = su.as_user(self.user, cmd)
        env = self.user_env if as_user else None
        if self.dry:
            print(f"  {ui.GREY}would run:{ui.RESET} {' '.join(cmd)}")
            return True
        res = su.run(cmd, timeout=timeout, env=env)
        if res.ok:
            ui.ok(desc)
        else:
            tail = (res.err or res.out or "no output").splitlines()
            msg = f"{desc} — exit {res.rc}: {tail[-1] if tail else ''}"
            # An optional step that fails is reported, never treated as damage:
            # the work it would have done is covered by our own stages.
            ui.note(msg) if optional else ui.warn(msg)
        return res.ok

    def purge(self, path: Path, desc: str, contents_only: bool = True):
        """Delete a path (or just its contents), accounting for the space freed."""
        if not path.exists():
            return
        size = su.dir_size(path) if path.is_dir() else path.stat().st_size
        if size == 0 and path.is_dir() and not any(path.iterdir()):
            return
        if self.dry:
            print(f"  {ui.GREY}would clear:{ui.RESET} {path} ({su.human(size)})")
            self.freed += size
            return
        try:
            if path.is_dir() and contents_only:
                for child in path.iterdir():
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            ui.warn(f"{desc}: {exc}")
            return
        self.freed += size
        ui.ok(f"{desc} ({su.human(size)})")

    def purge_many(self, paths, label: str):
        for path in paths:
            if path.exists():
                self.purge(path, f"{label}: {path.name}")

    def _dbus_service(self, qdbus: str, service: str) -> bool:
        """True when *service* is on the target user's session bus."""
        if not self.has_session():
            return False
        cmd = su.as_user(self.user, [qdbus]) if self.user else [qdbus]
        res = su.run(cmd, timeout=20, env=self.user_env)
        return res.ok and service in res.out.split()

    @staticmethod
    def _running() -> set[str]:
        res = su.run(["ps", "-eo", "comm="], timeout=15)
        return {l.strip().lower() for l in res.out.splitlines()} if res.ok else set()

    # --------------------------------------------------------------- stages
    def ostree(self):
        ui.header("rpm-ostree base")
        if not su.has("rpm-ostree"):
            ui.note("rpm-ostree not present — skipping")
            return
        # -b base, -m repomd, -p pending. The rollback deployment (-r) is left
        # alone: dropping it removes your ability to recover from a bad update.
        self.do(su.sudo(["rpm-ostree", "cleanup", "-b", "-m", "-p"]),
                "Pruned pending deployment and cached metadata")
        self.ujust_clean_system()

    def ujust_clean_system(self):
        """Run Bazzite's own clean-system recipe, if this image ships one.

        Run as the login user, never as root: the recipe touches rootless podman
        storage, per-user flatpaks and Homebrew, and Homebrew exits 1 outright
        when invoked as root. It is entirely optional — every step it performs
        is also covered by our ostree/flatpak/podman stages — so a failure here
        is reported and stepped over rather than allowed to look like breakage.
        """
        if not su.has("ujust"):
            ui.note("ujust not present — skipping clean-system")
            return
        if self.dropping_privs and not self.has_session():
            ui.note(f"ujust clean-system: {self.user.name} has no session at "
                    f"{self.runtime_dir} — skipping (its stages run anyway)")
            return
        # `just` writes shebang recipes into $XDG_RUNTIME_DIR/just, so the
        # recipe cannot run at all without a usable runtime directory.
        if not self.dry and self.dropping_privs and not os.access(
                self.runtime_dir, os.R_OK | os.W_OK | os.X_OK):
            ui.note(f"ujust clean-system: {self.runtime_dir} is not usable — "
                    "skipping (its stages run anyway)")
            return
        # Recipe names drift between Bazzite/Bluefin/Aurora releases; asking
        # first turns a confusing "Justfile does not contain recipe" error
        # into a one-line note.
        if not self.dry and not self._has_recipe("clean-system"):
            ui.note("ujust has no clean-system recipe on this image — skipping")
            return
        self.do(["ujust", "clean-system"],
                "Ran Bazzite's ujust clean-system recipe",
                as_user=True, needs_session=True, optional=True)

    def _has_recipe(self, recipe: str) -> bool:
        """True if *recipe* is listed by ujust on this image."""
        cmd = su.as_user(self.user, ["ujust", "--summary"]) if self.user else \
            ["ujust", "--summary"]
        res = su.run(cmd, timeout=60, env=self.user_env)
        if not res.ok:
            return True  # cannot tell — let the real invocation decide
        return recipe in res.out.split()

    def flatpak(self):
        ui.header("Flatpak runtimes and residue")
        if not su.has("flatpak"):
            ui.note("flatpak not installed — skipping")
            return
        self.do(su.sudo(["flatpak", "uninstall", "--unused", "-y"]),
                "Removed unused system runtimes")
        self.do(["flatpak", "--user", "uninstall", "--unused", "-y"],
                "Removed unused per-user runtimes", as_user=True)
        self.do(su.sudo(["flatpak", "repair"]), "Repaired system installation")
        self.purge(Path("/var/lib/flatpak/appstream"), "Cleared system AppStream cache")
        self.purge(self.home / ".local/share/flatpak/appstream",
                   "Cleared user AppStream cache")
        # Per-app caches inside the sandbox homes.
        var_app = self.home / ".var/app"
        if var_app.is_dir():
            for app in sorted(var_app.iterdir()):
                self.purge(app / "cache", f"Sandbox cache: {app.name}")

    def podman(self):
        ui.header("Container images and build cache")
        if not su.has("podman"):
            ui.note("podman not installed — skipping")
            return
        # Rootless podman keeps its state under $XDG_RUNTIME_DIR/libpod, so
        # these need the user's own runtime directory, not root's.
        self.do(["podman", "image", "prune", "-af"],
                "Pruned dangling container images",
                as_user=True, needs_session=True)
        self.do(["podman", "system", "prune", "-f"],
                "Pruned stopped containers and unused networks",
                as_user=True, needs_session=True)
        if self.deep:
            self.do(["podman", "volume", "prune", "-f"],
                    "Pruned unused volumes", as_user=True, needs_session=True)

    def devcache(self):
        ui.header("Developer and package-manager caches")
        cache = self.home / ".cache"
        self.purge_many([
            cache / "pip", cache / "uv", cache / "go-build", cache / "yarn",
            cache / "Homebrew", cache / "pre-commit", cache / "puppeteer",
            self.home / ".npm/_cacache",
            self.home / ".cargo/registry/cache",
            self.home / ".gradle/caches",
            self.home / ".m2/repository/.cache",
        ], "Dev cache")

        # conda and Homebrew have their own cleaners that know what is safe.
        if su.has("conda"):
            self.do(["conda", "clean", "-a", "-y"],
                    "Cleaned conda package cache", as_user=True)
        brew = Path("/home/linuxbrew/.linuxbrew/bin/brew")
        if os.access(brew, os.X_OK):
            # Must run as the user — Homebrew refuses to execute as root.
            self.do([str(brew), "cleanup", "-s"],
                    "Cleaned Homebrew cache", as_user=True)

    def journal(self):
        ui.header("System logs and crash dumps")
        self.do(su.sudo(["journalctl", "--vacuum-time=7d"]),
                "Trimmed journal to the last 7 days")
        self.purge(Path("/var/lib/systemd/coredump"), "Cleared systemd coredumps")
        self.purge(self.home / ".xsession-errors", "Removed .xsession-errors",
                   contents_only=False)

    def cache(self):
        ui.header("User cache directory")
        cache = self.home / ".cache"
        if not cache.is_dir():
            ui.note(f"{cache} does not exist — skipping")
            return
        for entry in sorted(cache.iterdir()):
            if entry.name in NEVER:
                print(f"  {ui.GREY}protected, keeping: {entry.name}{ui.RESET}")
                continue
            if entry.name in SHADER_CACHES and not self.purge_shaders:
                print(f"  {ui.GREY}keeping shader cache: {entry.name}{ui.RESET}")
                continue
            self.purge(entry, f"Cache: {entry.name}",
                       contents_only=not entry.is_dir())
        self.purge(self.home / ".thumbnails", "Legacy thumbnail cache")

    def browser(self):
        ui.header("Browser and internet cache")
        running = self._running()
        targets = {
            "firefox": [self.home / ".var/app/org.mozilla.firefox/cache",
                        self.home / ".cache/mozilla"],
            "brave": [self.home / ".var/app/com.brave.Browser/cache",
                      self.home / ".cache/BraveSoftware"],
            "chrome": [self.home / ".var/app/com.google.Chrome/cache",
                       self.home / ".cache/google-chrome"],
            "chromium": [self.home / ".var/app/org.chromium.Chromium/cache",
                         self.home / ".cache/chromium"],
            "vivaldi": [self.home / ".cache/vivaldi"],
        }
        for name, paths in targets.items():
            # Clearing a live profile can corrupt it, so skip running browsers.
            if any(name in proc for proc in running):
                ui.warn(f"{name} is running — skipping (close it and re-run)")
                continue
            for path in paths:
                self.purge(path, f"{name} cache")

        if self.deep:
            ui.warn("--deep: clearing cookies and history will sign you out "
                    "of every site")
            for profile in (self.home / ".var/app/org.mozilla.firefox/"
                            ".mozilla/firefox").glob("*/"):
                for name in ("cookies.sqlite", "places.sqlite",
                             "sessionstore.jsonlz4", "formhistory.sqlite"):
                    self.purge(profile / name, f"firefox {name}",
                               contents_only=False)

    def dns(self):
        ui.header("DNS resolver cache")
        if su.has("resolvectl"):
            self.do(["resolvectl", "flush-caches"], "Flushed systemd-resolved cache")
            self.do(["resolvectl", "reset-server-features"],
                    "Reset resolver feature cache")
        else:
            ui.note("systemd-resolved not present — skipping")
        # Avahi/mDNS keeps its own reachability cache.
        if su.has("systemctl"):
            self.do(su.sudo(["systemctl", "try-restart", "avahi-daemon.service"]),
                    "Restarted avahi-daemon to drop its mDNS cache", timeout=60)

    def recent(self):
        ui.header("Recently-accessed file history")
        self.purge_many([
            self.home / ".local/share/recently-used.xbel",
            self.home / ".recently-used.xbel",
            self.home / ".local/share/RecentDocuments",
            self.home / ".kde4/share/apps/RecentDocuments",
        ], "Recent files")
        # KDE stores recent documents and run history in these config files.
        for name in ("krunnerstaterc", "kactivitymanagerd-statsrc"):
            self.purge(self.home / ".config" / name, f"KDE history: {name}",
                       contents_only=False)
        self.purge(self.home / ".local/share/kactivitymanagerd/resources",
                   "KDE activity resource history")

    def clipboard(self):
        ui.header("Clipboard history")
        # Ask Klipper to drop its in-memory history first, otherwise it just
        # rewrites the file we are about to delete when it next saves.
        # Qt 5 and Qt 6 name this binary differently and a third-party Qt on
        # PATH may not reach the session bus at all, so take the first one that
        # can actually see Klipper rather than the first one that exists.
        tools = [c for c in ("qdbus", "qdbus6", "qdbus-qt6") if su.has(c)]
        qdbus = next((c for c in tools
                      if self._dbus_service(c, "org.kde.klipper")), None)
        if qdbus:
            self.do([qdbus, "org.kde.klipper", "/klipper",
                     "org.kde.klipper.klipper.clearClipboardHistory"],
                    "Cleared Klipper clipboard history",
                    as_user=True, needs_session=True, timeout=30)
        elif tools:
            ui.note("Klipper is not running — clearing its stored history only")
        self.purge_many([
            self.home / ".local/share/klipper/history2.lst",
            self.home / ".local/share/klipper",
            self.home / ".config/klipperrc",
        ], "Clipboard store")
        # wl-copy resolves WAYLAND_DISPLAY inside XDG_RUNTIME_DIR, so it needs
        # the user's session rather than root's.
        if su.has("wl-copy") and self.user_env.get("WAYLAND_DISPLAY"):
            self.do(["wl-copy", "--clear"], "Cleared Wayland clipboard",
                    as_user=True, needs_session=True, timeout=15)
        elif su.has("wl-copy"):
            ui.note("no Wayland display for this user — skipping wl-copy")

    def trash(self):
        ui.header("Trash")
        for sub in ("files", "info"):
            self.purge(self.home / ".local/share/Trash" / sub, f"Trash/{sub}")

    def temp(self):
        ui.header("Temporary files")
        # systemd-tmpfiles applies the distro's own age rules, which is far
        # safer than deleting /tmp wholesale while sessions are using it.
        if su.has("systemd-tmpfiles"):
            self.do(su.sudo(["systemd-tmpfiles", "--clean"]),
                    "Applied systemd-tmpfiles age policy to /tmp and /var/tmp")
        else:
            ui.note("systemd-tmpfiles unavailable — skipping")

    def orphans(self):
        ui.header("Orphaned data and dangling files")
        # Sandbox homes left behind by flatpaks that are no longer installed.
        var_app = self.home / ".var/app"
        if var_app.is_dir() and su.has("flatpak"):
            res = su.run(["flatpak", "list", "--columns=application"], timeout=60)
            installed = {l.strip() for l in res.out.splitlines() if l.strip()}
            if installed:
                for app in sorted(var_app.iterdir()):
                    if app.is_dir() and app.name not in installed:
                        self.purge(app, f"Orphaned Flatpak data: {app.name}",
                                   contents_only=False)
            else:
                ui.note("could not read the installed Flatpak list — skipping")

        # Desktop entries and binaries pointing at things that no longer exist.
        dangling = []
        for folder in (self.home / ".local/share/applications",
                       self.home / ".local/bin",
                       self.home / ".config/autostart"):
            if not folder.is_dir():
                continue
            for entry in folder.iterdir():
                if entry.is_symlink() and not entry.resolve().exists():
                    dangling.append(entry)
        for entry in dangling:
            self.purge(entry, f"Broken symlink: {entry.name}", contents_only=False)
        if not dangling:
            print(f"  {ui.GREY}no broken symlinks found{ui.RESET}")

        # rpm-ostree keeps overlay/rollback content that cleanup -b already
        # handles; report anything still pinned so you can decide.
        if su.has("rpm-ostree"):
            res = su.run(["rpm-ostree", "status", "--json"], timeout=60)
            if res.ok and '"pinned": true' in res.out.replace(" ", " "):
                ui.note("a pinned deployment exists — it is kept deliberately")

    # ------------------------------------------------------------ removable
    @staticmethod
    def _lsblk() -> list[dict]:
        """Every block device, flattened, with tran/rm/hotplug inherited.

        lsblk reports the transport on the parent disk and leaves it null on
        the partitions, so a USB partition only looks removable once the
        parent's attributes are pushed down onto it.
        """
        res = su.run(["lsblk", "-J", "-o",
                      "NAME,KNAME,PATH,LABEL,PARTLABEL,MOUNTPOINT,RM,HOTPLUG,"
                      "TRAN,FSTYPE,SIZE"], timeout=60)
        if not res.ok:
            return []
        try:
            tree = json.loads(res.out).get("blockdevices", [])
        except (ValueError, AttributeError):
            return []

        flat: list[dict] = []

        def walk(node, parent):
            node = dict(node)
            children = node.pop("children", []) or []
            for key in ("tran", "rm", "hotplug"):
                if node.get(key) in (None, "") and parent:
                    node[key] = parent.get(key)
            flat.append(node)
            for child in children:
                walk(child, node)

        for dev in tree:
            walk(dev, None)
        return flat

    @staticmethod
    def _truthy(value) -> bool:
        """lsblk emits rm/hotplug as JSON booleans on new util-linux and as
        "0"/"1" strings on older ones."""
        return value in (True, 1, "1", "true", "True")

    @classmethod
    def _removable(cls, dev: dict) -> bool:
        return (cls._truthy(dev.get("rm")) or cls._truthy(dev.get("hotplug"))
                or (dev.get("tran") or "").lower() in {"usb", "mmc", "ieee1394"})

    @staticmethod
    def _identifiers(dev: dict) -> set[str]:
        """Every name a user might reasonably type for *dev*."""
        names = {dev.get(k) for k in ("name", "kname", "path", "label",
                                      "partlabel", "mountpoint")}
        mount = dev.get("mountpoint")
        if mount:
            names.add(Path(mount).name)
        for key in ("path", "kname"):
            if dev.get(key):
                names.add(Path(dev[key]).name)
        return {str(n).lower() for n in names if n}

    def _resolve_usb(self, name: str) -> dict | None:
        """Find the mounted removable device the user asked for.

        Matching is deliberately strict — label, device name, device path or
        mount point, nothing fuzzy — because the consequence of a wrong match
        is deleting from the wrong filesystem.
        """
        devices = self._lsblk()
        if not devices:
            ui.warn(f"{name}: could not read the block device list (lsblk)")
            return None
        wanted = name.lower().rstrip("/")
        matches = [d for d in devices if wanted in self._identifiers(d)]
        if not matches:
            ui.warn(f"{name}: no such drive")
            self._list_removable(devices)
            return None
        if len(matches) > 1:
            ui.warn(f"{name}: matches {len(matches)} devices — name it by "
                    "mount point or /dev path instead")
            return None

        dev = matches[0]
        if not self._removable(dev):
            ui.fail(f"{name} ({dev.get('path')}) is not removable media — "
                    "refusing to sweep an internal disk")
            return None
        mount = dev.get("mountpoint")
        if not mount:
            ui.warn(f"{name} is not mounted — mount it first")
            return None
        try:
            resolved = Path(mount).resolve()
        except OSError as exc:
            ui.warn(f"{name}: {mount} is unreadable: {exc}")
            return None
        if str(resolved) in PROTECTED_MOUNTS or resolved == self.home \
                or resolved in self.home.parents:
            ui.fail(f"{name} resolves to {resolved} — refusing to sweep it")
            return None
        dev["mountpoint"] = str(resolved)
        return dev

    @staticmethod
    def _list_removable(devices=None) -> None:
        devices = devices if devices is not None else Cleaner._lsblk()
        found = [d for d in devices
                 if Cleaner._removable(d) and d.get("mountpoint")]
        if not found:
            ui.note("no mounted removable drives detected")
            return
        ui.note("mounted removable drives:")
        for dev in found:
            label = dev.get("label") or dev.get("partlabel") or "(no label)"
            print(f"    {ui.BOLD}{label}{ui.RESET}  {dev.get('path')}  "
                  f"{ui.GREY}{dev.get('mountpoint')}  {dev.get('fstype') or '?'}"
                  f"  {dev.get('size') or ''}{ui.RESET}")

    def _is_junk(self, name: str, is_dir: bool) -> bool:
        if is_dir:
            if name in USB_JUNK_DIRS:
                return True
            globs = USB_JUNK_DIR_GLOBS + (USB_DEEP_DIR_GLOBS if self.deep else ())
        else:
            if name in USB_JUNK_FILES:
                return True
            globs = USB_JUNK_FILE_GLOBS + (USB_DEEP_FILE_GLOBS if self.deep else ())
        return any(fnmatch.fnmatch(name, pattern) for pattern in globs)

    def _sweep(self, root: Path) -> tuple[list[Path], list[Path]]:
        """Collect the junk on *root* without touching anything yet.

        Collecting first keeps the dry run and the real run identical, and lets
        one summary line stand in for the thousands of ._* stubs a macOS copy
        leaves behind.
        """
        try:
            device = root.stat().st_dev
        except OSError as exc:
            ui.warn(f"{root}: {exc}")
            return [], []
        dirs: list[Path] = []
        files: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root, topdown=True,
                                                    onerror=lambda _e: None):
            here = Path(dirpath)
            keep = []
            for entry in dirnames:
                path = here / entry
                if self._is_junk(entry, is_dir=True):
                    dirs.append(path)      # matched: remove it, do not descend
                    continue
                try:
                    # Never follow a symlink off the stick, and never cross
                    # into another filesystem mounted underneath it.
                    if path.is_symlink() or path.stat().st_dev != device:
                        continue
                except OSError:
                    continue
                keep.append(entry)
            dirnames[:] = keep
            for entry in filenames:
                if self._is_junk(entry, is_dir=False):
                    files.append(here / entry)
        return dirs, files

    def _prune_empty(self, root: Path) -> int:
        """Remove directories left empty by the sweep. --deep only."""
        removed = 0
        for dirpath, dirnames, filenames in os.walk(root, topdown=False,
                                                    onerror=lambda _e: None):
            here = Path(dirpath)
            if here == root or dirnames or filenames:
                continue
            if self.dry:
                print(f"  {ui.GREY}would remove empty dir:{ui.RESET} {here}")
                removed += 1
                continue
            try:
                here.rmdir()
                removed += 1
            except OSError:
                pass
        return removed

    def usb(self):
        ui.header("Removable media")
        if not self.usb_names:
            ui.note("no --usb given — skipping (name a drive to sweep it)")
            self._list_removable()
            return
        if not su.has("lsblk"):
            ui.warn("lsblk not available — cannot identify removable media")
            return
        for name in self.usb_names:
            self._clean_usb(name)

    def _clean_usb(self, name: str):
        dev = self._resolve_usb(name)
        if not dev:
            return
        root = Path(dev["mountpoint"])
        label = dev.get("label") or dev.get("partlabel") or dev.get("name")
        print(f"  {ui.BOLD}{label}{ui.RESET} {ui.GREY}{dev.get('path')} "
              f"at {root} ({dev.get('fstype') or '?'}){ui.RESET}")

        res = su.run(["findmnt", "-no", "OPTIONS", str(root)], timeout=30)
        if res.ok and "ro" in res.out.split(",") and not self.dry:
            ui.warn(f"{label} is mounted read-only — remount rw to clean it")
            return
        if not self.dry and not os.access(root, os.W_OK):
            ui.warn(f"{label}: no write access to {root} — skipping")
            return

        dirs, files = self._sweep(root)
        if not dirs and not files:
            ui.ok(f"{label}: already clean — no foreign metadata found")
        else:
            before = self.freed
            for path in dirs:
                # purge() leaves an already-empty directory alone, which would
                # strand a bare "System Volume Information" on the drive; the
                # point here is that the folder itself goes.
                try:
                    empty = not any(path.iterdir())
                except OSError:
                    empty = False
                if empty and not self.dry:
                    try:
                        path.rmdir()
                        ui.ok(f"{label}: {self._rel(path, root)} (empty)")
                    except OSError as exc:
                        ui.warn(f"{label}: {self._rel(path, root)}: {exc}")
                elif empty:
                    print(f"  {ui.GREY}would clear:{ui.RESET} {path} (empty)")
                else:
                    self.purge(path, f"{label}: {self._rel(path, root)}",
                               contents_only=False)
            # Thousands of ._* stubs would drown the output one line each, so
            # account for them in bulk and report a single figure.
            size = gone = 0
            for path in files:
                try:
                    nbytes = path.lstat().st_size
                except OSError:
                    continue
                if not self.dry:
                    try:
                        path.unlink()
                    except OSError as exc:
                        ui.warn(f"{label}: {self._rel(path, root)}: {exc}")
                        continue
                size += nbytes
                gone += 1
            self.freed += size
            verb = "would remove" if self.dry else "Removed"
            ui.ok(f"{label}: {verb} {gone} junk file(s) "
                  f"({su.human(size)}) and {len(dirs)} junk folder(s)")
            self.usb_freed += self.freed - before

        if self.deep:
            empty = self._prune_empty(root)
            if empty:
                ui.ok(f"{label}: {'would remove' if self.dry else 'removed'} "
                      f"{empty} empty director{'y' if empty == 1 else 'ies'}")
        if not self.dry:
            # Flush the writes so the stick is safe to pull straight away.
            su.run(["sync", "-f", str(root)], timeout=300)

    @staticmethod
    def _rel(path: Path, root: Path) -> str:
        try:
            return str(path.relative_to(root))
        except ValueError:
            return str(path)

    def trim(self):
        ui.header("SSD trim")
        self.do(su.sudo(["fstrim", "-av"]), "Trimmed mounted filesystems")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Stages: " + ", ".join(STAGES))
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="report what would be removed without deleting anything")
    ap.add_argument("-s", "--stages", default="all",
                    help="comma-separated subset of stages (default: all)")
    ap.add_argument("--deep", action="store_true",
                    help="also clear cookies, history and container volumes "
                         "(signs you out of websites)")
    ap.add_argument("--purge-shaders", action="store_true",
                    help="also delete GPU shader caches (costs frame rate until rebuilt)")
    ap.add_argument("--usb", action="append", metavar="NAME",
                    help="deep-clean a mounted removable drive, named by its "
                         "label, device (sdb1), /dev path or mount point; "
                         "repeat for several drives")
    ap.add_argument("--list-usb", action="store_true",
                    help="list the mounted removable drives and exit")
    args = ap.parse_args()

    if args.list_usb:
        Cleaner._list_removable()
        return 0

    wanted = STAGES if args.stages == "all" else tuple(
        s.strip() for s in args.stages.split(",") if s.strip())
    if args.usb and "usb" not in wanted:
        wanted += ("usb",)
    unknown = [s for s in wanted if s not in STAGES]
    if unknown:
        ui.fail(f"unknown stage(s): {', '.join(unknown)}")
        ui.note("available: " + ", ".join(STAGES))
        return 2

    if not su.is_root() and not args.dry_run:
        ui.warn("not root — system-wide steps will prompt for sudo")

    mount = su.real_storage_mount()
    before = shutil.disk_usage(mount).free
    print(f"{ui.BOLD}{ui.CYAN}Bazzite deep cleaner{ui.RESET}"
          f"{'  (dry run — nothing will be deleted)' if args.dry_run else ''}")
    print(f"{ui.GREY}free-space baseline taken on {mount}{ui.RESET}")

    cleaner = Cleaner(args)
    for stage in wanted:
        try:
            getattr(cleaner, stage)()
        except Exception as exc:                      # one bad stage must not
            ui.fail(f"stage {stage} failed: {exc}")   # abort the whole run

    after = shutil.disk_usage(mount).free
    ui.header("Summary")
    if args.dry_run:
        print(f"  Reclaimable by deletion: {ui.BOLD}{su.human(cleaner.freed)}{ui.RESET}")
        print(f"  {ui.GREY}(package-manager steps reclaim more on top of this){ui.RESET}")
        print(f"  {ui.GREY}re-run without --dry-run to apply{ui.RESET}")
    else:
        print(f"  Free before : {su.human(before)}")
        print(f"  Free after  : {su.human(after)}")
        print(f"  {ui.GREEN}{ui.BOLD}Reclaimed: {su.human(max(0, after - before))}{ui.RESET}")
    if cleaner.usb_freed:
        # Removable media is not on the volume the baseline was taken from, so
        # its total would otherwise be invisible above.
        print(f"  {ui.GREY}(of which {su.human(cleaner.usb_freed)} on removable "
              f"media, measured separately){ui.RESET}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        ui.warn("interrupted")
        sys.exit(130)
