"""Firmware-specific debug support: recognize a known image, expose live readers.

The emulator itself is firmware-agnostic; this package adds *optional*, per-build
debugger views on top. Each supported build has a module with a byte-exact fingerprint
check and hook-free RAM readers (see ``docs/firmware-2n-mt.md``). An unrecognized image
simply gets no debugger — the consumer falls back to its generic panels.

:func:`support_for` resolves an image to a :class:`FirmwareSupport` bundle **once**, so
the TUI and scripting consumers never re-detect the generation or branch on it to build
a debugger / pick a session cadence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterable

from ..firmware_profile import MT, ZC3201, FirmwareProfile
from . import model, mt, zc3201
from .model import FirmwareDebugger

if TYPE_CHECKING:
    from ..boot import BootedMachine
    from ..loader import Firmware
    from .symbols import TttoolSymbols

__all__ = ["FirmwareSupport", "detect", "model", "mt", "support_for", "zc3201"]

#: ZC3201's emulation cadence under its real MMU (``docs/zc3201-boot-feasibility.md``
#: Leg 26). The 1st-gen firmware demand-pages heavily, so a large share of executed
#: instructions are the abort-handler/refiner overhead of servicing faults — work that
#: does not exist on real silicon (hardware paging is ~free). To keep the
#: instruction-counted timer firing in step with *real* firmware progress (as it does on
#: hardware), one 20 ms tick is ~100k instructions, not MT's 20k. At 20k the timer fires
#: far too often relative to fault-slowed progress and the timer-gated boot descent never
#: reaches book mode (the firmware stalls in standby). (Distinct from the
#: ``MmuBoot._fault_addr`` scaled-index bug — the *thrash* root cause — which 100k had
#: merely masked; see Leg 26.) Applied only when the caller left the MT session default.
_ZC3201_INSTRUCTIONS_PER_TICK = 100_000


def detect(prog: bytes) -> str | None:
    """A short label for a recognized PROG image, or None (kept for callers that only
    want the label; :func:`support_for` returns the whole bundle)."""
    if mt.recognize(prog):
        return mt.FIRMWARE_LABEL
    if zc3201.recognize(prog):
        return zc3201.FIRMWARE_LABEL
    return None


@dataclass(frozen=True)
class FirmwareSupport:
    """Everything the consumers need to drive one recognized firmware, resolved once.

    The machine build itself is generation-agnostic (:func:`tt_emu.boot.build_machine`
    auto-detects the profile), so this bundles what's left: the debugger factory (hiding
    the per-generation constructor differences), the session cadence, and the display
    label. Consumers hold a ``FirmwareSupport | None`` and stop re-detecting the
    generation or branching on it to construct the debugger.
    """

    profile: FirmwareProfile
    label: str
    is_zc3201: bool
    #: Session instructions-per-tick this generation needs, or ``None`` to keep the
    #: caller's default (only ZC3201 overrides it — its fault-slowed boot descent).
    default_instructions_per_tick: int | None

    def make_debugger(
        self,
        booted: BootedMachine,
        *,
        gme_files: Iterable[bytes] = (),
        symbols: TttoolSymbols | None = None,
        log: Callable[[str], None] | None = None,
    ) -> FirmwareDebugger:
        """Construct this firmware's hook-free live-reader, hiding the constructor
        difference (MT needs the MMU ``read_va`` translator; ZC3201 reads via its own
        ``machine.mmu`` internally)."""
        blobs = list(gme_files)
        if self.is_zc3201:
            return zc3201.Zc3201Debugger(
                booted.machine, gme_files=blobs, symbols=symbols, log=log
            )
        return mt.MtDebugger(
            booted.machine, gme_files=blobs, symbols=symbols, log=log,
            read_mem=booted.mmu.read_va,
        )


def support_for(firmware: Firmware) -> FirmwareSupport | None:
    """The :class:`FirmwareSupport` for ``firmware``, or ``None`` if unrecognized.

    Uses the same byte-exact fingerprint the debugger modules do, so consumers resolve
    the generation here once instead of calling ``recognize`` ad hoc at each site.
    """
    prog = firmware.prog.data
    if zc3201.recognize(prog):
        return FirmwareSupport(
            profile=ZC3201, label=zc3201.FIRMWARE_LABEL, is_zc3201=True,
            default_instructions_per_tick=_ZC3201_INSTRUCTIONS_PER_TICK,
        )
    if mt.recognize(prog):
        return FirmwareSupport(
            profile=MT, label=mt.FIRMWARE_LABEL, is_zc3201=False,
            default_instructions_per_tick=None,
        )
    return None
