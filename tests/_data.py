"""Portable test-artifact resolution.

The boot / play integration tests need two artifacts that live outside the repo:

* the firmware ``.upd`` — auto-downloadable from the Ravensburger CDN
  (SHA-verified, cached) via :func:`tt_emu.firmware_fetch.ensure_firmware`;
* a real tiptoi game directory (``.gme`` + tttool ``.yaml``) — *not* shipped.

These helpers resolve both from the environment first, then from the legacy
local paths on the original author's machine, so the same tests run on CI (with
``$TT_EMU_DOWNLOAD_FIRMWARE`` set) and on a developer box, and skip cleanly
elsewhere. Tests gate on ``firmware_path() is None`` / ``game_dir() is None``.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Legacy local paths on the original author's machine.
_LEGACY_FIRMWARE = Path("/home/jojo/tiptoi/update3202MT.upd")
_LEGACY_GAME_DIR = Path("/home/jojo/tiptoi/tiptoi-taschenrechner")
#: Legacy local path to the 1st-gen ZC3201 firmware container.
_LEGACY_FIRMWARE_ZC3201 = Path("/home/jojo/tiptoi/update.upd")


def firmware_path() -> Path | None:
    """Resolve the firmware ``.upd``, or ``None`` if unavailable.

    Resolution order:

    1. ``$TT_EMU_FIRMWARE`` if set (used verbatim);
    2. the legacy local path if it exists;
    3. a fresh download via :func:`ensure_firmware` — **only** when
       ``$TT_EMU_DOWNLOAD_FIRMWARE`` is set (so a plain offline ``pytest``
       never fetches ~11 MB); returns ``None`` if the download fails.
    """
    env = os.environ.get("TT_EMU_FIRMWARE")
    if env:
        return Path(env)
    if _LEGACY_FIRMWARE.exists():
        return _LEGACY_FIRMWARE
    if os.environ.get("TT_EMU_DOWNLOAD_FIRMWARE"):
        from tt_emu.firmware_fetch import ensure_firmware

        try:
            return Path(ensure_firmware(None))
        except Exception:
            return None
    return None


def firmware_path_zc3201() -> Path | None:
    """Resolve the 1st-gen ZC3201 firmware ``.upd``, or ``None`` if unavailable.

    ``$TT_EMU_FIRMWARE_ZC3201`` if set (verbatim), else the legacy local path if
    it exists, else a fresh SHA-verified download via :func:`ensure_firmware`
    (ZC3201 profile) — **only** when ``$TT_EMU_DOWNLOAD_FIRMWARE`` is set.
    """
    env = os.environ.get("TT_EMU_FIRMWARE_ZC3201")
    if env:
        return Path(env)
    if _LEGACY_FIRMWARE_ZC3201.exists():
        return _LEGACY_FIRMWARE_ZC3201
    if os.environ.get("TT_EMU_DOWNLOAD_FIRMWARE"):
        from tt_emu.firmware_fetch import ensure_firmware
        from tt_emu.firmware_profile import ZC3201

        try:
            return Path(ensure_firmware(None, profile=ZC3201))
        except Exception:
            return None
    return None


#: Legacy local path to the 1st-gen ZC3201 test game (product 42, OIDs 8065-8067).
_LEGACY_GME_ZC3201 = Path(
    "/home/jojo/tiptoi/firmware-re/tools/ttemu/content/B/example.gme"
)


def gme_zc3201() -> Path | None:
    """Resolve the ZC3201 test ``.gme`` (``example.gme``), or ``None``.

    ``$TT_EMU_GME_ZC3201`` if set and it exists, else the legacy local path if it
    exists, else ``None``. This is a small hand-built game (product 42, content
    OIDs 8065-8067) used for the 1st-gen discover → mount → OID-tap → play test.
    """
    env = os.environ.get("TT_EMU_GME_ZC3201")
    if env and Path(env).exists():
        return Path(env)
    if _LEGACY_GME_ZC3201.exists():
        return _LEGACY_GME_ZC3201
    return None


def game_dir() -> Path | None:
    """Resolve a tiptoi game directory (``.gme`` + ``.yaml``), or ``None``.

    ``$TT_EMU_GAME_DIR`` if set and it exists, else the legacy local path if it
    exists, else ``None``.
    """
    env = os.environ.get("TT_EMU_GAME_DIR")
    if env and Path(env).exists():
        return Path(env)
    if _LEGACY_GAME_DIR.exists():
        return _LEGACY_GAME_DIR
    return None
