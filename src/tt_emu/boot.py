"""Boot recipe: assemble the machine, load artifacts, seed the from-entry state.

Implements ``memory-map-and-boot.md`` §5 (the recipe that works):

* §5.1 place PROG at 0x08009000, nandboot at 0x08000000 and its HAL alias
  0x07FF8000;
* §5.2 seed CPU state: PC = 0x08039100, SP(SVC) = 0x08420000, SVC mode, IRQs
  enabled;
* §5.6 seed the only state the skipped stages would have produced — the NAND
  geometry struct at 0x08008CA8 (96 bytes), the QHsm frame byte at 0x08007E80 =
  1, and (optionally, §5.4 item 2) the HAL leaf override 0x07FFE740 → 1;
* the peripheral set: the real SysCon (chip-ID/clock gate), IntcTimer, GPIO,
  BatteryAdc, the ZC90B auth chip, and the storage subsystem (NFC + ECC + L2
  buffer serving a built NAND image — ``nand-and-nfc-controller.md`` §10,
  ``nand-image-layout.md`` §6), plus the constant stubs of the boot-constants
  checklist (``index.md``).

The ZC90B S-boxes are read from the loaded PROG image (``zc90b-auth.md`` §4).
"""

from __future__ import annotations

import logging
import struct

from .loader import (
    Firmware,
    NANDBOOT_ALIAS_ADDR,
    NANDBOOT_LOAD_ADDR,
    PROG_ENTRY,
    PROG_LOAD_ADDR,
)
from .machine import Machine, MachineConfig
from .nand_image import NandImage, build_nand_image
from .peripherals.battery import BatteryAdc
from .peripherals.gpio import GpioBlock
from .peripherals.intc import IntcTimer
from .peripherals.nand import EccEngine, L2NandBuffer, NfcController
from .peripherals import stubs
from .peripherals.syscon import SysCon
from .peripherals.zc90b import Zc90bAuth

log = logging.getLogger(__name__)

# --- Boot-time RAM seeds (memory-map-and-boot.md §5.6) -----------------------------

#: QHsm initial-frame byte (state leaf 1 = splash, §5.6 item 2).
QHSM_FRAME_ADDR = 0x0800_7E80
QHSM_FRAME_VALUE = 1

#: NAND chip/geometry struct (96 raw bytes) at 0x08008CA8 (§5.6 item 1). The doc
#: gives the literal byte stream; groups are shown in memory order, so decode the
#: hex directly (do NOT reinterpret groups as little-endian words). Key device
#: fields (device object at 0x08008CC4 = struct+0x1c) then land as: +0x10 page
#: size 0x800, +0x14 = 256, +0x18 = 1, +0x1c = 0x1000 (§5.6 item 1).
NAND_GEOMETRY_ADDR = 0x0800_8CA8
NAND_GEOMETRY_BYTES = bytes.fromhex(
    "04010100" "00000000" "ec000000" "00000000"
    "00000000" "ffffffff" "00000000" "02000000"
    "41000400" "02000000" "02000000" "00080000"
    "00010000" "01000000" "00100000" "c4fc0008"
    "00fb0008" "2cf90008" "ccf90008" "a4fa0008"
    "68d80308" "b8d70308" "6cd70308" "00000000"
)

#: HAL leaf polled during clock/battery calibration (§5.4 item 2 / §5.6 item 3);
#: returning non-zero makes calibration self-abort cleanly when the timer HW is
#: not cycle-faithful.
HAL_LEAF_TIMER_ADDR = 0x07FF_E740

#: The blob's row-address-cycle count byte (``nand-and-nfc-controller.md`` §3:
#: "a global row-address-cycle count (3) ... seeded by the probe"). The .upd
#: blob's static value is **2**; the skipped boot probe would set **3** for
#: this chip class. Found empirically at blob offset 0x79E0 (the address-cycle
#: stager reads it via a literal pool): with 2, every row is emitted truncated
#: to 16 bits and the NFTL scan of blocks >= 256 aliases into blocks 0..255 —
#: the scan then sees duplicate chain heads and erases the system bins.
NAND_ROW_CYCLES_ADDR = 0x0800_79E0
NAND_ROW_CYCLES = 3

#: CPU entry state (§5.2).
SVC_STACK_TOP = 0x0842_0000
CPSR_SVC_IRQS_ON = 0x13  # SVC mode (0x13), ARM state, I = 0

#: A small ARM stub returning 1 (`mov r0,#1; bx lr`), planted at HAL_LEAF_TIMER
#: so the calibration probe reads a non-zero "timer busy/expired" (§5.4 item 2).
_RETURN_ONE_STUB = struct.pack("<II", 0xE3A00001, 0xE12FFF1E)


