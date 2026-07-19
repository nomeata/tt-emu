"""Command-line entry point: ``python -m tt_emu`` / ``tt-emu``.

By default ``tt-emu`` launches the interactive TUI (:mod:`tt_emu.tui`). With
``--headless`` it runs the scripted, no-UI flow instead: boot the firmware,
optionally inject taps through the OID sensor model, capture the audio the
firmware plays and (with ``--wav``) write it out — the full
boot → tap product → tap content → WAV chain — printing the run log.

The firmware argument is optional in both modes: when omitted, the official
``update3202MT.upd`` is downloaded from Ravensburger's CDN, SHA-256 verified and
cached (see :mod:`tt_emu.firmware_fetch`).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .firmware_fetch import FirmwareDownloadError, FirmwareIntegrityError, ensure_firmware
from .machine import MachineConfig
from .runner import (
    SESSION_INSTRUCTIONS_PER_TICK,
    BootReport,
    boot_firmware,
    gme_product_code,
    run_session,
)

#: Default instruction budgets: plain boot vs. a session (OGG decode on the
#: emulated CPU dominates a playback session's cost).
DEFAULT_BOOT_BUDGET = 40_000_000
DEFAULT_SESSION_BUDGET = 800_000_000


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tt-emu --headless",
        description="Scripted (no-UI) tiptoi 2N ('MT') pen emulator — boot, tap, capture.",
    )
    parser.add_argument(
        "firmware",
        nargs="?",
        default=None,
        help="path to update3202MT.upd; omit to download the official firmware "
        "from Ravensburger's CDN into the tt-emu cache (SHA-256 verified)",
    )
    parser.add_argument(
        "--firmware-cache",
        metavar="DIR",
        default=None,
        help="cache directory for the auto-downloaded firmware "
        "(default: the platform cache dir, e.g. ~/.cache/tt-emu)",
    )
    parser.add_argument(
        "-n",
        "--max-instructions",
        type=int,
        default=None,
        help=f"instruction budget before giving up (default: {DEFAULT_BOOT_BUDGET} "
        f"for a boot, {DEFAULT_SESSION_BUDGET} for a tap session)",
    )
    parser.add_argument(
        "--instructions-per-tick",
        type=int,
        default=None,
        help="emulated instructions per 20 ms timer tick "
        "(default: 20000 for a boot, 1000000 for a tap session — the firmware's "
        "busy-delay loops need a realistic CPU speed, see runner.py)",
    )
    parser.add_argument(
        "--trace-mmio", action="store_true", help="log the first few MMIO accesses per address"
    )
    parser.add_argument(
        "--a-dir", metavar="DIR", default=None,
        help="host directory mirrored onto NAND partition A: (system files)",
    )
    parser.add_argument(
        "--b-dir", metavar="DIR", default=None,
        help="host directory mirrored onto NAND partition B: (user .gme files)",
    )
    parser.add_argument(
        "--gme", metavar="GME", action="append", default=[],
        help=".gme file placed on partition B: (repeatable)",
    )
    parser.add_argument(
        "--tap", metavar="OID", action="append", default=[],
        help="OID to tap once the pen is idle (repeatable, in order); "
        "'product' = the product code of the first --gme",
    )
    parser.add_argument(
        "--wav", metavar="FILE", default=None,
        help="write the captured audio (S16LE stereo) to FILE",
    )
    parser.add_argument(
        "--dac-pacing", choices=("fast", "faithful"), default="fast",
        help="fast (default): produce audio at the emulator's own speed; "
        "faithful: pace to the pen's real audio timeline (captured PCM is "
        "identical either way — this only affects emulated timing)",
    )
    parser.add_argument(
        "--pacing", choices=("deterministic", "realtime"), default="deterministic",
        help="deterministic (default): count-paced, bit-for-bit reproducible "
        "runs — right for scripted/captured sessions; realtime: emulated "
        "time tracks wall time as in the interactive TUI (faster, not "
        "reproducible)",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="-v: info, -vv: debug"
    )
    return parser


def run_headless(argv: list[str] | None = None) -> int:
    """The scripted, no-UI flow (``tt-emu --headless``)."""
    args = build_parser().parse_args(argv)
    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")

    # No firmware argument → resolve to the cached official download
    # (SHA-256 verified; see firmware_fetch.py). An explicit path is used as-is.
    try:
        firmware = str(ensure_firmware(args.firmware, cache_dir=args.firmware_cache))
    except (FirmwareDownloadError, FirmwareIntegrityError) as exc:
        print(f"tt-emu: {exc}", file=sys.stderr)
        return 1

    b_files: dict[str, bytes] = {}
    for gme in args.gme:
        p = Path(gme)
        b_files[p.name] = p.read_bytes()

    taps: list[int] = []
    for spec in args.tap:
        if spec == "product":
            if not b_files:
                print("--tap product requires --gme", file=sys.stderr)
                return 2
            taps.append(gme_product_code(next(iter(b_files.values()))))
        else:
            taps.append(int(spec, 0))

    def make_config(default_ipt: int) -> MachineConfig:
        return MachineConfig(
            instructions_per_tick=args.instructions_per_tick or default_ipt,
            trace_mmio=args.trace_mmio,
            dac_pacing=args.dac_pacing,
            pacing=args.pacing,
        )

    report: BootReport
    if taps:
        report = run_session(
            firmware,
            taps,
            wav_path=args.wav,
            max_instructions=args.max_instructions or DEFAULT_SESSION_BUDGET,
            config=make_config(SESSION_INSTRUCTIONS_PER_TICK),
            a_dir=args.a_dir,
            b_dir=args.b_dir,
            b_files=b_files or None,
        )
    else:
        report = boot_firmware(
            firmware,
            max_instructions=args.max_instructions or DEFAULT_BOOT_BUDGET,
            config=make_config(20_000),
            a_dir=args.a_dir,
            b_dir=args.b_dir,
            b_files=b_files or None,
        )
    print(report.format_log())
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point: launch the TUI by default; ``--headless`` runs the scripted flow."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--headless" in args:
        args.remove("--headless")
        return run_headless(args)
    from .tui import main as tui_main

    return tui_main(args)


if __name__ == "__main__":
    sys.exit(main())
