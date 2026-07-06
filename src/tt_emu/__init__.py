"""tt-emu — a hardware-level emulator of the Ravensburger tiptoi 2N ("MT") pen.

Clean-room implementation built from ``docs/`` only. This package provides the
CPU + memory core (:mod:`tt_emu.machine`), the peripheral framework
(:mod:`tt_emu.peripheral`), firmware loading (:mod:`tt_emu.loader`), the boot
recipe (:mod:`tt_emu.boot`), and a headless runner (:mod:`tt_emu.runner`).
"""

from __future__ import annotations

from .boot import BootedMachine, build_machine
from .loader import Firmware, load_upd
from .machine import Machine, MachineConfig, RunResult
from .peripheral import MmioRegion, Peripheral, WordRegisterPeripheral
from .runner import BootReport, boot_firmware

__all__ = [
    "BootReport",
    "BootedMachine",
    "Firmware",
    "Machine",
    "MachineConfig",
    "MmioRegion",
    "Peripheral",
    "RunResult",
    "WordRegisterPeripheral",
    "boot_firmware",
    "build_machine",
    "load_upd",
]

__version__ = "0.0.1"
