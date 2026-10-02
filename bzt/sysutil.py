"""Process, privilege and filesystem helpers for atomic Fedora hosts.

Everything here avoids `shell=True`. Commands are passed as argument lists so
a username or unit name can never be interpreted as shell syntax.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class Result:
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


def has(cmd: str) -> bool:
    """True if *cmd* exists on PATH."""
    return shutil.which(cmd) is not None


def run(cmd: Sequence[str], timeout: int = 120, env=None) -> Result:
    """Run *cmd* without a shell and capture its output.

    Never raises: transport-level problems come back as a non-zero Result so
    callers can decide what is fatal.
    """
    try:
        proc = subprocess.run(list(cmd), capture_output=True, text=True,
                              timeout=timeout, env=env)
        return Result(proc.returncode, proc.stdout.strip(), proc.stderr.strip())
    except FileNotFoundError:
        return Result(127, "", f"{cmd[0]}: command not found")
    except subprocess.TimeoutExpired:
        return Result(124, "", f"{cmd[0]}: timed out after {timeout}s")
    except OSError as exc:
        return Result(1, "", str(exc))


def is_root() -> bool:
    return os.geteuid() == 0


@dataclass(frozen=True)
class Target:
    name: str
    uid: int
    gid: int
    home: Path


def target_user() -> Target | None:
    """The human behind the session, even when running under sudo.

    The home directory comes from the passwd database rather than an assumed
    /home/<name>: on ostree systems real homes live in /var/home, and /home is
    only a symlink that may not exist inside every container or chroot.
    """
    name = os.environ.get("SUDO_USER") or os.environ.get("USER") or ""
    if not name or name == "root":
        try:
            name = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            return None
    try:
        pw = pwd.getpwnam(name)
    except KeyError:
        return None
    return Target(pw.pw_name, pw.pw_uid, pw.pw_gid, Path(pw.pw_dir))


def as_user(user: Target, cmd: Sequence[str]) -> list[str]:
    """Wrap *cmd* so it runs as *user* rather than root.

    Needed for anything user-scoped — per-user flatpaks, rootless podman,
    Homebrew — which misbehaves or refuses outright when run as root.
    """
    if not is_root() or user.uid == os.geteuid():
        return list(cmd)
    if has("runuser"):
        return ["runuser", "-u", user.name, "--", *cmd]
    return ["sudo", "-u", user.name, "--", *cmd]


def sudo(cmd: Sequence[str]) -> list[str]:
    """Prepend sudo only when we are not already root."""
    return list(cmd) if is_root() else ["sudo", *cmd]


def is_atomic() -> bool:
    """True on an ostree-booted system (Bazzite, Silverblue, Kinoite...)."""
    return Path("/run/ostree-booted").exists()


def write_json(path: Path, data) -> None:
    """Atomically replace *path* so readers never observe a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def dir_size(path: Path) -> int:
    """Total size of *path* in bytes, ignoring anything unreadable."""
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def human(nbytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024 or unit == "TB":
            return f"{int(nbytes)} B" if unit == "B" else f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def real_storage_mount() -> str:
    """The mount point that actually holds user data.

    On an atomic system `/` is a read-only ostree deployment whose free space
    figure is meaningless; the writable btrfs volume is mounted at /var.
    """
    return "/var" if is_atomic() and Path("/var").is_dir() else "/"


def daemonize() -> None:
    """Detach from the controlling terminal (double fork + setsid).

    A single fork leaves the child in the shell's process group, so it dies
    when the terminal closes.
    """
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.chdir("/")
    os.umask(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
