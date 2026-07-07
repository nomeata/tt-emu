"""Command-line entry point: ``python -m tt_emu`` / ``tt-emu``.

Boots the firmware headless and prints the run log (§5.8 checkpoints and the
stop condition). With ``--tap`` a scripted session runs instead: boot to
standby, inject the taps through the OID sensor model, capture the audio the
firmware plays and (with ``--wav``) write it out — the full
boot → tap product → tap content → WAV chain.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

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
        prog="tt-emu",
        description="Headless tiptoi 2N ('MT') pen emulator — boot the real firmware.",
    )
    parser.add_argument("firmware", help="path to update3202MT.upd")
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
        "--game", metavar="GME", action="append", default=[],
        help=".gme file placed on partition B: (repeatable)",
    )
    parser.add_argument(
        "--tap", metavar="OID", action="append", default=[],
        help="OID to tap once the pen is idle (repeatable, in order); "
        "'product' = the product code of the first --game",
    )
    parser.add_argument(
        "--wav", metavar="FILE", default=None,
        help="write the captured audio (S16LE stereo) to FILE",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="-v: info, -vv: debug"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")

    b_files: dict[str, bytes] = {}
    for game in args.game:
        p = Path(game)
        b_files[p.name] = p.read_bytes()

    taps: list[int] = []
    for spec in args.tap:
        if spec == "product":
            if not b_files:
                print("--tap product requires --game", file=sys.stderr)
                return 2
            taps.append(gme_product_code(next(iter(b_files.values()))))
        else:
            taps.append(int(spec, 0))

    def make_config(default_ipt: int) -> MachineConfig:
        return MachineConfig(
            instructions_per_tick=args.instructions_per_tick or default_ipt,
            trace_mmio=args.trace_mmio,
        )

    report: BootReport
    if taps:
        report = run_session(
            args.firmware,
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
            args.firmware,
            max_instructions=args.max_instructions or DEFAULT_BOOT_BUDGET,
            config=make_config(20_000),
            a_dir=args.a_dir,
            b_dir=args.b_dir,
            b_files=b_files or None,
        )
    print(report.format_log())
    return 0


if __name__ == "__main__":
    sys.exit(main())
