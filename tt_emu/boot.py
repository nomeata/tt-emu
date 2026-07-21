"""Boot recipe: assemble the machine, load artifacts, seed the from-entry state.

Implements ``memory-map-and-boot.md`` §5 (the recipe that works):

* §5.1 place PROG at 0x08009000, nandboot at 0x08000000 and its HAL alias
  0x07FF8000;
* §5.2 seed CPU state: PC = 0x08039100, SP(SVC) = 0x08420000, SVC mode, IRQs
  enabled;
* §5.6 seed the only state the skipped stages would have produced — the NAND
  geometry struct at 0x08008CA8 (96 bytes), the QHsm frame byte at 0x08007E80 =
  1, and (optionally, §5.4 item 2) the HAL leaf override 0x07FFE740 → 1;
* hand off to PROG *through the MMU*: run nandboot ``init2`` to build the page table and
  enable the MMU, and install the abort-driven demand-paging (:mod:`tt_emu.mmu_boot`), so
  PROG runs under its real MMU and the DMA engines read physical memory directly;
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

from .firmware_profile import ZC3201, FirmwareProfile
from .loader import (
    Firmware,
    NANDBOOT_ALIAS_ADDR,
    NANDBOOT_LOAD_ADDR,
    PROG_ENTRY,
    PROG_LOAD_ADDR,
)
from .machine import Machine, MachineConfig
from .mmu_boot import MmuBoot
from .nand_image import NandImage, build_nand_image
from .peripherals.audio import AudioDma
from .peripherals.battery import BatteryAdc
from .peripherals.gpio import GpioBlock
from .peripherals.intc import IntcTimer
from .peripherals.nand import EccEngine, L2NandBuffer, NfcController
from .peripherals.oid import OidSensor
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

#: CPU entry state (§5.2). DOC GAP (found empirically; refines §5.2's
#: "SP = 0x08420000"): the stack must live **inside the pen's real 4-MiB RAM**
#: ``[0x08000000, 0x08400000]`` — the firmware's ``Utl_UStr*`` string
#: routines validate every pointer against exactly that range and silently
#: no-op on stack-resident strings otherwise (their guard:
#: ``addr + 0xF8000000 <= 0x400000``). With the doc's 0x08420000 the game
#: discovery scan builds its ``"B:/"``/``"A:/"`` root paths on the stack,
#: the copy is rejected, and the enumeration silently opens a garbage path
#: (booklist count 0). The firmware itself never references addresses above
#: ~0x081E0000, so the top of real RAM is free for the stack.
SVC_STACK_TOP = 0x0840_0000
CPSR_SVC_IRQS_ON = 0x13  # SVC mode (0x13), ARM state, I = 0

#: Bare-probe return trap (``build_bare_machine``): a mapped low-page address the
#: entry leaf's ``bx lr`` lands on. Matches ``scripts/zc3201_boot_probe.py``.
BARE_LR_SENTINEL = 0x0000_2000

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
        oid: OidSensor,
        audio: AudioDma,
        gpio: GpioBlock,
        mmu: MmuBoot,
    ) -> None:
        self.machine = machine
        self.firmware = firmware
        self.zc90b = zc90b
        self.nand = nand
        self.oid = oid
        self.audio = audio
        self.gpio = gpio
        self.mmu = mmu

    def tap(self, oid: int) -> None:
        """Arm an OID tap; the firmware's own 40 ms poll captures and decodes
        it (``oid-sensor.md`` §6)."""
        self.oid.tap(oid)


def build_machine(
    firmware: Firmware,
    config: MachineConfig | None = None,
    *,
    override_hal_timer_leaf: bool = True,
    nand_image: NandImage | None = None,
    a_files: dict[str, bytes] | None = None,
    b_files: dict[str, bytes] | None = None,
    oid_answer_status_polls: bool = False,
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
    syscon = SysCon()
    oid = OidSensor(gpio, answer_status_polls=oid_answer_status_polls)
    audio = AudioDma(nfc, intc, syscon, gpio)
    machine.add_peripheral(syscon)
    machine.add_peripheral(intc)
    machine.add_peripheral(gpio)
    machine.add_peripheral(BatteryAdc())
    machine.add_peripheral(zc90b)
    machine.add_peripheral(oid)
    machine.add_peripheral(audio)
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

    # --- MMU handoff: run nandboot init2 to build the page table + enable the MMU,
    # and install the abort-driven demand-paging + romboot backing store (mmu_boot).
    # The DMA engines then read physical memory directly (no page-table inversion).
    mmu = MmuBoot(machine, firmware, audio)
    mmu.setup()
    # From here the firmware runs under its own MMU; route CPU-visible reads (read_u*) through
    # it so inspection sees firmware globals at their real (often non-identity) frames.
    machine.mmu = mmu

    # --- CPU entry state (§5.2): PROG's pre-init entry, now under the MMU ----------
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
    return BootedMachine(machine, firmware, zc90b, nand, oid, audio, gpio, mmu)


def build_zc3201_machine(
    firmware: Firmware,
    config: MachineConfig | None = None,
    *,
    profile: FirmwareProfile = ZC3201,
    nand_image: NandImage | None = None,
    a_files: dict[str, bytes] | None = None,
    b_files: dict[str, bytes] | None = None,
    provision: bool = False,
) -> Machine:
    """Assemble a ZC3201 machine and seed it to its real boot-task entry.

    The 1st-gen bring-up substrate (``docs/zc3201-boot-feasibility.md`` "Leg 3").
    Unlike the MT recipe (:func:`build_machine`), ZC3201 needs **no demand-paging
    MMU** — the whole :mod:`tt_emu.mmu_boot` layer is absent here; PROG is loaded
    **flat at its true link base ``0x08008000``** (``profile.prog_load``) so every
    absolute reference resolves as linked (``memory-map-and-boot.md`` §1.3.1). The
    nandboot blob is mapped at ``0x07ff8000`` **and** aliased at ``0x08000000``
    (``profile.nandboot_alias``) exactly like MT, because PROG's HAL veneers call
    ``0x0800xxxx`` (``= 0x07ffxxxx + 0x8000``).

    > The load base is ``0x08008000``, not ``0x08000000``. The reveng project and
    > the lab both used ``0x08000000`` — the same wrong-base mistake MT made before
    > it was corrected to ``0x08009000``. At ``0x08000000`` code executes (it is
    > PC-relative) but every absolute *data* pointer is ``0x8000`` too low; e.g. the
    > clock code reads its CPU-frequency ``.rodata`` table through a baked pointer
    > ``0x080251b8`` that only lands on the real table (file offset ``0x1d1b8``) when
    > PROG sits at ``0x08008000`` — at ``0x08000000`` it aliases into
    > ``fs_storage_mount_init``'s code and the clock-set helper spins forever on a
    > garbage frequency. (Absolute code-pointer tables in the image resolve to valid
    > ARM prologues 423:40 for ``0x08008000`` over ``0x08000000``.)

    The SoC is the same Anyka family, so the MT peripheral models are **reused
    verbatim at their MT addresses**: the core page (:class:`SysCon`,
    :class:`IntcTimer`, :class:`GpioBlock`, :class:`BatteryAdc` at ``0x040000xx``)
    and the **storage trio** (:class:`NfcController` ``0x0404a000`` + :class:`EccEngine`
    ``0x0405b000`` + :class:`L2NandBuffer` ``0x04010000``) serving a
    :class:`NandImage`. The HAL's NAND-ready poll reads NFC ``+0x158`` bit31 and
    stages command-list micro-ops at ``+0x100`` — the identical sequencer the MT NFC
    model implements.

    One ZC3201-specific from-entry seed (authentic — it reproduces state the skipped
    boot ROM / C-runtime would have left): ``profile.bss_seed`` zeroes the low
    working-RAM window between the nandboot alias (ends ``0x08006fe4``) and PROG
    (``0x08008000``), where the HAL IRQ-nesting depth byte lives — otherwise
    ``irq_mask_push`` ``0x07ffdb00`` trips its nesting-overflow guard on the first
    critical section. (With the correct base PROG no longer clobbers this window.)

    ``nand_image`` supplies the NAND backing store; a blank image is used by default
    (the ZC3201 NFTL/FAT image builder is the remaining step — see the doc). No
    hooks: this only loads, seeds and runs the unmodified firmware.
    """
    machine = Machine(config)
    gpio = GpioBlock(in_idle=profile.gpio_in_idle, amp_pin=profile.gpio_amp_pin)
    # ZC3201's timer ISR (nandboot 0x08003d6c) acks the top-level line-10 status
    # (0xCC) before reading the second-level timer-fired bit (0x4C bit17) that
    # gates the HAL software-timer tick; decouple the two latches so the tick —
    # and thus the statechart's periodic event driver — actually runs.
    intc = IntcTimer(gpio, zc3201_timer_ack=True)
    machine.add_peripheral(SysCon(chip_id=profile.soc_chip_id))
    machine.add_peripheral(intc)
    machine.add_peripheral(gpio)
    machine.add_peripheral(BatteryAdc())
    # Audio codec (0x04036000) + DAC/dormant scratch blocks (like MT's build_machine).
    # ZC3201's nandboot codec-command HAL writes 0x04036004 then spins on bit 27
    # (command-complete) — without the codec model that spin never ends once a voice
    # plays, parking the event pump (the OID content tap is then never dispatched).
    machine.add_peripheral(stubs.make_zc3201_audio_codec_stub())
    machine.add_peripheral(stubs.make_dac_stub())
    machine.add_peripheral(stubs.make_dormant_bank_stub())
    machine.intc = intc

    # Storage trio, re-pointed verbatim from MT (same Anyka NFC/ECC/L2 registers).
    # A blank NAND by default; ``provision`` runs the firmware's own producer.bin
    # to format an authentic MtdLib/FatLib image the mount can walk (the standard
    # NAND provisioning path — see :mod:`tt_emu.nand_provision`).
    if nand_image is not None:
        nand = nand_image
    elif provision and profile.producer is not None:
        from .nand_provision import provision_nand_image

        nand = provision_nand_image(firmware, profile)
    elif profile.nand_small_page:
        # Hand-built small-page MtdLib + FAT16 A:/B: image (the ZC3201 twin of
        # MT's build_nand_image) — the default so fs_storage_mount_init mounts.
        from .nand_image_zc3201 import build_zc3201_nand_image

        nand = build_zc3201_nand_image(firmware, a_files=a_files, b_files=b_files)
    else:
        nand = NandImage()
    ecc = EccEngine()
    nfc = NfcController(
        nand, ecc, sram_window=profile.nand_sram_window, read_id=profile.nand_read_id,
        small_page=profile.nand_small_page, page_size=profile.nand_page_size,
    )
    machine.add_peripheral(nfc)
    machine.add_peripheral(ecc)
    machine.add_peripheral(L2NandBuffer(nfc))
    # NOTE: the 0x04010000 block is shared between NAND L2 staging and the audio
    # DAC DMA, but ZC3201's DAC submit encoding differs from MT's (it writes the
    # control word at +0x00 directly — no +0x0c wordcount START — and involves
    # 0x04080000), so MT's AudioDma model does not fire here and its DMA_CTRL
    # kick-clear could spuriously drop the audio IRQ line. Retargeting the audio
    # DAC is the next wall (docs/zc3201-boot-feasibility.md "Leg 18").
    if profile.nand_small_page and profile.nand_spare_surface_strobe:
        # The MtdLib readspare leaf presents the page's OOB at the NAND window
        # head, then reads the 4-byte map tag from window[0]. On hardware the
        # NAND daughterboard advances the L2 window to the spare when the HAL
        # pulses its dedicated spare-surface strobe leaf (nandboot 0x080030d8:
        # sets GPIO out bit 3). That GPIO bit *latches high* and is written on
        # every later GPIO access, so it is not an edge/level signal we can key
        # on; instead we model the hardware effect at the strobe leaf itself —
        # its sole purpose is this spare surface, and it runs only in the
        # readspare path, never during a main data read (nand.py
        # NfcController.surface_spare).
        machine.on_code(profile.nand_spare_surface_strobe, nfc.surface_spare)

    machine.write_bytes(profile.prog_load, firmware.prog.data)
    machine.write_bytes(profile.nandboot_load, firmware.nandboot.data)
    if profile.nandboot_alias is not None:
        machine.write_bytes(profile.nandboot_alias, firmware.nandboot.data)
    if profile.bss_seed is not None:
        addr, size = profile.bss_seed
        machine.write_bytes(addr, b"\x00" * size)

    if profile.nandboot_geom_seed is not None:
        # Seed the nandboot NAND-geometry descriptor's sub-page-transfer count
        # (byte +1) the skipped chip-detect would have set — otherwise the
        # bulk-read primitive reads the large-page default (4 × 512 B) into the
        # 512-byte map-read buffer and overruns the MtdLib manager allocated just
        # after it (FatLib divide-by-zero — see FirmwareProfile.nandboot_geom_seed).
        # Written to the nandboot alias (the read literal targets it) and the load
        # copy, matching NAND_ROW_CYCLES for MT.
        geom_addr, geom_bytes = profile.nandboot_geom_seed
        machine.write_bytes(geom_addr, geom_bytes)
        if profile.nandboot_alias is not None:
            machine.write_bytes(
                profile.nandboot_load + (geom_addr - profile.nandboot_alias),
                geom_bytes,
            )

    if profile.nandboot_shift_seed is not None:
        # Seed the nandboot system-bin shift globals (log2 page/block + plane
        # factor) the skipped nandboot init would derive from the device geometry;
        # the boot-file loader (FUN_0x08000868) needs them to walk a system file's
        # block map (they read 0 from-entry). See FirmwareProfile.nandboot_shift_seed
        # and docs/zc3201-boot-feasibility.md "Leg 16".
        shift_addr, shift_bytes = profile.nandboot_shift_seed
        machine.write_bytes(shift_addr, shift_bytes)

    if profile.nand_dev_geometry is not None:
        # Seed the MtdLib device-geometry struct the skipped nandboot chip-detect
        # (READ-ID -> flash_ic decode) would have written — the ZC3201 analogue of
        # MT's §5.6 NAND_GEOMETRY seed. It lives at 0x08007d94, *inside* the
        # bss_seed window that was just zeroed, so it must be written AFTER. Without
        # it the mount's dev+0x14/dev+0x1c (page geometry) read 0 and
        # fs_storage_mount_init bails before the map read (FUN_0800c208).
        dev_addr, geom = profile.nand_dev_geometry
        machine.write_bytes(dev_addr, geom)

    # OID sensor: the two-wire bit-bang link, re-pointed to the ZC3201 pins /
    # capture-state byte (profile.oid_*). The firmware's own nandboot OID HAL
    # (hal_oid_shift_in 0x08005d80, 40 ms poll callback 0x08005f48 armed at the
    # INIT leaf + book descent) clocks the frame out through the modelled GPIO
    # handshake and posts the tap event into the statechart — nothing hooked.
    oid = OidSensor(
        gpio,
        pin_clock=profile.oid_pin_clock,
        pin_data=profile.oid_pin_data,
        bit_count_addr=profile.oid_bit_count_addr,
    )
    machine.add_peripheral(oid)

    machine.set_entry_state(profile.prog_entry, profile.svc_stack_top, CPSR_SVC_IRQS_ON)
    machine.nand = nand
    machine.oid = oid
    log.info(
        "zc3201 machine for %s (%s): PROG @ %#010x, boot-task entry %#010x",
        profile.label, firmware.boot_generation, profile.prog_load, profile.prog_entry,
    )
    return machine


def build_bare_machine(
    firmware: Firmware,
    config: MachineConfig | None = None,
    *,
    profile: FirmwareProfile = ZC3201,
    entry: int | None = None,
) -> Machine:
    """A minimal from-entry probe: load PROG + nandboot, run from ``entry``.

    Loads PROG + nandboot at the *profile's* addresses (ZC3201: PROG identity at
    ``0x08000000``, nandboot at ``0x07ff8000``) with **no** peripherals and **no**
    MMU — every MMIO read returns 0, exactly the model the ``firmware-re`` lab
    uses for this build. Hook-free: it only loads and runs, so a caller can
    observe how far the unmodified firmware executes from a chosen ``entry``.

    ``entry`` defaults to ``profile.prog_entry`` (ZC3201: ``boot_task_main``); a
    ``bx lr`` back to the low sentinel is trapped as a clean return. Note the boot
    task never returns (it runs the event pump) and, with no SoC peripherals and
    no crt0 ``.bss`` seed, stalls at the HAL IRQ-nesting overflow guard
    (``0x07ffdb14``) — pass a *returning* leaf (e.g. the INIT-state handler
    ``state_init_power_on`` ``0x08030e48``) to observe a bounded run, or use
    :func:`build_zc3201_machine` for the real bootable substrate.
    """
    from unicorn.arm_const import UC_ARM_REG_LR

    machine = Machine(config)
    machine.write_bytes(profile.prog_load, firmware.prog.data)
    machine.write_bytes(profile.nandboot_load, firmware.nandboot.data)
    if profile.nandboot_alias is not None:
        machine.write_bytes(profile.nandboot_alias, firmware.nandboot.data)
    machine.set_entry_state(entry if entry is not None else profile.prog_entry,
                            SVC_STACK_TOP, CPSR_SVC_IRQS_ON)
    # Return-trap: a leaf ending with ``bx lr`` lands on a mapped sentinel in the
    # low (zero) page and stops there, so a probe observes the clean return
    # rather than running off into the zero page.
    machine.uc.reg_write(UC_ARM_REG_LR, BARE_LR_SENTINEL)
    machine.on_code(BARE_LR_SENTINEL, lambda m: m.request_stop("returned to entry sentinel"))
    log.info(
        "bare machine for %s (%s): PROG @ %#010x, entry %#010x",
        profile.label, firmware.boot_generation, profile.prog_load,
        entry if entry is not None else profile.prog_entry,
    )
    return machine
