"""Command-line entry point: ``python -m tt_emu`` / ``tt-emu``.

Boots the firmware headless and prints the run log (§5.8 checkpoints and the
stop condition).
"""

from __future__ import annotations

import argparse
import logging
import sys

from .machine import MachineConfig
from .runner import boot_firmware


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
        default=40_000_000,
        help="instruction budget before giving up (default: 40M)",
    )
    parser.add_argument(
        "--instructions-per-tick",
        type=int,
        default=20_000,
        help="emulated instructions per 20 ms timer tick (default: 20000)",
    )
    parser.add_argument(
        "--trace-mmio", action="store_true", help="log the first few MMIO accesses per address"
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

    config = MachineConfig(
        instructions_per_tick=args.instructions_per_tick,
        trace_mmio=args.trace_mmio,
    )
    report = boot_firmware(
        args.firmware, max_instructions=args.max_instructions, config=config
    )
    print(report.format_log())
    return 0


if __name__ == "__main__":
    sys.exit(main())
