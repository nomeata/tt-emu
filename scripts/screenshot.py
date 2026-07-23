#!/usr/bin/env python3
"""Generate the README TUI screenshot as a Textual SVG export.

Boots the emulator on the real firmware + the taschenrechner game, drives it
(headless, via Textual's test pilot — no terminal or audio device needed) to a
populated debugger view (book mounted, a content OID tapped and decoded), then
saves an SVG screenshot. Regenerate with:

    python scripts/screenshot.py [--out docs/tui-screenshot.svg]

The firmware argument is optional — omitted, it downloads/caches the official
.upd (see tt_emu.firmware_fetch). The game/yaml default to the taschenrechner
sources; override with --gme / --yaml.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

# Make the package importable when run straight from a checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tt_emu.firmware_fetch import ensure_firmware  # noqa: E402
from tt_emu.tui import EmulatorSession, TtEmuApp  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUT = _ROOT / "docs" / "tui-screenshot.svg"


def _default_game() -> tuple[Path | None, Path | None]:
    """The game's ``.gme``/``.yaml`` from ``$TT_EMU_GAME_DIR`` (a dir holding both), or
    ``(None, None)`` — pass ``--gme``/``--yaml`` instead. No machine-specific default."""
    d = os.environ.get("TT_EMU_GAME_DIR")
    if not d:
        return None, None
    return next(Path(d).glob("*.gme"), None), next(Path(d).glob("*.yaml"), None)


async def _drive(firmware: str, game: Path, yaml: Path | None, out: Path, timeout: float) -> None:
    # Deterministic pacing: the Textual test pilot's message-pump loop holds
    # the GIL aggressively, which starves realtime pacing's wall-locked
    # phases; count-paced chunks are immune (and reproducible screenshots
    # are a feature here anyway).
    session = EmulatorSession(firmware, [game], yaml_path=yaml, pacing="deterministic")
    app = TtEmuApp(session, audio=None)
    async with app.run_test(size=(120, 40)) as pilot:
        session.start()
        session.tap(session.product_code or 0)  # mount the game
        session.tap(4716)  # tap 'acht' -> $eingabe = 8, the debugger populates
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await pilot.pause()
            await asyncio.sleep(0.05)
            snap = session.snapshot
            debug = snap.debug
            if (
                debug is not None
                and debug.ready
                and snap.mounted_product == 42
                and len(debug.registers) == 11
                and debug.registers[2] == 8
            ):
                break
        else:
            print("warning: debugger view did not fully populate before timeout", file=sys.stderr)
        # Debug panels are on by default and reveal themselves once the snapshot
        # is ready (the wait above); just let the UI settle and repaint.
        for _ in range(20):
            await pilot.pause()
        app.save_screenshot(str(out))
    session.shutdown(timeout=20.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the README TUI screenshot (SVG).")
    default_gme, default_yaml = _default_game()
    parser.add_argument("--firmware", default=None, help="path to the .upd (default: auto-download)")
    parser.add_argument("--gme", type=Path, default=default_gme,
                        help="game .gme (default: from $TT_EMU_GAME_DIR)")
    parser.add_argument("--yaml", type=Path, default=default_yaml)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    if args.gme is None:
        parser.error("no game: pass --gme PATH or set $TT_EMU_GAME_DIR")

    firmware = str(ensure_firmware(args.firmware))
    yaml = args.yaml if args.yaml and args.yaml.exists() else None
    args.out.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(_drive(firmware, args.gme, yaml, args.out, args.timeout))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