class BootedMachine:
    """A machine loaded with firmware and seeded to the PROG entry state."""

    def __init__(
        self,
        machine: Machine,
        firmware: Firmware,
        zc90b: Zc90bAuth,
        nand: NandImage,
    ) -> None:
        self.machine = machine
        self.firmware = firmware
        self.zc90b = zc90b
        self.nand = nand


def build_machine(
    firmware: Firmware,
    config: MachineConfig | None = None,
    *,
    override_hal_timer_leaf: bool = True,
    nand_image: NandImage | None = None,
    a_files: dict[str, bytes] | None = None,
    b_files: dict[str, bytes] | None = None,
) -> BootedMachine:
    """Build a machine, load the firmware, register peripherals, and seed boot state.

    ``override_hal_timer_leaf`` plants the return-1 stub at 0x07FFE740 (§5.4
    item 2); leave it on unless the timer model is made cycle-faithful.

    ``nand_image`` supplies a pre-built NAND image; by default one is built
    from the firmware artifacts per ``nand-image-layout.md`` §6, with
    ``a_files``/``b_files`` as the host content of the A: (system) and B:
    (user ``.gme``) partitions.
    """
    machine = Machine(config)

    # --- storage: NAND image + controller trio (nand-and-nfc-controller.md §10) -----
    nand = nand_image or build_nand_image(firmware, a_files=a_files, b_files=b_files)
    ecc = EccEngine()
    nfc = NfcController(nand, ecc)

    # --- peripherals ---------------------------------------------------------------
    gpio = GpioBlock()
    intc = IntcTimer(gpio)
    zc90b = Zc90bAuth(gpio)
    machine.add_peripheral(SysCon())
    machine.add_peripheral(intc)
    machine.add_peripheral(gpio)
    machine.add_peripheral(BatteryAdc())
    machine.add_peripheral(zc90b)
    machine.add_peripheral(L2NandBuffer(nfc))
    machine.add_peripheral(stubs.make_audio_clock_stub())
    machine.add_peripheral(nfc)
    machine.add_peripheral(ecc)
    machine.add_peripheral(stubs.UsbStub())
    machine.add_peripheral(stubs.make_dac_stub())
    machine.add_peripheral(stubs.make_dormant_bank_stub())
    machine.intc = intc

    # --- load artifacts (§5.1) -----------------------------------------------------
    machine.write_bytes(PROG_LOAD_ADDR, firmware.prog.data)
    machine.write_bytes(NANDBOOT_LOAD_ADDR, firmware.nandboot.data)
    machine.write_bytes(NANDBOOT_ALIAS_ADDR, firmware.nandboot.data)

    # ZC90B S-boxes live in the loaded PROG image (zc90b-auth.md §4).
    zc90b.load_tables(machine.read_bytes)

    # --- boot-time RAM seeds (§5.6) ------------------------------------------------
    machine.write_u8(QHSM_FRAME_ADDR, QHSM_FRAME_VALUE)
    machine.write_bytes(NAND_GEOMETRY_ADDR, NAND_GEOMETRY_BYTES)
    # DOC GAP (see report): the doc's 96-byte seed is a *post-boot* live-pen dump
    # in which driver-state byte +1 (0x08008CA9) reads 1. But a from-entry boot
    # must start it at 0: the resident blob's NAND-clock-init leaf (nandboot
    # 0x08002e24) reads this byte and, if nonzero, does gpio_write(15, 0) — the
    # power-off "give up" branch — otherwise increments it 0->1 (reproducing the
    # dumped value). Seeding the dumped 1 makes the pen power off ~228k insns in.
    machine.write_u8(NAND_GEOMETRY_ADDR + 1, 0)
    # Row-address-cycle count the probe would have set (see NAND_ROW_CYCLES).
    # Written to the loaded blob and its HAL alias copy.
    machine.write_u8(NAND_ROW_CYCLES_ADDR, NAND_ROW_CYCLES)
    machine.write_u8(NANDBOOT_ALIAS_ADDR + (NAND_ROW_CYCLES_ADDR - NANDBOOT_LOAD_ADDR),
                     NAND_ROW_CYCLES)
    if override_hal_timer_leaf:
        machine.write_bytes(HAL_LEAF_TIMER_ADDR, _RETURN_ONE_STUB)

    # --- CPU entry state (§5.2) ----------------------------------------------------
    machine.set_entry_state(PROG_ENTRY, SVC_STACK_TOP, CPSR_SVC_IRQS_ON)

    log.info(
        "loaded %s (build %s, boot gen %s): PROG %#x bytes @ %#010x, nandboot %#x bytes",
        firmware.path.name,
        firmware.build_id,
        firmware.boot_generation,
        firmware.prog.size,
        PROG_LOAD_ADDR,
        firmware.nandboot.size,
    )
    return BootedMachine(machine, firmware, zc90b, nand)
