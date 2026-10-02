#!/usr/bin/env python3
"""Recursively strip metadata from every file under a directory.

Point it at a folder and it walks the whole tree, removing the identifying
information that files carry around invisibly: camera serial numbers and GPS
coordinates in photos, author names and edit history in office documents,
producer strings in PDFs, tags in audio, and the extended attributes Linux uses
to record where a downloaded file came from.

The common formats are handled byte-for-byte with nothing but the standard
library, so a JPEG is never re-encoded and a PNG never loses a pixel. Formats
that genuinely need a parser use an optional dependency, and the tool tells you
which one rather than silently skipping the file.

Nothing is written without --dry-run being offered first, and every rewrite is
atomic: the original is replaced only once its scrubbed copy is complete.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from bzt import sysutil as su
from bzt import ui

CATEGORIES = ("image", "video", "pdf", "office", "audio", "xattr", "times")
# `times` is opt-in: a modification date is often information the owner wants,
# unlike a GPS tag nobody asked to be there.
DEFAULT_CATEGORIES = ("image", "video", "pdf", "office", "audio", "xattr")

# Refusing these keeps a mistyped path from rewriting the operating system.
PROTECTED_DIRS = {"/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64",
                  "/proc", "/run", "/sbin", "/srv", "/sys", "/usr", "/var",
                  "/var/home", "/home", "/sysroot", "/ostree"}

IMAGE_EXT = {".jpg", ".jpeg", ".jpe", ".png", ".webp", ".tif", ".tiff",
             ".gif", ".bmp", ".heic", ".heif", ".avif"}
PDF_EXT = {".pdf"}
OFFICE_EXT = {".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".pptm",
              ".odt", ".ods", ".odp", ".odg", ".odf"}
AUDIO_EXT = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".m4b", ".aac",
             ".wav", ".wma", ".ape", ".wv", ".aiff", ".aif", ".mpc", ".spx",
             ".tta", ".ac3", ".dts", ".dsf", ".dff"}
# Every container ffmpeg can read. The ones it cannot write back out fail the
# remux and are reported and skipped, leaving the original untouched.
VIDEO_EXT = {".mp4", ".m4v", ".mov", ".qt", ".mqv", ".mkv", ".mk3d", ".webm",
             ".avi", ".divx", ".wmv", ".asf", ".flv", ".f4v", ".f4p",
             ".ogv", ".ogm", ".ogx", ".mpg", ".mpeg", ".mpe", ".mpv", ".m1v",
             ".m2v", ".m2p", ".ts", ".m2ts", ".mts", ".m2t", ".tsv", ".vob",
             ".mod", ".tod", ".vro", ".3gp", ".3g2", ".3gpp", ".3gp2",
             ".rm", ".rmvb", ".mxf", ".dv", ".dif", ".y4m", ".amv", ".nsv",
             ".svi", ".wtv", ".dvr-ms", ".gxf", ".ivf", ".bik", ".roq",
             ".mng", ".mjpeg", ".mjpg", ".viv", ".yuv"}
# Format-level tags that identify the container rather than describe the film.
VIDEO_FORMAT_IGNORE = {"major_brand", "minor_version", "compatible_brands"}
# Stream tags that are statistics or structure, regenerated on every mux.
VIDEO_STREAM_IGNORE = {"duration", "bps", "number_of_frames", "number_of_bytes",
                       "source_id", "statistics_writing_app",
                       "statistics_writing_date_utc", "statistics_tags"}
# Kept on every non-video stream: this is what makes a subtitle track usable —
# which language it is, what it is called, and for an attached font, the
# filename and MIME type the renderer matches on. Matroska refuses to mux an
# attachment whose filename tag has been stripped.
VIDEO_KEEP_TAGS = {"language", "title", "filename", "mimetype", "rotate"}
# Values a scrubbed file is left with, so a second pass sees nothing to do.
VIDEO_NEUTRAL = {"handler_name": {"VideoHandler", "SoundHandler",
                                  "SubtitleHandler", "DataHandler",
                                  "GoPro AVC  ", "Core Media Video"},
                 "vendor_id": {"[0][0][0][0]", "FFMP"},
                 "encoder": {"Lavf", "Lavc"},
                 # MP4 has no way to omit a track language, so "und" is what a
                 # cleared one reads back as.
                 "language": {"und", "unk", ""}}
MP4_FAMILY = {".mp4", ".m4v", ".mov", ".qt", ".3gp", ".3g2", ".m4b", ".m4a"}

# JPEG application segments. APP0 carries JFIF density, APP14 the Adobe colour
# transform flag — dropping either changes how the image decodes, so both stay.
JPEG_KEEP_APP = {0xE0, 0xEE}
# PNG chunks that hold text, timestamps or embedded EXIF. Everything else is
# left alone, because ancillary chunks like gAMA and pHYs affect rendering.
PNG_STRIP_CHUNKS = {b"tEXt", b"zTXt", b"iTXt", b"tIME", b"eXIf"}
WEBP_STRIP_CHUNKS = {b"EXIF", b"XMP "}
# TIFF stores metadata as numbered directory tags; these are the identifying
# ones, as opposed to the tags that describe how to decode the pixels.
TIFF_META_TAGS = {270: "ImageDescription", 271: "Make", 272: "Model",
                  305: "Software", 306: "DateTime", 315: "Artist",
                  33432: "Copyright", 34665: "EXIF IFD", 34853: "GPS IFD",
                  700: "XMP", 33723: "IPTC", 37510: "UserComment"}

# OOXML keeps its metadata in a fixed set of parts. They are replaced with
# valid empty stubs rather than deleted: the package relationships point at
# them, and Word repairs (and re-adds) a document whose parts have vanished.
OOXML_STUBS = {
    "docProps/core.xml":
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<cp:coreProperties '
        'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
        'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"/>',
    "docProps/app.xml":
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Properties xmlns="http://schemas.openxmlformats.org/'
        'officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/'
        'docPropsVTypes"/>',
    "docProps/custom.xml":
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Properties xmlns="http://schemas.openxmlformats.org/'
        'officeDocument/2006/custom-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/'
        'docPropsVTypes"/>',
    "meta.xml":  # OpenDocument
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document-meta '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'office:version="1.2"><office:meta/></office:document-meta>',
}

# ZIP entries store a local modification time in the archive itself, which
# survives every content-level scrub. 1980-01-01 is the earliest a ZIP can hold.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


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


class Scrubber:
    def __init__(self, args):
        self.dry = args.dry_run
        self.categories = set(args.categories)
        self.strip_icc = args.strip_color_profiles
        self.backup = args.backup
        self.verbose = args.verbose
        self.include = args.include or []
        self.exclude = args.exclude or []
        self.hidden = args.hidden
        self.stamp = args.timestamp
        self.media_timeout = args.media_timeout
        self.scanned = 0
        self.changed = 0
        self.bytes_saved = 0
        self.skipped: dict[str, int] = {}
        self.found: dict[str, int] = {}
        self.missing_deps: set[str] = set()
        self.root = Path(".")

    # ---------------------------------------------------------------- utils
    def _record(self, labels) -> None:
        for label in labels:
            self.found[label] = self.found.get(label, 0) + 1

    def _skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def _wanted(self, path: Path) -> bool:
        name = path.name
        if self.backup and name.endswith(".orig"):
            return False      # a backup this tool wrote on an earlier pass
        if self.exclude and any(fnmatch.fnmatch(name, p) for p in self.exclude):
            return False
        if self.include and not any(fnmatch.fnmatch(name, p) for p in self.include):
            return False
        return True

    def _write(self, path: Path, data: bytes) -> bool:
        """Replace *path* with *data*, atomically and without losing its mode."""
        prepared = self._prepare(path)
        if not prepared:
            return False
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".bzt-scrub-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except OSError as exc:
            Path(tmp).unlink(missing_ok=True)
            ui.warn(f"{path}: {exc}")
            return False
        return self._install(path, tmp, *prepared)

    def _prepare(self, path: Path):
        """Capture what a replacement must carry over, and take the backup.

        The scrubbed copy is written beside the original and renamed over it, so
        an interrupted run can never leave a half-written file where a photo was.
        """
        try:
            info = path.stat()
        except OSError as exc:
            ui.warn(f"{path}: {exc}")
            return None
        # Replacing the file would otherwise drop every extended attribute it
        # carries — including its SELinux label and any POSIX ACL, which are
        # not ours to discard. Only user.* is dropped, and only when the xattr
        # category actually asked for it.
        try:
            keep = {n: os.getxattr(path, n) for n in os.listxattr(path)
                    if not (n.startswith("user.") and "xattr" in self.categories)}
        except (OSError, AttributeError):
            keep = {}
        if self.backup:
            try:
                shutil.copy2(path, path.with_name(path.name + ".orig"))
            except OSError as exc:
                ui.warn(f"{path}: could not write backup: {exc}")
                return None
        return info, keep

    def _install(self, path: Path, tmp: str, info, keep: dict) -> bool:
        """Move the finished replacement at *tmp* over *path*."""
        try:
            os.chmod(tmp, stat.S_IMODE(info.st_mode))
            try:
                os.chown(tmp, info.st_uid, info.st_gid)
            except (OSError, PermissionError):
                pass          # not root: the file keeps our ownership, which is
                              # correct when we are the owner anyway
            for name, value in keep.items():
                try:
                    os.setxattr(tmp, name, value)
                except OSError:
                    pass      # relabelling can need privileges we do not have;
                              # the kernel's default for this directory applies
            os.replace(tmp, path)
        except OSError as exc:
            Path(tmp).unlink(missing_ok=True)
            ui.warn(f"{path}: {exc}")
            return False
        # Rewriting a file bumps its mtime, which is itself a disclosure the
        # caller did not ask for. Put the original back unless they want it gone.
        if "times" not in self.categories:
            try:
                os.utime(path, (info.st_atime, info.st_mtime))
            except OSError:
                pass
        return True

    # ------------------------------------------------------------- handlers
    def _jpeg(self, data: bytes) -> tuple[bytes | None, list[str]]:
        """Drop metadata segments, copying the compressed image verbatim.

        Re-encoding through an imaging library would quietly degrade every photo
        in the tree, so the marker structure is edited directly instead.
        """
        if not data.startswith(b"\xff\xd8"):
            return None, []
        out = bytearray(b"\xff\xd8")
        removed: list[str] = []
        i, n = 2, len(data)
        while i < n:
            if data[i] != 0xFF:
                return None, []                   # malformed: leave it alone
            while i < n and data[i] == 0xFF:
                i += 1                            # fill bytes are legal padding
            if i >= n:
                break
            marker, i = data[i], i + 1
            if marker == 0x01 or 0xD0 <= marker <= 0xD8:
                out += bytes((0xFF, marker))
                continue
            if marker == 0xD9:
                out += b"\xff\xd9"
                break
            if i + 2 > n:
                return None, []
            length = int.from_bytes(data[i:i + 2], "big")
            if length < 2 or i + length > n:
                return None, []
            body = data[i + 2:i + length]
            if marker == 0xDA:                    # SOS: entropy data to the end
                out += bytes((0xFF, marker)) + data[i:i + length]
                tail, junk = self._jpeg_scan(data, i + length)
                out += tail
                if junk:
                    removed.append("appended data")
                break
            label = self._jpeg_label(marker, body)
            if label:
                removed.append(label)
            else:
                out += bytes((0xFF, marker)) + data[i:i + length]
            i += length
        return bytes(out), removed

    @staticmethod
    def _jpeg_scan(data: bytes, start: int) -> tuple[bytes, bool]:
        """Return the entropy-coded scan up to EOI, and whether junk followed.

        Anything after the end-of-image marker is not part of the picture; it is
        where thumbnails, editor state and the occasional whole second file get
        hidden.
        """
        i, n = start, len(data)
        while i < n - 1:
            if data[i] != 0xFF:
                i += 1
                continue
            nxt = data[i + 1]
            if nxt == 0x00 or 0xD0 <= nxt <= 0xD7 or nxt == 0xFF:
                i += 2 if nxt != 0xFF else 1      # stuffed byte or restart
                continue
            if nxt == 0xD9:
                return data[start:i + 2], i + 2 < n
            i += 2
        return data[start:], False

    def _jpeg_label(self, marker: int, body: bytes) -> str | None:
        if marker == 0xFE:
            return "JPEG comment"
        if not 0xE0 <= marker <= 0xEF:
            return None
        if marker in JPEG_KEEP_APP:
            return None
        if marker == 0xE2 and body.startswith(b"ICC_PROFILE\x00"):
            return "ICC profile" if self.strip_icc else None
        if marker == 0xE1:
            if body.startswith(b"Exif\x00"):
                return "EXIF"
            if body.startswith(b"http://ns.adobe.com/xap/"):
                return "XMP"
            return "APP1"
        if marker == 0xED:
            return "IPTC/Photoshop"
        return f"APP{marker - 0xE0}"

    def _png(self, data: bytes) -> tuple[bytes | None, list[str]]:
        """Rebuild the chunk stream without the text, time and EXIF chunks."""
        sig = b"\x89PNG\r\n\x1a\n"
        if not data.startswith(sig):
            return None, []
        out = bytearray(sig)
        removed: list[str] = []
        i, n = len(sig), len(data)
        while i + 8 <= n:
            length = int.from_bytes(data[i:i + 4], "big")
            ctype = data[i + 4:i + 8]
            end = i + 12 + length
            if length < 0 or end > n:
                return None, []
            strip = ctype in PNG_STRIP_CHUNKS or (
                ctype == b"iCCP" and self.strip_icc)
            if strip:
                removed.append(ctype.decode("ascii", "replace"))
            else:
                out += data[i:end]
            i = end
            if ctype == b"IEND":
                if i < n:
                    removed.append("appended data")
                break
        return bytes(out), removed

    def _webp(self, data: bytes) -> tuple[bytes | None, list[str]]:
        """Drop the EXIF/XMP RIFF chunks and clear their VP8X presence flags."""
        if not (data.startswith(b"RIFF") and data[8:12] == b"WEBP"):
            return None, []
        chunks: list[tuple[bytes, bytes]] = []
        removed: list[str] = []
        i, n = 12, len(data)
        while i + 8 <= n:
            fourcc = data[i:i + 4]
            size = int.from_bytes(data[i + 4:i + 8], "little")
            body = data[i + 8:i + 8 + size]
            if len(body) < size:
                return None, []
            if fourcc in WEBP_STRIP_CHUNKS or (
                    fourcc == b"ICCP" and self.strip_icc):
                removed.append(fourcc.decode("ascii", "replace").strip())
            else:
                chunks.append((fourcc, body))
            i += 8 + size + (size & 1)            # chunks are padded to even
        if not removed:
            return bytes(data), []
        rebuilt = []
        for fourcc, body in chunks:
            if fourcc == b"VP8X" and len(body) >= 1:
                flags = body[0] & ~0x08 & ~0x04   # EXIF and XMP presence bits
                if self.strip_icc:
                    flags &= ~0x20
                body = bytes((flags,)) + body[1:]
            rebuilt.append((fourcc, body))
        payload = bytearray()
        for fourcc, body in rebuilt:
            payload += fourcc + len(body).to_bytes(4, "little") + body
            if len(body) & 1:
                payload += b"\x00"
        out = b"RIFF" + (len(payload) + 4).to_bytes(4, "little") + b"WEBP" + payload
        return out, removed

    def _gif(self, data: bytes) -> tuple[bytes | None, list[str]]:
        """Drop comment and metadata extension blocks, frame data untouched.

        Re-saving a GIF through an imaging library collapses every frame but the
        first, so animations are edited at the block level. The NETSCAPE2.0
        application extension is kept: it is what makes a GIF loop.
        """
        if not data.startswith((b"GIF87a", b"GIF89a")):
            return None, []
        n = len(data)
        i = 13                                    # header + logical screen desc
        if i > n:
            return None, []
        packed = data[10]
        if packed & 0x80:                         # global colour table
            i += 3 * (2 ** ((packed & 0x07) + 1))
        out = bytearray(data[:i])
        removed: list[str] = []
        if i > n:
            return None, []

        def sub_blocks(pos: int) -> int:
            """Advance past a chain of length-prefixed sub-blocks."""
            while pos < n and data[pos]:
                pos += 1 + data[pos]
            return pos + 1                        # step over the 0x00 terminator

        while i < n:
            block = data[i]
            if block == 0x3B:                     # trailer
                out += b"\x3b"
                if i + 1 < n:
                    removed.append("appended data")
                break
            if block == 0x21:                     # extension
                if i + 2 > n:
                    return None, []
                label = data[i + 1]
                end = sub_blocks(i + 2)
                if end > n:
                    return None, []
                keep = True
                if label in (0xFE, 0x01):         # comment, plain text
                    removed.append("comment" if label == 0xFE else "plain text")
                    keep = False
                elif label == 0xFF:               # application extension
                    ident = data[i + 3:i + 14]
                    if not ident.startswith((b"NETSCAPE", b"ANIMEXTS")):
                        removed.append(ident.decode("ascii", "replace").strip()
                                       or "application data")
                        keep = False
                if keep:
                    out += data[i:end]
                i = end
                continue
            if block == 0x2C:                     # image descriptor
                if i + 10 > n:
                    return None, []
                local = data[i + 9]
                j = i + 10
                if local & 0x80:
                    j += 3 * (2 ** ((local & 0x07) + 1))
                j += 1                            # LZW minimum code size
                end = sub_blocks(j)
                if end > n:
                    return None, []
                out += data[i:end]
                i = end
                continue
            return None, []                       # unknown block: leave it alone
        return bytes(out), removed

    def _pillow(self, path: Path, data: bytes) -> tuple[bytes | None, list[str]]:
        """Fallback for image formats with no safe byte-level edit.

        Only reached for TIFF/GIF/BMP and friends; the formats people actually
        have thousands of are handled losslessly above.
        """
        try:
            from PIL import Image
        except ImportError:
            self.missing_deps.add("pillow")
            self._skip("needs pillow")
            return None, []
        try:
            with Image.open(io.BytesIO(data)) as img:
                fmt = img.format
                if getattr(img, "n_frames", 1) > 1:
                    # Re-saving would keep only the first frame; losing pages of
                    # a document scan to strip a tag is not a trade worth making.
                    self._skip("multi-frame image (needs exiftool)")
                    return None, []
                present = [k for k in ("exif", "XML:com.adobe.xmp", "comment",
                                       "icc_profile")
                           if img.info.get(k)]
                # TIFF keeps its metadata as directory tags rather than in
                # .info, so the usual keys never see it.
                tags = getattr(img, "tag_v2", None)
                if tags:
                    present += [TIFF_META_TAGS[t] for t in sorted(tags)
                                if t in TIFF_META_TAGS]
                if not present:
                    return bytes(data), []
                clean = Image.frombytes(img.mode, img.size, img.tobytes())
                if img.mode == "P":
                    clean.putpalette(img.getpalette() or [])
                buf = io.BytesIO()
                clean.save(buf, format=fmt)
        except Exception as exc:                  # Pillow raises many types
            self._skip(f"unreadable image ({type(exc).__name__})")
            return None, []
        if not self.strip_icc and "icc_profile" in present:
            present.remove("icc_profile")
            if not present:
                return bytes(data), []
        return buf.getvalue(), [p.replace("XML:com.adobe.xmp", "XMP")
                                for p in present]

    def _pdf(self, path: Path, data: bytes) -> tuple[bytes | None, list[str]]:
        try:
            from pypdf import PdfReader, PdfWriter
        except ImportError:
            try:
                from PyPDF2 import PdfReader, PdfWriter   # older name
            except ImportError:
                self.missing_deps.add("pypdf")
                self._skip("needs pypdf")
                return None, []
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                self._skip("encrypted PDF")
                return None, []
            present = []
            if reader.metadata:
                present += sorted(str(k).lstrip("/") for k in reader.metadata)
            root = reader.trailer.get("/Root", {})
            if "/Metadata" in root:
                present.append("XMP")
            if not present:
                return bytes(data), []
            writer = PdfWriter()
            for page in reader.pages:
                # Pages carry their own metadata: /Metadata is per-page XMP and
                # /PieceInfo is where editors keep private application state.
                for key in ("/Metadata", "/PieceInfo"):
                    if key in page:
                        del page[key]
                        if key.strip("/") not in present:
                            present.append(key.strip("/"))
                writer.add_page(page)
            try:
                # Assigning None drops the /Info dictionary outright. Merging an
                # empty dict with add_metadata() looks equivalent and is not: it
                # leaves every existing key in place and stamps on a new
                # /Producer of its own.
                writer.metadata = None
            except (AttributeError, TypeError):
                writer.add_metadata({})       # PyPDF2 has no setter
            try:
                writer.xmp_metadata = None
            except (AttributeError, TypeError, NotImplementedError):
                pass
            buf = io.BytesIO()
            writer.write(buf)
        except Exception as exc:
            self._skip(f"unreadable PDF ({type(exc).__name__})")
            return None, []
        return buf.getvalue(), present

    def _office(self, path: Path, data: bytes) -> tuple[bytes | None, list[str]]:
        """Rewrite the ZIP container without its metadata parts."""
        try:
            src = zipfile.ZipFile(io.BytesIO(data))
            names = src.namelist()
        except (zipfile.BadZipFile, OSError):
            self._skip("not a valid archive")
            return None, []
        removed = [n for n in names if n in OOXML_STUBS
                   and src.read(n) != OOXML_STUBS[n].encode()]
        dated = [i for i in src.infolist() if i.date_time > ZIP_EPOCH]
        if not removed and not dated:
            src.close()
            return bytes(data), []
        buf = io.BytesIO()
        try:
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
                for item in src.infolist():
                    payload = (OOXML_STUBS[item.filename].encode()
                               if item.filename in OOXML_STUBS
                               else src.read(item.filename))
                    fresh = zipfile.ZipInfo(item.filename, date_time=ZIP_EPOCH)
                    fresh.compress_type = item.compress_type
                    fresh.external_attr = item.external_attr
                    dst.writestr(fresh, payload)
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            self._skip(f"unreadable archive ({type(exc).__name__})")
            return None, []
        finally:
            src.close()
        labels = [Path(n).name for n in removed]
        if dated:
            labels.append("archive timestamps")
        return buf.getvalue(), labels

    def _audio(self, path: Path) -> list[str]:
        """Delete every tag frame, editing the file in place.

        Takes a path rather than the file's bytes: media runs to gigabytes and
        mutagen rewrites only the tag block, never the stream.
        """
        try:
            import mutagen
        except ImportError:
            self.missing_deps.add("mutagen")
            self._skip("needs mutagen")
            return []
        try:
            handle = mutagen.File(str(path))
        except Exception as exc:
            self._skip(f"unreadable audio ({type(exc).__name__})")
            return []
        if handle is None or not handle.tags:
            return []
        labels = [f"{type(handle.tags).__name__} tags"]
        if self.dry:
            return labels                         # report only; nothing written
        try:
            handle.delete()
            handle.save()
        except Exception as exc:
            self._skip(f"unwritable audio ({type(exc).__name__})")
            return []
        return labels

    # ---------------------------------------------------------------- video
    def _probe(self, path: Path) -> dict | None:
        res = su.run(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                      "-show_chapters", "-of", "json", str(path)], timeout=300)
        if not res.ok:
            return None
        try:
            return json.loads(res.out)
        except ValueError:
            return None

    @staticmethod
    def _neutral(key: str, value: str) -> bool:
        """True when a tag holds the placeholder a scrubbed file is left with.

        Some tags cannot simply be dropped — a container writes its own default
        back on every mux. Recognising those defaults is what makes a second
        pass over an already-clean tree a no-op.
        """
        return value in VIDEO_NEUTRAL.get(key.lower(), ())

    def _video_metadata(self, info: dict) -> list[str]:
        """What is worth removing from a probed container."""
        found: list[str] = []
        for key, value in (info.get("format", {}).get("tags") or {}).items():
            if key.lower() in VIDEO_FORMAT_IGNORE or self._neutral(key, value):
                continue
            found.append(key.lower())
        if info.get("chapters"):
            found.append("chapters")
        for stream in info.get("streams", []):
            kind = stream.get("codec_type", "stream")
            for key, value in (stream.get("tags") or {}).items():
                low = key.lower().lstrip("_")
                if low in VIDEO_STREAM_IGNORE or self._neutral(key, value):
                    continue
                # Subtitle and audio tracks keep what identifies them to a
                # player; on the video track those same tags are just metadata.
                if low in VIDEO_KEEP_TAGS and kind != "video":
                    continue
                if low == "rotate":
                    continue                      # display orientation, not data
                found.append(f"{kind} {low}")
        return list(dict.fromkeys(found))

    def _video(self, path: Path) -> list[str]:
        """Remux the container without its metadata, keeping every stream.

        `-c copy` means nothing is re-encoded and `-map 0` carries every track
        across — video, audio, subtitles and attached fonts alike. The metadata
        is cleared wholesale and only the tags a player needs are written back,
        because clearing them all makes Matroska refuse to mux an attachment.
        """
        if not (su.has("ffmpeg") and su.has("ffprobe")):
            if path.suffix.lower() in MP4_FAMILY:
                return self._audio(path)   # mutagen still reaches the MP4 atoms
            self.missing_deps.add("ffmpeg")
            self._skip("needs ffmpeg")
            return []
        info = self._probe(path)
        if info is None:
            self._skip("unreadable video")
            return []
        found = self._video_metadata(info)
        if not found:
            return []
        if self.dry:
            return found

        prepared = self._prepare(path)
        if not prepared:
            return []
        # The muxer is chosen from the extension, so the working copy needs the
        # same one; it sits beside the original so the rename stays atomic.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".bzt-scrub-",
                                   suffix=path.suffix)
        os.close(fd)
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y",
               "-i", str(path), "-map", "0", "-c", "copy",
               "-map_metadata", "-1", "-map_chapters", "-1",
               # Without this ffmpeg stamps its own version string into the
               # file, trading the original fingerprint for a fresh one.
               "-fflags", "+bitexact"]
        for stream in info.get("streams", []):
            if stream.get("codec_type") == "video":
                continue
            for key, value in (stream.get("tags") or {}).items():
                if key.lower() in VIDEO_KEEP_TAGS:
                    cmd += [f"-metadata:s:{stream['index']}", f"{key}={value}"]
        cmd.append(tmp)

        res = su.run(cmd, timeout=self.media_timeout)
        if not res.ok:
            Path(tmp).unlink(missing_ok=True)
            reason = (res.err or "").splitlines()
            self._skip(f"ffmpeg could not rewrite this container "
                       f"({reason[-1][:60] if reason else f'exit {res.rc}'})")
            return []
        # A remux that quietly dropped a subtitle track would be worse than not
        # scrubbing at all, so the result is checked before it is installed.
        after = self._probe(Path(tmp))
        if after is None or len(after.get("streams", [])) != len(
                info.get("streams", [])):
            Path(tmp).unlink(missing_ok=True)
            self._skip("remux lost a stream — original left alone")
            return []
        # ffmpeg picks the muxer from the output extension, so a file whose
        # name does not match its actual container would come back converted
        # into a different format entirely. Changing what a file *is* goes well
        # beyond stripping its metadata.
        if after.get("format", {}).get("format_name") != \
                info.get("format", {}).get("format_name"):
            Path(tmp).unlink(missing_ok=True)
            self._skip(f"{path.suffix} does not match the actual container "
                       "— original left alone")
            return []
        if not self._install(path, tmp, *prepared):
            return []
        return found

    def _xattrs(self, path: Path) -> list[str]:
        """Remove user.* extended attributes.

        `user.xdg.origin.url` records the exact URL a file was downloaded from
        and travels with it onto a USB stick. security.* and system.* are left
        alone: those hold the SELinux label and POSIX ACLs.
        """
        try:
            names = os.listxattr(path, follow_symlinks=False)
        except (OSError, AttributeError):
            return []
        targets = [n for n in names if n.startswith("user.")]
        if not targets or self.dry:
            return targets
        removed = []
        for name in targets:
            try:
                os.removexattr(path, name, follow_symlinks=False)
                removed.append(name)
            except OSError:
                pass
        return removed

    def _times(self, path: Path) -> list[str]:
        try:
            info = path.stat()
        except OSError:
            return []
        if int(info.st_mtime) == self.stamp:
            return []
        if not self.dry:
            try:
                os.utime(path, (self.stamp, self.stamp))
            except OSError:
                return []
        return ["timestamps"]

    # ------------------------------------------------------------------ run
    def scrub_file(self, path: Path) -> None:
        self.scanned += 1
        suffix = path.suffix.lower()
        labels: list[str] = []
        saved = 0

        # Swept before the content rewrite, not after: replacing the file drops
        # its extended attributes, so a later pass would find nothing left to
        # report even though the data had gone.
        if "xattr" in self.categories:
            labels += [f"xattr {n}" for n in self._xattrs(path)]

        content_handler = None
        if "video" in self.categories and suffix in VIDEO_EXT:
            content_handler = "video"
        elif "image" in self.categories and suffix in IMAGE_EXT:
            content_handler = "image"
        elif "pdf" in self.categories and suffix in PDF_EXT:
            content_handler = "pdf"
        elif "office" in self.categories and suffix in OFFICE_EXT:
            content_handler = "office"
        elif "audio" in self.categories and suffix in AUDIO_EXT:
            content_handler = "audio"

        if content_handler in ("video", "audio"):
            # Media runs to gigabytes: these handlers work on the path and do
            # their own writing, so the file is never read into memory.
            before = path.stat().st_size if not self.dry else 0
            labels += (self._video(path) if content_handler == "video"
                       else self._audio(path))
            if labels and not self.dry:
                try:
                    saved = max(0, before - path.stat().st_size)
                except OSError:
                    saved = 0
        elif content_handler:
            try:
                data = path.read_bytes()
            except OSError as exc:
                self._skip("unreadable")
                if self.verbose:
                    ui.warn(f"{path}: {exc}")
                data = None
            if data is not None:
                new, found = self._dispatch(content_handler, path, suffix, data)
                labels += found
                if found and new is not None and new != data:
                    if self.dry or self._write(path, new):
                        saved = max(0, len(data) - len(new))

        # Last: the content rewrite above sets a fresh mtime of its own.
        if "times" in self.categories:
            labels += self._times(path)

        if not labels:
            return
        self.changed += 1
        self.bytes_saved += saved
        self._record(labels)
        verb = "would strip" if self.dry else "stripped"
        detail = ", ".join(dict.fromkeys(labels))
        size = f" (-{su.human(saved)})" if saved else ""
        try:
            shown = path.relative_to(self.root)
        except ValueError:
            shown = path
        print(f"  {ui.GREEN}{verb}{ui.RESET} {shown}: {ui.GREY}{detail}{size}{ui.RESET}")

    def _dispatch(self, kind: str, path: Path, suffix: str, data: bytes):
        if kind == "image":
            if suffix in (".jpg", ".jpeg", ".jpe"):
                return self._jpeg(data)
            if suffix == ".png":
                return self._png(data)
            if suffix == ".webp":
                return self._webp(data)
            if suffix == ".gif":
                return self._gif(data)
            return self._pillow(path, data)
        if kind == "pdf":
            return self._pdf(path, data)
        return self._office(path, data)

    def walk(self, root: Path) -> None:
        self.root = root
        for dirpath, dirnames, filenames in os.walk(root, topdown=True,
                                                    onerror=self._walk_error):
            here = Path(dirpath)
            if not self.hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            # Never leave the tree the user named by way of a symlinked folder.
            dirnames[:] = [d for d in dirnames if not (here / d).is_symlink()]
            for name in sorted(filenames):
                path = here / name
                if not self.hidden and name.startswith("."):
                    continue
                if path.is_symlink():
                    self._skip("symlink")
                    continue
                try:
                    if not stat.S_ISREG(path.lstat().st_mode):
                        self._skip("not a regular file")
                        continue
                except OSError:
                    self._skip("unreadable")
                    continue
                if not self._wanted(path):
                    continue
                try:
                    self.scrub_file(path)
                except Exception as exc:          # one bad file must not end
                    self._skip(f"failed ({type(exc).__name__})")   # the walk
                    if self.verbose:
                        ui.warn(f"{path}: {exc}")

    def _walk_error(self, exc: OSError) -> None:
        self._skip("unreadable directory")
        if self.verbose:
            ui.warn(str(exc))

    def report(self) -> None:
        ui.header("Summary")
        if self.found:
            table = ui.Table(["Metadata removed", "Files"])
            for label, count in sorted(self.found.items(),
                                       key=lambda kv: (-kv[1], kv[0])):
                table.add([label, count])
            table.show()
        print(f"  Files scanned  : {self.scanned}")
        print(f"  Files {'to change' if self.dry else 'changed  '}: "
              f"{ui.BOLD}{self.changed}{ui.RESET}")
        if self.bytes_saved:
            print(f"  Size reclaimed : {su.human(self.bytes_saved)}")
        for reason, count in sorted(self.skipped.items()):
            ui.note(f"skipped {count} file(s): {reason}")
        if self.missing_deps:
            ui.note("install for wider coverage: pip install --user "
                    + " ".join(sorted(self.missing_deps)))
        if self.dry:
            print(f"  {ui.GREY}dry run — nothing was modified; "
                  f"re-run without --dry-run to apply{ui.RESET}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Categories: " + ", ".join(CATEGORIES))
    ap.add_argument("directory", type=Path,
                    help="directory to clean, recursively")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="report what would be stripped without writing anything")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="do not ask for confirmation before modifying files")
    ap.add_argument("-c", "--categories", default=",".join(DEFAULT_CATEGORIES),
                    help="comma-separated subset of categories "
                         f"(default: {','.join(DEFAULT_CATEGORIES)})")
    ap.add_argument("-b", "--backup", action="store_true",
                    help="keep the original alongside each file as <name>.orig")
    ap.add_argument("--include", action="append", metavar="GLOB",
                    help="only files matching this glob (repeatable)")
    ap.add_argument("--exclude", action="append", metavar="GLOB",
                    help="skip files matching this glob (repeatable)")
    ap.add_argument("--hidden", action="store_true",
                    help="also descend into dot-directories and dotfiles")
    ap.add_argument("--strip-color-profiles", action="store_true",
                    help="also remove embedded ICC profiles (changes how "
                         "images render on calibrated displays)")
    ap.add_argument("--timestamp", type=int, default=315532800,
                    metavar="EPOCH",
                    help="value the 'times' category writes (default: "
                         "1980-01-01, the earliest a ZIP can store)")
    ap.add_argument("--media-timeout", type=int, default=1800, metavar="SECONDS",
                    help="how long a single video remux may take "
                         "(default: 1800; raise it for very large files)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="report unreadable files individually")
    args = ap.parse_args()

    args.categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    unknown = [c for c in args.categories if c not in CATEGORIES]
    if unknown:
        ui.fail(f"unknown categor(y/ies): {', '.join(unknown)}")
        ui.note("available: " + ", ".join(CATEGORIES))
        return 2

    root = args.directory.expanduser()
    if not root.is_dir():
        ui.fail(f"{root} is not a directory")
        return 2
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        ui.fail(f"{root}: {exc}")
        return 2
    if str(root) in PROTECTED_DIRS:
        ui.fail(f"{root} is a system directory — refusing to rewrite it")
        return 2
    if root == Path.home() and not args.yes:
        ui.warn("that is your entire home directory")

    stamp = datetime.fromtimestamp(args.timestamp, timezone.utc)
    print(f"{ui.BOLD}{ui.CYAN}Metadata scrubber{ui.RESET}"
          f"{'  (dry run — nothing will be modified)' if args.dry_run else ''}")
    print(f"{ui.GREY}tree: {root}{ui.RESET}")
    print(f"{ui.GREY}categories: {', '.join(args.categories)}"
          f"{'' if 'times' not in args.categories else f'  (times -> {stamp:%Y-%m-%d})'}"
          f"{ui.RESET}")
    if "audio" in args.categories and not args.dry_run:
        ui.warn("the audio category deletes artist, album and track tags too")
    if "video" in args.categories and not (su.has("ffmpeg") and su.has("ffprobe")):
        ui.note("ffmpeg is not installed — video files will be skipped "
                "(rpm-ostree install ffmpeg, or use the Flatpak runtime)")

    if not args.dry_run and not args.yes:
        ui.warn("this rewrites files in place and cannot be undone"
                f"{'' if args.backup else ' (--backup keeps originals)'}")
        if not ask(f"Strip metadata from every file under {root}?"):
            ui.note("nothing was changed")
            return 1

    ui.header("Scrubbing")
    scrubber = Scrubber(args)
    scrubber.walk(root)
    if not scrubber.changed:
        ui.ok("no metadata found — every file under this tree is already clean")
    scrubber.report()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        ui.warn("interrupted")
        sys.exit(130)
