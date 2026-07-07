"""Test harness: run tiny bare-metal blobs against the peripheral models.

Loads a raw ARM image (cross-compiled from ``tests/firmware/``, linked at
0x08000000 so its vector table serves the machine's fixed IRQ vector
0x08000018), assembles the same peripheral set the real boot uses — minus the
pieces that need firmware artifacts (ZC90B S-boxes, OID) — and runs the blob
under an instruction budget.

The blob signals through the **result mailbox** (the convention of
``tests/firmware/tt_test.h`` — keep the two in sync): it writes a detail word
to ``MAILBOX_ADDR + 4``, then a status magic (:data:`STATUS_PASS` /
:data:`STATUS_FAIL`) to ``MAILBOX_ADDR``, then parks in a spin loop.  The
harness polls the status word between execution chunks and stops the machine
when it appears.

This is a *test* entry point: the normal boot path (``boot.build_machine``)
is untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .machine import Machine, MachineConfig
from .nand_image import NandImage
from .peripherals import stubs
from .peripherals.audio import AudioDma
from .peripherals.battery import BatteryAdc
from .peripherals.gpio import GpioBlock
from .peripherals.intc import IntcTimer
from .peripherals.nand import EccEngine, NfcController
from .peripherals.syscon import SysCon

__all__ = [
    "BLOB_BASE",
    "MAILBOX_ADDR",
    "STATUS_PASS",
    "STATUS_FAIL",
    "BlobResult",
    "TestBench",
]

#: Load/link/entry address of a test blob (tests/firmware/blob.ld).
BLOB_BASE = 0x0800_0000

#: Result mailbox (tests/firmware/tt_test.h): +0 status magic, +4 detail word.
MAILBOX_ADDR = 0x083F_F000
STATUS_PASS = 0x600D_CAFE
STATUS_FAIL = 0xBAD0_DEAD

#: Initial SVC stack for a blob; start.S immediately re-loads its own
#: ``__stack_top`` (the same value), so this is belt-and-braces.
BLOB_STACK_TOP = 0x0802_0000
_CPSR_SVC_IRQS_ON = 0x13


@dataclass
class BlobResult:
    """Outcome of one blob run."""

    #: ``"pass"`` / ``"fail"`` per the mailbox, or ``None`` if the blob never
    #: signalled (budget exhausted, fault, or a machine-initiated stop such as
    #: the GPIO15 power-off).
    status: str | None
    #: The blob's detail word (mailbox +4).
    detail: int
    #: Why the run ended (:attr:`tt_emu.machine.Machine.stop_reason` or the
    #: budget-exhausted reason).
    stop_reason: str
    #: Machine clock at the end of the run.
    instructions: int

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    def describe(self) -> str:
        return (
            f"status={self.status or 'none'} detail={self.detail:#010x} "
            f"({self.stop_reason}; {self.instructions} instructions)"
        )


class TestBench:
    """A machine wired with the standard peripheral models, for blob runs.

    The NAND backing store defaults to a blank (all-erased) :class:`NandImage`;
    pass a prepared one (or program/tag ``bench.nand`` before running) for
    storage tests.
    """

    #: Not a pytest test class (it is imported into test modules).
    __test__ = False

    def __init__(
        self,
        nand_image: NandImage | None = None,
        config: MachineConfig | None = None,
    ) -> None:
        self.machine = Machine(config)
        self.nand = nand_image or NandImage()
        self.ecc = EccEngine()
        self.nfc = NfcController(self.nand, self.ecc)
        self.gpio = GpioBlock()
        self.intc = IntcTimer(self.gpio)
        self.syscon = SysCon()
        self.audio = AudioDma(self.nfc, self.intc, self.syscon, self.gpio)
        for peripheral in (
            self.syscon,
            self.intc,
            self.gpio,
            BatteryAdc(),
            self.audio,
            self.nfc,
            self.ecc,
            stubs.make_audio_clock_stub(),
            stubs.UsbStub(),
            stubs.make_dac_stub(),
            stubs.make_dormant_bank_stub(),
        ):
            self.machine.add_peripheral(peripheral)
        self.machine.intc = self.intc

    def run_blob(
        self,
        blob: bytes | Path,
        *,
        max_instructions: int = 2_000_000,
    ) -> BlobResult:
        """Load ``blob`` at :data:`BLOB_BASE`, run it, return its verdict.

        ``blob`` is the raw ``.bin`` (bytes or a path to it).  The run ends
        when the blob writes the mailbox status, the machine stops itself
        (fault, power-off), or ``max_instructions`` is exhausted.
        """
        data = blob.read_bytes() if isinstance(blob, Path) else blob
        machine = self.machine
        machine.write_bytes(BLOB_BASE, data)
        machine.write_bytes(MAILBOX_ADDR, bytes(8))
        machine.set_entry_state(BLOB_BASE, BLOB_STACK_TOP, _CPSR_SVC_IRQS_ON)

        def check_mailbox(m: Machine) -> None:
            if m.read_u32(MAILBOX_ADDR) in (STATUS_PASS, STATUS_FAIL):
                m.request_stop("blob signalled a result")

        result = machine.run(max_instructions, on_chunk=check_mailbox)
        status_word = machine.read_u32(MAILBOX_ADDR)
        status = {STATUS_PASS: "pass", STATUS_FAIL: "fail"}.get(status_word)
        return BlobResult(
            status=status,
            detail=machine.read_u32(MAILBOX_ADDR + 4),
            stop_reason=result.reason,
            instructions=result.instructions,
        )
