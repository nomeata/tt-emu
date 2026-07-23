"""Portable test-artifact resolution.

The boot / play integration tests need artifacts that live outside the repo:

* the firmware ``.upd`` — auto-downloadable from the Ravensburger CDN
  (SHA-verified, cached) via :func:`tt_emu.firmware_fetch.ensure_firmware`;
* a real tiptoi game directory (``.gme`` + tttool ``.yaml``) — *not* shipped;
* a couple of hand-built local fixtures (the ZC3201 ``example.gme``, a reference
  provisioned NAND image).

Each helper resolves an artifact in this order, and tests skip cleanly when it
returns ``None`` (they gate on ``firmware_path() is None`` / ``game_dir() is None``):

1. the artifact's ``$TT_EMU_*`` environment variable, if set;
2. a path from an **optional, untracked** ``tests/_data_local.py`` (gitignored) —
   so no machine-specific path ever lives in this public repo. Copy
   ``tests/_data_local.example.py`` to ``tests/_data_local.py`` and edit it to make
   the local-artifact tests run on your box without exporting env vars;
3. for the firmware, a fresh SHA-verified download — only when
   ``$TT_EMU_DOWNLOAD_FIRMWARE`` is set (so a plain offline ``pytest`` never fetches).
"""

from __future__ import annotations

import os
from pathlib import Path

try:  # optional, gitignored dev-box overrides (see module docstring)
    import _data_local as _local  # type: ignore[import-not-found]
except ImportError:
    _local = None  # type: ignore[assignment]


def _local_path(name: str) -> Path | None:
    """A path from the untracked ``tests/_data_local.py`` (or ``None`` if absent)."""
    value = getattr(_local, name, None) if _local is not None else None
    return Path(value) if value else None


def firmware_path() -> Path | None:
    """Resolve the firmware ``.upd``, or ``None`` if unavailable."""
    env = os.environ.get("TT_EMU_FIRMWARE")
    if env:
        return Path(env)
    local = _local_path("FIRMWARE")
    if local is not None and local.exists():
        return local
    if os.environ.get("TT_EMU_DOWNLOAD_FIRMWARE"):
        from tt_emu.firmware_fetch import ensure_firmware

        try:
            return Path(ensure_firmware(None))
        except Exception:
            return None
    return None


def firmware_path_zc3201() -> Path | None:
    """Resolve the 1st-gen ZC3201 firmware ``.upd``, or ``None`` if unavailable."""
    env = os.environ.get("TT_EMU_FIRMWARE_ZC3201")
    if env:
        return Path(env)
    local = _local_path("FIRMWARE_ZC3201")
    if local is not None and local.exists():
        return local
    if os.environ.get("TT_EMU_DOWNLOAD_FIRMWARE"):
        from tt_emu.firmware_fetch import ensure_firmware
        from tt_emu.firmware_profile import ZC3201

        try:
            return Path(ensure_firmware(None, profile=ZC3201))
        except Exception:
            return None
    return None


def gme_zc3201() -> Path | None:
    """Resolve the ZC3201 test ``.gme`` (``example.gme``), or ``None``.

    A small hand-built game (product 42, content OIDs 8065-8067) used for the
    1st-gen discover → mount → OID-tap → play test; not downloadable.
    """
    env = os.environ.get("TT_EMU_GME_ZC3201")
    if env and Path(env).exists():
        return Path(env)
    local = _local_path("GME_ZC3201")
    if local is not None and local.exists():
        return local
    return None


def game_dir() -> Path | None:
    """Resolve a tiptoi game directory (``.gme`` + ``.yaml``), or ``None``."""
    env = os.environ.get("TT_EMU_GAME_DIR")
    if env and Path(env).exists():
        return Path(env)
    local = _local_path("GAME_DIR")
    if local is not None and local.exists():
        return local
    return None


def gme2() -> Path | None:
    """Resolve a second, more complex ``.gme`` for cross-checks, or ``None`` (not shipped)."""
    env = os.environ.get("TT_EMU_GME2")
    if env and Path(env).exists():
        return Path(env)
    local = _local_path("GME2")
    if local is not None and local.exists():
        return local
    return None


def ref_nand_image() -> Path | None:
    """Resolve the reference provisioned NAND image (``producer_nand.img``), or ``None``.

    A local fixture the NAND-provisioning test compares its output against; not shipped.
    """
    env = os.environ.get("TT_EMU_REF_NAND_IMG")
    if env and Path(env).exists():
        return Path(env)
    local = _local_path("REF_NAND_IMG")
    if local is not None and local.exists():
        return local
    return None
