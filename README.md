# pyBazzite — System Utility Suite for Bazzite / Atomic Fedora

A set of maintenance, inspection and security tools for [Bazzite](https://bazzite.gg/)
and other ostree-based atomic Fedora systems (Silverblue, Kinoite, Bluefin).

Atomic distributions behave differently from ordinary Linux: `/` is a read-only
deployment, packages are layered rather than installed, and most applications
live in Flatpak sandboxes or rootless containers. Tools written for traditional
Fedora quietly report the wrong thing here. These are written for the atomic
model specifically.

Every tool prints colour-coded tables, degrades gracefully when an optional
dependency is missing, and never changes anything without saying so first.

---

## Requirements

**Python 3.9+.** Nothing else is required to run the core tools — they shell out
to utilities that already ship with Bazzite (`rpm-ostree`, `flatpak`, `systemctl`,
`ss`, `nmcli`, `firewall-cmd`, `bluetoothctl`, `lspci`, `lsusb`).

Five tools need a Python package. Each one checks at startup and tells you
exactly what to install rather than crashing:

| Package | Needed by |
| :--- | :--- |
| `psutil` | `bzt_conn.py`, `bzt_conn_daemon.py` |
| `requests`, `beautifulsoup4` | `bzt_urlaudit.py` |
| `scapy` | `bzt_wifiguard.py` |
| `speedtest-cli` | `bzt_sysinfo.py --speedtest` (optional) |
| `pillow`, `pypdf`, `mutagen` | `bzt_scrub.py`, per format — see below |

`bzt_scrub.py` handles JPEG, PNG, WebP, GIF and every ZIP-based office format
with the standard library alone; the packages above only widen it to TIFF-class
images, PDFs and audio. Video additionally needs **ffmpeg**, which is a system
package rather than a pip one:

```bash
rpm-ostree install ffmpeg     # layered onto the base image, needs a reboot
```

Without it, video files are skipped with a note (except MP4-family containers,
where `mutagen` can still reach the tag atoms).

---

## Installation

```bash
git clone https://github.com/y-v-j/pyBazzite.git
cd pyBazzite
chmod +x bzt_*.py
```

Install the optional Python dependencies you want. On an atomic system, use
`--user` so nothing touches the base image:

```bash
pip install --user -r requirements.txt
```

Optionally put the tools on your `PATH`:

```bash
mkdir -p ~/.local/bin
for f in "$PWD"/bzt_*.py; do ln -sf "$f" ~/.local/bin/"$(basename "$f" .py)"; done
```

You can then run `bzt_ports`, `bzt_clean`, and so on from anywhere.

> **Note on `sudo`:** the tools call `sudo` themselves only for the specific
> steps that need it. Do **not** run the whole script with `sudo` unless a tool
> tells you to — several operations are user-scoped and misbehave as root.
> See *Why not just run everything with sudo?* below.

---

## The tools

| Script | What it does | Needs root? |
| :--- | :--- | :--- |
| `bzt_clean.py` | Deep-clean caches, logs, orphans, clipboard, browser data and USB sticks | partly (prompts) |
| `bzt_scrub.py` | Strip metadata from images, video, documents and audio under a directory, recursively | no |
| `bzt_hardware.py` | Hardware inventory and which kernel driver owns each device | no |
| `bzt_inventory.py` | Conda envs, Flatpaks, network profiles, deployments, containers | no |
| `bzt_services.py` | List systemd units, or manage them in an interactive TUI | for changes |
| `bzt_sysinfo.py` | RAM, storage, latency and optional bandwidth | no |
| `bzt_ports.py` | What is listening, and how far each socket is exposed | better with |
| `bzt_conn.py` | Live connection audit with risk assessment | better with |
| `bzt_conn_daemon.py` | Background connection logger | no |
| `bzt_conn_log.py` | Renders the daemon's log | no |
| `bzt_firewall.py` | **Guided firewall checkup in plain English** | for changes |
| `bzt_recon.py` | Devices on your LAN and paired/nearby Bluetooth | no |
| `bzt_btwatch.py` | Passive Bluetooth presence log, reporting every few hours | no |
| `bzt_authmon.py` | Live sudo / SSH / login event monitor | better with |
| `bzt_urlaudit.py` | Website security, tracker and privacy audit | no |
| `bzt_wifiguard.py` | Deauth-attack and rogue-AP detection | yes + monitor mode |

---

## Usage

### `bzt_clean.py` — deep system clean

Reclaims space across every layer an atomic system accumulates it: pending
ostree deployments, unused Flatpak runtimes, dangling container images,
developer tool caches, browser and DNS caches, recent-file and clipboard
history, orphaned data from uninstalled apps, and broken symlinks. Named
removable drives are swept of foreign-OS residue on request.

**Always preview first.** `--dry-run` shows every path and the space it holds
without deleting anything.

```bash
./bzt_clean.py --dry-run                    # see what would go (safe)
./bzt_clean.py                              # run every stage
./bzt_clean.py -s cache,browser,trash       # run selected stages only
./bzt_clean.py --deep                       # also cookies, history, volumes
./bzt_clean.py --purge-shaders              # also GPU shader caches
./bzt_clean.py --list-usb                   # which removable drives are mounted
./bzt_clean.py --usb MYSTICK --dry-run      # preview a USB clean
```

Stages: `ostree`, `flatpak`, `podman`, `devcache`, `journal`, `cache`,
`browser`, `dns`, `recent`, `clipboard`, `trash`, `temp`, `orphans`, `usb`,
`trim`.

Two things are deliberately **kept** by default:

- **GPU shader caches** — deleting them costs you frame rate until every game
  rebuilds them. `--purge-shaders` overrides.
- **The rollback deployment** — `rpm-ostree cleanup -r` would remove your
  ability to recover from a bad update, so it is never run automatically.

Cookies and browsing history are only touched with `--deep`, which signs you
out of every website. Running browsers are skipped rather than corrupted.

#### Cleaning a USB stick

`--usb` sweeps a mounted removable drive of the residue other operating systems
leave on it — the trash cans, search indexes and per-folder view settings that
accumulate every time the stick is plugged into a different machine:

| Removed by default | From |
| :--- | :--- |
| `.Trashes`, `.Spotlight-V100`, `.fseventsd`, `.DS_Store`, `._*` | macOS |
| `$RECYCLE.BIN`, `System Volume Information`, `Thumbs.db`, `desktop.ini` | Windows |
| `.Trash-1000`, `.directory`, `.thumbnails` | Linux desktops |

With `--deep` it also takes `*.tmp`, `~$*` office lock files, `*.part` and
`*.crdownload` part-downloads, chkdsk `found.NNN` folders, and any directory
left empty afterwards.

```bash
./bzt_clean.py --usb MYSTICK                # by filesystem label
./bzt_clean.py --usb sdb1                   # by device name
./bzt_clean.py --usb /run/media/you/MYSTICK # by mount point
./bzt_clean.py --usb ONE --usb TWO --deep   # several drives, aggressively
```

Name a drive however you like — label, `sdb1`, `/dev/sdb1` or its mount point.
The match is exact rather than fuzzy, because the cost of matching the wrong
device is deleting from the wrong filesystem. It **refuses** to run on anything
that is not removable, on an unmounted drive, on an ambiguous name, and on any
system mount — including a stick someone has mounted over `/home`, which on an
ostree system resolves to `/var/home`. Symlinks are never followed off the
drive and nested mounts are never descended into.

Your files are not touched: only the metadata and trash listed above is
removed. Removable media is not on the volume the free-space baseline was taken
from, so its total is reported separately in the summary.

### `bzt_scrub.py` — strip metadata from a directory tree

Files carry identifying information most people never see: the camera serial
number and GPS coordinates in a photo, the phone model and filming location in
a video, the author and review history in a Word document, the editor that
produced a PDF, and — on Linux — the exact URL a download came from, recorded
in an extended attribute that travels with the file onto a USB stick.

Point it at a directory and it walks the whole tree, subfolders included.

```bash
./bzt_scrub.py --dry-run ~/Pictures/holiday   # see what is there (safe)
./bzt_scrub.py ~/Pictures/holiday             # strip it (asks first)
./bzt_scrub.py -y -c image ~/to-publish       # images only, no prompt
./bzt_scrub.py -b ~/Documents                 # keep <name>.orig backups
./bzt_scrub.py --include '*.jpg' ~/Photos     # only matching files
./bzt_scrub.py -c video ~/Videos               # video only
./bzt_scrub.py -c image,video,pdf,office,audio,xattr,times ~/share
```

| Category | What goes |
| :--- | :--- |
| `image` | EXIF (camera, serial, GPS), XMP, IPTC, PNG text chunks, GIF comments, TIFF tags, data appended after the end of the image |
| `video` | Container tags (title, artist, comment, encoder), the QuickTime GPS and device-model atoms, creation times, chapters, and per-stream handler names — see below |
| `pdf` | The `/Info` dictionary, the XMP packet, per-page XMP and editor state |
| `office` | `docProps/core.xml`, `app.xml`, `custom.xml`, ODF `meta.xml`, and the modification times stored inside the archive |
| `audio` | Every tag frame — ID3, Vorbis comments, MP4 atoms |
| `xattr` | `user.*` extended attributes, including `user.xdg.origin.url` |
| `times` | Modification and access times, set to 1980-01-01 |

Default: everything except `times`, which is opt-in — a modification date is
usually information the owner wants, unlike a GPS tag nobody asked to be there.

**Your files are not re-encoded.** JPEG, PNG, WebP and GIF are edited at the
byte level: the compressed image data is copied through untouched, so a photo
loses its EXIF without losing a single pixel and an animated GIF keeps every
frame and its loop. Only formats with no safe byte-level edit (TIFF, BMP, HEIC)
go through Pillow, and a multi-page one is skipped rather than flattened.

#### Video, and keeping your subtitles

Video is remuxed with `ffmpeg -map 0 -c copy`: every track is carried across and
nothing is re-encoded, so a two-hour film is rewritten in seconds at exactly its
original quality. All the common containers are covered — MP4, MKV, WebM, MOV,
AVI, WMV/ASF, FLV, OGV, 3GP, MPEG-TS/PS, MXF and the rest; MPEG-TS and MPEG-PS
have nowhere to store metadata in the first place, so they come back clean.

**Subtitles are kept.** Clearing a container's metadata wholesale would take the
subtitle tracks' language and title tags with it — and in Matroska it removes an
attached font's `filename` tag, which makes the file refuse to mux at all. So
the metadata is cleared and only what a player actually needs is written back:

| Kept | Removed |
| :--- | :--- |
| Every stream — video, audio, **all subtitle tracks**, attached fonts | Title, artist, comment, description, copyright |
| Subtitle and audio `language` and `title` tags | GPS location and `make` / `model` device atoms |
| Attachment `filename` and `mimetype` (fonts for ASS subtitles) | Creation and encoding timestamps |
| Display rotation, so phone video does not play sideways | Chapters, and identifying per-stream handler names |

Two safety checks run before the rewrite is installed. The new file must have
the **same number of streams** as the original — a remux that quietly dropped a
subtitle track is discarded — and it must still be the **same container format**,
because ffmpeg picks its muxer from the file extension and would otherwise
convert a mislabelled `.rm` that is really a QuickTime file into actual
RealMedia. Either check failing leaves the original untouched and reports why.

> **One thing this cannot remove:** the encoder's own signature inside the
> compressed video stream — x264 and friends write their version and settings
> into an SEI header. Getting rid of it means either re-encoding the whole film
> or discarding every SEI, which would also discard HDR mastering data. The
> container metadata that identifies *you* — location, device, author, times —
> is removed; the fact that some version of x264 encoded it is not.

`--media-timeout` (default 1800s) caps how long a single remux may take; raise
it for very large files.

Other properties worth knowing:

- **Nothing is written without asking.** `--dry-run` reports what it found and
  changes nothing; without it you get one confirmation prompt (`-y` skips it).
- **Rewrites are atomic.** The scrubbed copy is written beside the original and
  renamed over it, so an interruption cannot leave a half-written file.
- **Permissions, ownership, SELinux labels and ACLs are preserved**, and the
  original modification time is put back unless `times` asked for it.
- **Running it twice is a no-op** — the second pass reports a clean tree.
- **`--strip-color-profiles`** additionally removes embedded ICC profiles. It is
  off by default because that changes how images render on calibrated displays.
- Symlinks are never followed, dot-directories are skipped unless `--hidden`,
  and system directories are refused outright.

> **This cannot be undone.** Metadata you want to keep — a photo's capture date,
> your music library's artist tags — is gone once stripped. Preview with
> `--dry-run`, narrow with `-c`, or keep originals with `--backup`.

### `bzt_firewall.py` — guided firewall checkup

Written for non-experts. Run it with no arguments and it explains, in plain
English, what each opening in your firewall is for and whether it matters,
then offers to close the ones you do not need — one at a time, with an undo
command printed for each.

```bash
./bzt_firewall.py              # guided checkup (asks before every change)
./bzt_firewall.py --check      # read-only report, changes nothing
./bzt_firewall.py --harden     # close everything rated "worth closing"
./bzt_firewall.py --expert     # show the advanced port-level options
```

Nothing is changed without an explicit `y`.

### `bzt_ports.py` / `bzt_conn.py` — what is reachable

```bash
./bzt_ports.py                 # every listening socket
./bzt_ports.py -e              # only what is reachable from the network
sudo ./bzt_ports.py            # include process names for all users
./bzt_conn.py -r               # live connections, risky ones only
```

`bzt_ports.py` names the process behind each socket and prints the exact
`bzt_firewall.py` command to close anything exposed.

### `bzt_conn_daemon.py` / `bzt_conn_log.py` — connection logging

```bash
./bzt_conn_daemon.py --once            # single snapshot
./bzt_conn_daemon.py --start           # run in the background
./bzt_conn_daemon.py --status          # is it running?
./bzt_conn_daemon.py --stop            # stop it
./bzt_conn_log.py                      # view the log
./bzt_conn_log.py --risky-only         # only flagged entries
```

Log lives at `~/.local/state/bzt/network_audit.json`. Use `--no-dns` to skip
reverse lookups, which is faster and sends nothing to your resolver.

### `bzt_btwatch.py` — passive Bluetooth presence log

Records which Bluetooth devices are around you over time and reports every
couple of hours. It is **observation-only**: it never connects, pairs, trusts
or probes anything. LE mode (the default) purely listens for the advertisements
devices already broadcast.

```bash
./bzt_btwatch.py                       # one scan, then show the report
./bzt_btwatch.py --install-timer       # scan every 2h via a systemd user timer
./bzt_btwatch.py --report              # show the accumulated report
./bzt_btwatch.py --report --since 24   # only devices seen in the last day
./bzt_btwatch.py --interval 4 --install-timer   # every 4 hours instead
```

The report separates **regulars** (seen in most scans — likely resident
devices) from **one-off sightings** (passers-by). Modern phones rotate their
MAC addresses for privacy, so one handset often appears as several short-lived
entries — this is expected, not a fault.

`--classic` additionally runs BR/EDR inquiry, which *does* transmit, so it is
opt-in rather than the default.

Manage the timer with:

```bash
systemctl --user list-timers bzt-btwatch.timer
systemctl --user disable --now bzt-btwatch.timer
```

### `bzt_services.py` — systemd units

```bash
./bzt_services.py                      # table of all system units
./bzt_services.py --state failed       # only failed units
./bzt_services.py --user               # user units
./bzt_services.py --tui                # interactive manager
```

In the TUI: arrow keys and PgUp/PgDn to move, `S` start, `X` stop, `R` restart,
`L` reload the list, `Q` quit. Privileged actions prompt for `sudo` at the
moment you take them.

### `bzt_sysinfo.py`, `bzt_hardware.py`, `bzt_inventory.py` — inspection

```bash
./bzt_sysinfo.py                       # RAM, storage, latency
./bzt_sysinfo.py --speedtest           # also measure bandwidth (slow)
./bzt_hardware.py                      # devices and their drivers
./bzt_hardware.py --all                # include every PCI device
./bzt_inventory.py                     # envs, flatpaks, deployments
./bzt_inventory.py --packages          # also list conda package contents
./bzt_inventory.py --only deployments  # one section
```

### `bzt_recon.py` — local network inventory

```bash
./bzt_recon.py                         # read the existing ARP table
./bzt_recon.py --sweep                 # actively ping your subnet
./bzt_recon.py --bt-seconds 10         # also scan for Bluetooth
./bzt_recon.py --vendors               # identify hardware makers
```

`--vendors` sends MAC prefixes to `api.macvendors.com`, so it is opt-in.
Results are cached in `~/.cache/bzt/oui.json` and rate-limited to the one
request per second that the free tier allows.

### `bzt_authmon.py` — authentication monitor

```bash
sudo ./bzt_authmon.py                  # follow live events
sudo ./bzt_authmon.py --no-follow -n 200   # review the last 200 log lines
```

Tracks sudo use and failures, SSH attempts, and desktop logins. Prints a
summary table on exit. Without `sudo` you see only your own events.

### `bzt_urlaudit.py` — website audit

```bash
./bzt_urlaudit.py github.com
./bzt_urlaudit.py --no-geo example.com     # skip third-party IP geolocation
```

Reports HTTPS, HSTS, CSP, X-Frame-Options, cookies set on load, known
trackers, paywall signatures, login forms and sensor permission requests.

### `bzt_wifiguard.py` — Wi-Fi attack detection

Detects deauthentication floods and rogue access points broadcasting a network
name that already belongs to another radio.

**Requires monitor mode.** On a normal managed interface it sees no 802.11
management frames at all and will appear to show a perfectly quiet network:

```bash
sudo ip link set wlan0 down
sudo iw dev wlan0 set type monitor
sudo ip link set wlan0 up

sudo ./bzt_wifiguard.py wlan0 -q       # alerts only
sudo ./bzt_wifiguard.py wlan0 -t 300   # run for five minutes

sudo ip link set wlan0 down            # restore afterwards
sudo iw dev wlan0 set type managed
sudo ip link set wlan0 up
```

The tool refuses to start if the interface is not in monitor mode, rather than
running silently and implying all is well.

> Monitor only networks you own or are authorised to test. Capturing other
> people's wireless traffic may be illegal where you live.

---

## History: fixes over the original scripts

pyBazzite is a rewrite of an earlier collection of 21 standalone Python
scripts, which are not included in this repository. They became 15 tools plus
a shared `bzt/` package, with `bzt_scrub.py` added since. Beyond removing
duplicated code, the rewrite fixed these defects (the *Where* column names the
original script):

| Defect | Effect | Where |
| :--- | :--- | :--- |
| `ujust clean-system` run as root | Homebrew refuses to run as root and is the recipe's last command, so it exited 1 every time | `atomicity.py` |
| `runuser` handed root's `XDG_RUNTIME_DIR` | Under `sudo`, pam_systemd points it at root's 0700 `/run/user/0`; dropping to the user left it there, so `just`, rootless podman, `wl-copy` and the session bus all failed with `Permission denied` | `bzt_clean.py` |
| `lspci -nnk` split on blank lines | That output separates devices by *indentation*, so GPU and audio detection never worked at all | both `bazzite_hardware*.py` |
| `journalctl -u ... -t ...` | journalctl ANDs matches on different fields, so the filter could never match a line and the monitor stayed silent forever | `bazzite_auth_monitor.py` |
| `COLOR_YELLOW` never defined | `NameError` crash whenever any device had an unbound driver | `bazzite_hardware.py` |
| `BeautifulSoup` imported outside the dependency guard | Raw `ImportError` before the helpful install message could print | `bazzite_url_checker.py` |
| `--remove-port` described as "blocking" | Only withdraws an *allow* rule; fails if the port was never opened, and does nothing when traffic arrives via an allowed service | `bazzite_block_ports.py` |
| Hardcoded `--zone=public` | Bazzite's default zone is normally `FedoraWorkstation`, so the rules targeted the wrong zone; the active zone is now detected | `bazzite_block_ports.py` |
| `elif remote_port == 80` after `if remote_port in UNSAFE_PORTS` | Unreachable — 80 is already in that dict | `bazzite_firewall.py` |
| Listening ports never risk-checked | A host listening on SMB/445 was reported "secure" | `bazzite_firewall.py` |
| `free + buffers + cached` as available RAM | Overstates free memory; `MemAvailable` is the kernel's own figure | `bazzite_speedtest.py` |
| `disk_usage("/")` on an atomic system | `/` is a read-only 55 MB overlay that always reports ~100% full | `bazzite_speedtest.py` |
| Single `fork()` with no `setsid()` | The "daemon" died with its parent shell | `bazzite_monitor_daemon.py` |
| Non-atomic JSON writes | The viewer could read a half-written file | `bazzite_monitor_daemon.py` |
| `gethostbyaddr` with no timeout | One unreachable resolver stalled every poll cycle | `bazzite_monitor_daemon.py` |
| `conda_prefix` key | `conda env list --json` never emits it, so no environment was marked active | `bazzite_details.py` |
| `conda list -n <name>` | Fails for path-only environments; `-p <prefix>` works for both | `bazzite_details.py` |
| Scraping `●` from `rpm-ostree status` | Broke on any non-UTF-8 terminal; `--json` is stable | `bazzite_details.py` |
| `nmcli -t` split on every `:` | Connection names containing a colon corrupted the row | `bazzite_details.py` |
| Loopback check `== "127.0.0.1"` | Missed `127.0.0.53` (systemd-resolved), reporting it as LAN-facing | `bazzite_port_recon.py` |
| `os.system()` for systemctl | Reports a wait status, not the exit code, and passes the unit name through a shell | `bazzite_services_02.py` |
| `addstr` without clipping | curses raised and crashed the TUI on narrow terminals | `bazzite_services_02.py` |
| `shutil.rmtree` without `ignore_errors` | One unreadable file aborted the whole clean | `bazzite_cleaner.py` |
| `os.listdir` on a missing `~/.cache` | `FileNotFoundError` crash | `bazzite_cleaner.py` |
| `expanduser("~")` under sudo | Resolves to `/root`, so it cleaned root's cache instead of yours | `bazzite_cleaner.py` |
| Hardcoded `/home/<user>` | On ostree systems homes live in `/var/home`; the path is now read from the passwd database | `atomicity.py` |
| `shell=True` with an interpolated username | Command-injection surface; everything now uses argument lists | `atomicity.py` |
| Corrupted box-drawing characters | Every table border rendered as mojibake (`â`, `â¦`) from a UTF-8/Latin-1 mix-up | most scripts |
| Per-device MAC vendor API calls | Rate-limited to ~1/sec, so nearly every lookup returned nothing | both `bazzite_recon*.py` |
| Unguarded `scapy` import; no monitor-mode check | Crashed without guidance, or ran silently on a managed interface and implied the network was quiet | `bazzite_wifi_guard.py` |

**Dependency removed:** `jeepney` is no longer needed. `bluetoothctl` ships
with BlueZ and is more reliable than hand-rolled D-Bus `ObjectManager` calls.

---

## Why not just run everything with `sudo`?

This is the single most common way to break things on an atomic system, and it
is what caused the `ujust clean-system` failure in the original scripts.

Several subsystems are **user-scoped**: rootless podman storage, per-user
Flatpak installations, Homebrew, and systemd `--user` units. Running them as
root either operates on root's copy of that data — cleaning the wrong thing —
or refuses outright. Homebrew aborts with an explicit error when invoked as
root, and because it is the last command in Bazzite's `clean-system` recipe,
its exit code becomes the whole recipe's exit code.

These tools therefore call `sudo` per operation, and drop back to your own
account via `runuser` for anything user-scoped.

Dropping privileges is not enough on its own, though, and this is the subtle
part. `runuser` passes the environment it was given straight through, and under
`sudo` that environment has `XDG_RUNTIME_DIR=/run/user/0` — root's own runtime
directory, mode 0700. Every user-scoped tool then tries to use it as *you* and
fails: `just` cannot write its scratch directory, rootless podman cannot create
`libpod`, `wl-copy` cannot find the Wayland socket, and `qdbus` talks to root's
session bus instead of yours. `bzt_clean.py` rebuilds `HOME`, `XDG_*` and
`DBUS_SESSION_BUS_ADDRESS` for the real user before handing the command over,
and skips the step with an explanation when that user has no session at all.

---

## Privacy notes

Three tools touch data worth being deliberate about:

- **`bzt_recon.py --vendors`** sends MAC address prefixes to a third-party API.
  Opt-in; omit the flag and nothing leaves your machine.
- **`bzt_btwatch.py`** records Bluetooth devices in radio range, which will
  include hardware belonging to neighbours and passers-by. The log stays local
  at `~/.local/state/bzt/bluetooth_presence.json`; delete it whenever you like.
- **`bzt_urlaudit.py`** performs an IP geolocation lookup against `ip-api.com`
  by default. `--no-geo` disables it.

`bzt_scrub.py` works the other way round — it exists to remove the metadata the
other tools would only report on. Nothing it reads leaves your machine.

---

## Layout

```
pyBazzite/
├── LICENSE
├── README.md
├── requirements.txt
├── bzt/                    shared package — no duplicated code between tools
│   ├── ui.py               colours, self-sizing tables, streaming tables
│   ├── sysutil.py          command running, privilege handling, atomic writes
│   └── netrisk.py          shared connection risk rules
└── bzt_*.py                the 16 tools
```

`bzt/ui.py` renders real UTF-8 box drawing and falls back to ASCII when the
terminal cannot encode it. Colour is disabled automatically when output is
piped, and honours `NO_COLOR`.

---

## License

pyBazzite is released under the [MIT License](LICENSE).

You may use, copy, modify, merge, publish, distribute, sublicense and sell
copies of this software, provided the copyright notice and permission notice
are included. The software is provided **as is**, without warranty of any
kind — in particular, `bzt_clean.py` and `bzt_scrub.py` delete data, so
preview with `--dry-run` first.

The optional dependencies keep their own licences (`scapy` is GPL-2.0,
the rest are permissive); pyBazzite only imports them and does not
redistribute them.
