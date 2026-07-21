"""Fetch and cache the ``update3202MT.upd`` firmware from Ravensburger's CDN.

Usage note (for the README): the ``firmware`` argument of ``tt-emu`` is
optional. When omitted, the emulator downloads the official 2N "MT" firmware
update container from Ravensburger's CDN (the same file the tiptoi Manager
installs), verifies its SHA-256 against a pinned hash, and caches it under the
platform cache directory (``~/.cache/tt-emu`` on Linux — ``$XDG_CACHE_HOME``
respected —, ``~/Library/Caches/tt-emu`` on macOS, ``%LOCALAPPDATA%\\tt-emu\\Cache``
on Windows). Subsequent runs reuse the cached copy without touching the
network. ``--firmware-cache DIR`` overrides the cache location;
``TT_EMU_FIRMWARE_URL`` overrides the download URL (the pinned SHA-256 still
gates the result, so a wrong file fails loudly instead of being trusted).
Passing an explicit firmware path bypasses all of this, exactly as before.

Integrity model: the download URL is convenience, the **SHA-256 is the
authority**. Every download *and* every cached copy is checked against
:data:`FIRMWARE_SHA256`; a stale cache is re-downloaded once, and a mismatching
download raises :class:`FirmwareIntegrityError` naming expected vs. got hash
and the URL. If Ravensburger ever moves or changes the file, that is caught,
never silently trusted.

Stdlib only: ``urllib.request`` + ``hashlib`` (no new dependency).
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from .firmware_profile import MT, FirmwareProfile

#: Official download URL for the 2N "MT" firmware update container. This is
#: the Ravensburger CDN path the tiptoi Manager fetches firmware from
#: (verified live 2026-07: 11,303,276 bytes, last-modified 2018-08-01, and the
#: payload hashes to :data:`FIRMWARE_SHA256`). Override with the
#: ``TT_EMU_FIRMWARE_URL`` environment variable if it ever moves — the SHA-256
#: check below still gates whatever comes back. Other pens' download metadata
#: live on their :class:`~tt_emu.firmware_profile.FirmwareProfile`; these names
#: stay the MT defaults for backward compatibility.
DEFAULT_FIRMWARE_URL = MT.fetch.urls[0]

#: Pinned SHA-256 of the authentic ``update3202MT.upd`` (build N0038MT).
FIRMWARE_SHA256 = MT.fetch.sha256

#: File name used inside the cache directory.
FIRMWARE_FILENAME = MT.fetch.filename

#: Download chunk size (64 KiB keeps progress updates smooth).
_CHUNK = 64 * 1024


class FirmwareDownloadError(RuntimeError):
    """The firmware could not be downloaded (network / HTTP failure)."""


class FirmwareIntegrityError(RuntimeError):
    """A downloaded firmware file does not match the pinned SHA-256."""

    def __init__(self, url: str, expected: str, got: str) -> None:
        self.url = url
        self.expected = expected
        self.got = got
        super().__init__(
            f"firmware SHA-256 mismatch for {url}:\n"
            f"  expected {expected}\n"
            f"  got      {got}\n"
            "The file at the URL is not the pinned update3202MT.upd "
            "(moved/changed upstream?). Refusing to use it; pass an explicit "
            "firmware path or fix the URL (TT_EMU_FIRMWARE_URL)."
        )


def default_cache_dir() -> Path:
    """The per-user cache directory for tt-emu (cross-platform, stdlib only)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "tt-emu" / "Cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "tt-emu"
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "tt-emu"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


