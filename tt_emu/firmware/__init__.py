"""Firmware-specific debug support: recognize a known image, expose live readers.

The emulator itself is firmware-agnostic; this package adds *optional*,
per-build debugger views on top. Each supported build has a module with a
byte-exact fingerprint check and hook-free RAM readers (see
``docs/firmware-2n-mt.md``). An unrecognized image simply gets no debugger —
the TUI falls back to its generic panels.
"""

from __future__ import annotations

from . import mt

__all__ = ["detect", "mt"]


def detect(prog: bytes) -> str | None:
    """A short label for a recognized PROG image, or None."""
    if mt.recognize(prog):
        return mt.FIRMWARE_LABEL
    return None
