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
import sys
import time
from pathlib import Path

# Make the package importable when run straight from a checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tt_emu.firmware_fetch import ensure_firmware  # noqa: E402
from tt_emu.tui import EmulatorSession, TtEmuApp  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_GAME = Path("/home/jojo/tiptoi/tiptoi-taschenrechner/taschenrechner.gme")
_DEFAULT_YAML = Path("/home/jojo/tiptoi/tiptoi-taschenrechner/taschenrechner.yaml")
_DEFAULT_OUT = _ROOT / "docs" / "tui-screenshot.svg"


async def _drive(firmware: str, game: Path, yaml: Path | None, out: Path, timeout: float) -> None:
    session = EmulatorSession(firmware, [game], yaml_path=yaml)
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
    parser.add_argument("--firmware", default=None, help="path to the .upd (default: auto-download)")
    parser.add_argument("--gme", type=Path, default=_DEFAULT_GAME)
    parser.add_argument("--yaml", type=Path, default=_DEFAULT_YAML)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)

    firmware = str(ensure_firmware(args.firmware))
    yaml = args.yaml if args.yaml and args.yaml.exists() else None
    args.out.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(_drive(firmware, args.gme, yaml, args.out, args.timeout))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