class _Progress:
    """A one-line stderr progress report.

    On a tty the line is rewritten in place (``\\r``); on a non-tty stream
    (logs, pipes) updates are throttled to every 10% / 1 MiB so the output
    stays readable.
    """

    def __init__(self, quiet: bool, filename: str = FIRMWARE_FILENAME) -> None:
        self.quiet = quiet
        self.filename = filename
        self.tty = sys.stderr.isatty()
        self.last = -1  # last reported percent (or MiB when total is unknown)

    def update(self, done: int, total: int | None, *, end: bool = False) -> None:
        if self.quiet:
            return
        if total:
            step = 100 * done // total if self.tty else 10 * (10 * done // total)
            line = (
                f"tt-emu: downloading {self.filename}: "
                f"{100 * done // total:3d}% ({done / 2**20:.1f}/{total / 2**20:.1f} MiB)"
            )
        else:
            step = done // 2**16 if self.tty else done // 2**20
            line = f"tt-emu: downloading {self.filename}: {done / 2**20:.1f} MiB"
        if step == self.last and (not end or not self.tty):
            return  # unchanged; on a tty the end call still adds the newline
        self.last = step
        if self.tty:
            sys.stderr.write("\r" + line + ("\n" if end else ""))
        else:
            sys.stderr.write(line + "\n")
        sys.stderr.flush()


def _download(url: str, dest: Path, *, quiet: bool, filename: str = FIRMWARE_FILENAME) -> None:
    """Stream ``url`` to ``dest`` with a simple stderr progress line."""
    req = Request(url, headers={"User-Agent": "tt-emu"})
    progress = _Progress(quiet, filename)
    try:
        with urlopen(req) as resp, dest.open("wb") as out:
            length = resp.headers.get("Content-Length")
            total = int(length) if length and length.isdigit() else None
            done = 0
            while chunk := resp.read(_CHUNK):
                out.write(chunk)
                done += len(chunk)
                progress.update(done, total)
            progress.update(done, total, end=True)
    except URLError as exc:
        dest.unlink(missing_ok=True)
        raise FirmwareDownloadError(
            f"could not download firmware from {url}: {exc}. "
            "Pass the firmware path explicitly, or set TT_EMU_FIRMWARE_URL."
        ) from exc
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


def ensure_firmware(
    path: str | Path | None,
    *,
    profile: FirmwareProfile = MT,
    cache_dir: str | Path | None = None,
    url: str | None = None,
    sha256: str | None = None,
    quiet: bool = False,
) -> Path:
    """Resolve the firmware to use: an explicit path, or the cached download.

    * ``path`` given → returned as-is (the pre-existing explicit-path
      behaviour; no hash check, so any compatible ``.upd`` still works).
    * ``path`` is None → ``<cache dir>/<profile filename>``. A cached copy whose
      SHA-256 matches is used without any network access; otherwise the file is
      downloaded from ``url`` (default the profile's pinned URL, or the
      ``TT_EMU_FIRMWARE_URL`` environment variable — honoured for MT only, to
      preserve its meaning), verified against ``sha256`` (default the profile's
      pinned hash) and moved into place atomically.

    ``profile`` selects which pen's firmware to fetch (default the 2N "MT"). Its
    filename / URL / SHA-256 are used unless the explicit ``url`` / ``sha256``
    arguments override them.

    Raises :class:`FirmwareIntegrityError` on a hash mismatch (expected vs.
    got, and the URL) and :class:`FirmwareDownloadError` on network failure.
    """
    if path is not None:
        return Path(path)

    sha256 = sha256 or profile.fetch.sha256
    filename = profile.fetch.filename
    cache = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    cached = cache / filename
    if cached.is_file():
        if _sha256_file(cached) == sha256:
            return cached
        # Stale or corrupt cache (e.g. an interrupted earlier download):
        # fall through and re-download once; the hash check below decides.

    # TT_EMU_FIRMWARE_URL overrides the MT default only (it predates profiles and
    # names "the firmware"); other profiles use their own pinned URL.
    env_url = os.environ.get("TT_EMU_FIRMWARE_URL") if profile is MT else None
    resolved_url = url or env_url or profile.fetch.urls[0]
    cache.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=filename + ".", suffix=".part", dir=cache)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        _download(resolved_url, tmp, quiet=quiet, filename=filename)
        got = _sha256_file(tmp)
        if got != sha256:
            raise FirmwareIntegrityError(resolved_url, sha256, got)
        tmp.replace(cached)
    finally:
        tmp.unlink(missing_ok=True)
    return cached
