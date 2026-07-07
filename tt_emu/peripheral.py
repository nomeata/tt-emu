"""Peripheral base interface for the MMIO dispatch layer.

Every hardware model plugs into the :class:`~tt_emu.machine.Machine` through this
interface: a peripheral declares the MMIO range(s) it owns (absolute addresses)
and services ``read``/``write`` calls with offsets relative to its ``base``.

The default register semantics follow the boot contract of
``memory-map-and-boot.md`` §5.3: RAM-like registers (writes stored, reads return
the last-written value, unwritten registers read 0), with peripheral-specific
overrides layered on top via :class:`WordRegisterPeripheral`.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .machine import Machine


@dataclass(frozen=True)
class MmioRegion:
    """One contiguous MMIO address range claimed by a peripheral (absolute)."""

    base: int
    size: int

    @property
    def end(self) -> int:
        """First address past the region."""
        return self.base + self.size

    def __contains__(self, addr: int) -> bool:
        return self.base <= addr < self.end


class Peripheral(abc.ABC):
    """A hardware model attached to the machine.

    Subclasses declare the address ranges they serve via :attr:`regions`
    (may be empty for pure GPIO-attached devices such as the ZC90B auth chip)
    and implement :meth:`read` / :meth:`write`. Offsets passed to those methods
    are relative to :attr:`base`, so sub-blocks of the SoC core window
    (``system-control-and-clock.md`` §1) can all use the documented
    ``0x040000xx`` register offsets directly.
    """

    #: Human-readable name used in trace/log output.
    name: str = "peripheral"
    #: Reference base address; ``read``/``write`` offsets are relative to it.
    base: int = 0

    def __init__(self) -> None:
        self.machine: Machine | None = None

    @property
    def regions(self) -> Sequence[MmioRegion]:
        """MMIO ranges served by this peripheral (absolute addresses)."""
        return ()

    def attach(self, machine: Machine) -> None:
        """Called when the peripheral is registered with a machine."""
        self.machine = machine

    def read(self, offset: int, size: int) -> int:
        """Serve an MMIO read of ``size`` bytes at ``base + offset``."""
        raise NotImplementedError(f"{self.name}: unexpected MMIO read")

    def write(self, offset: int, size: int, value: int) -> None:
        """Serve an MMIO write of ``size`` bytes at ``base + offset``."""
        raise NotImplementedError(f"{self.name}: unexpected MMIO write")

    def tick(self, now: int) -> None:
        """Advance model time; ``now`` is the machine clock (emulated instructions).

        Called by the machine between execution chunks. Peripherals that need
        to schedule events (timer expiry, DMA completion) override this.
        """

    def reset(self) -> None:
        """Return the model to its power-on state."""


class WordRegisterPeripheral(Peripheral):
    """Helper base: a sparse file of 32-bit RAM-like registers.

    Provides byte/halfword access on top of word-granular ``read_reg`` /
    ``write_reg`` hooks (sub-word writes are read-modify-write on the backing
    word). Subclasses override ``read_reg``/``write_reg`` for registers with
    behaviour (constants, self-clearing bits, side effects) and fall back to
    ``super()`` for plain storage — the "RAM-like registers" default of
    ``memory-map-and-boot.md`` §5.3.
    """

    def __init__(self) -> None:
        super().__init__()
        self._regs: dict[int, int] = {}

    def read(self, offset: int, size: int) -> int:
        if size == 4 and not offset & 3:  # fast path: the firmware's usual access
            return self.read_reg(offset) & 0xFFFFFFFF
        word = self.read_reg(offset & ~3) & 0xFFFFFFFF
        shift = (offset & 3) * 8
        return (word >> shift) & ((1 << (size * 8)) - 1)

    def write(self, offset: int, size: int, value: int) -> None:
        word_off = offset & ~3
        if size == 4:
            self.write_reg(word_off, value & 0xFFFFFFFF)
            return
        shift = (offset & 3) * 8
        mask = ((1 << (size * 8)) - 1) << shift
        cur = self.read_reg(word_off) & 0xFFFFFFFF
        self.write_reg(word_off, (cur & ~mask) | ((value << shift) & mask))

    def read_reg(self, offset: int) -> int:
        """Return the 32-bit register at word-aligned ``offset`` (default: stored value)."""
        return self._regs.get(offset, 0)

    def write_reg(self, offset: int, value: int) -> None:
        """Store the 32-bit register at word-aligned ``offset``."""
        self._regs[offset] = value & 0xFFFFFFFF

    def reset(self) -> None:
        self._regs.clear()
