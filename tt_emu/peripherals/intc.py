"""Interrupt controller + timer1 (``interrupts-and-timers.md``).

One peripheral models the top-level interrupt controller (§2) and timer1 (§4),
because they share registers and the line-10 second-level status:

* ``INT_ENABLE`` 0x34 — RAM-backed, honored at delivery time; boot default
  ``0xFFFFFFFF`` (§7.1: the proven-working default, safe because pending never
  asserts without a real event);
* ``INT_PENDING`` 0xCC — read-only level status, 0 when idle (§2.3);
* ``TIMER_STAT_CTRL`` 0x4C — low bits firmware config, bit17 = timer latched,
  bit20 = GPIO-scan cause (computed live from the GPIO banks, §6);
* ``TIMER1_CTRL`` 0x18 — reload bits [25:0], bit27 enable, bit28 write = ACK
  (clears the timer latch); the timer runs on the machine clock at
  ``instructions_per_tick`` per nominal 20 ms period (reload 240000, §4/§7.4);
* 0x38 — write-0-only companion, RAM-backed (§2.1).

Other peripherals raise lines 0 (audio) / 6 (USB) via :meth:`assert_line` /
:meth:`clear_line`; the machine polls :meth:`irq_asserted` between chunks.
"""

from __future__ import annotations

from typing import Callable

from ..peripheral import MmioRegion, WordRegisterPeripheral
from .gpio import GpioBlock

# Register offsets within the SoC core block (§2).
TIMER1_CTRL = 0x18
INT_ENABLE = 0x34
INT_COMPANION = 0x38
TIMER_STAT_CTRL = 0x4C
INT_PENDING = 0xCC

LINE_AUDIO = 0
LINE_USB = 6
LINE_TIMER_GPIO = 10

TIMER1_RELOAD_MASK = 0x03FF_FFFF  # bits [25:0]
TIMER1_ENABLE = 1 << 27
TIMER1_ACK = 1 << 28

STAT_TIMER_FIRED = 1 << 17  # 0x20000
STAT_GPIO_CAUSE = 1 << 20  # 0x100000
STAT_GPIO_SCAN_ENABLE = 1 << 4

#: The firmware's reload value for a 20 ms period (240000 @ 12 MHz, §4).
NOMINAL_RELOAD = 240_000


class IntcTimer(WordRegisterPeripheral):
    """Top-level interrupt controller + timer1 + line-10 second-level status."""

    name = "intc"
    base = 0x0400_0000

    def __init__(self, gpio: GpioBlock) -> None:
        super().__init__()
        self._gpio = gpio
        self._lines: set[int] = set()  # externally asserted lines (0 audio, 6 USB)
        self._timer_latched = False
        self._timer_deadline: int | None = None
        self._timer_period = 0
        self.timer_irqs = 0
        self.reset()

    @property
    def regions(self) -> tuple[MmioRegion, ...]:
        return (
            MmioRegion(self.base + TIMER1_CTRL, 4),
            MmioRegion(self.base + INT_ENABLE, 8),  # 0x34, 0x38
            MmioRegion(self.base + TIMER_STAT_CTRL, 4),
            MmioRegion(self.base + INT_PENDING, 4),
        )

    def reset(self) -> None:
        super().reset()
        self._regs[INT_ENABLE] = 0xFFFF_FFFF  # §7.1 boot default
        self._lines.clear()
        self._timer_latched = False
        self._timer_deadline = None
        self._timer_period = 0

    # --- line API for other peripherals (audio DMA, USB) ------------------------------

    def assert_line(self, line: int) -> None:
        """Assert a top-level pending line (level; stays up until cleared)."""
        self._lines.add(line)

    def clear_line(self, line: int) -> None:
        """De-assert a line (its source-specific ACK happened, §2.1)."""
        self._lines.discard(line)

    # --- status computation --------------------------------------------------------------

    def _line10_asserted(self) -> bool:
        if self._timer_latched:
            return True
        return self._gpio_cause()

    def _gpio_cause(self) -> bool:
        """Live GPIO-scan cause: enabled pin level/polarity mismatch while
        the scan is armed (0x4C bit4) — ``interrupts-and-timers.md`` §6."""
        if not self._regs.get(TIMER_STAT_CTRL, 0) & STAT_GPIO_SCAN_ENABLE:
            return False
        return self._gpio.gpio_int_trigger() != 0

    def pending(self) -> int:
        """The INT_PENDING word: 0 when idle, level per line (§2.2/§2.3)."""
        word = 0
        for line in self._lines:
            word |= 1 << line
        if self._line10_asserted():
            word |= 1 << LINE_TIMER_GPIO
        return word

    def irq_asserted(self) -> bool:
        """Machine-side delivery gate: ``(pending & enable) != 0`` (§3)."""
        if not self._lines and not self._timer_latched:
            # Fast path (polled every chunk): with nothing latched, only a
            # live GPIO-scan cause could assert line 10 — and the scan is
            # usually disarmed, so skip composing the pending word.
            if not self._regs.get(TIMER_STAT_CTRL, 0) & STAT_GPIO_SCAN_ENABLE:
                return False
        return bool(self.pending() & self._regs.get(INT_ENABLE, 0))

    # --- register behaviour ---------------------------------------------------------------

    def read_hook_addrs(self) -> tuple[int, ...]:
        # INT_PENDING / TIMER_STAT_CTRL are computed live; TIMER1_CTRL reads
        # back with the write-only ACK bit masked off (not the raw store). All
        # low-frequency, so keep their read callbacks; INT_ENABLE reads back its
        # last write and is served natively from RAM (seeded below).
        return (self.base + TIMER1_CTRL, self.base + TIMER_STAT_CTRL,
                self.base + INT_PENDING)

    def seed_ram(self, poke: Callable[[int, int], None]) -> None:
        poke(self.base + INT_ENABLE, self._regs.get(INT_ENABLE, 0))

    def read_reg(self, offset: int) -> int:
        if offset == INT_PENDING:
            return self.pending()
        if offset == TIMER_STAT_CTRL:
            value = super().read_reg(offset)
            if self._timer_latched:
                value |= STAT_TIMER_FIRED
            if self._gpio_cause():
                value |= STAT_GPIO_CAUSE
            return value
        return super().read_reg(offset)

    def write_reg(self, offset: int, value: int) -> None:
        if offset == INT_PENDING:
            # Only written 0 on reset/teardown paths ("everything off/clear", §2.2).
            if value == 0:
                self._lines.clear()
                self._timer_latched = False
            return
        if offset == TIMER_STAT_CTRL:
            # Store the firmware's config bits; hardware status bits are computed.
            super().write_reg(offset, value & ~(STAT_TIMER_FIRED | STAT_GPIO_CAUSE))
            return
        if offset == TIMER1_CTRL:
            if value & TIMER1_ACK:
                self._timer_latched = False  # the per-tick ACK (§4 bit28)
            super().write_reg(offset, value & ~TIMER1_ACK)
            self._program_timer(value)
            return
        super().write_reg(offset, value)

    # --- timer model (§4, pacing §7.4) -------------------------------------------------------

    def _program_timer(self, ctrl: int) -> None:
        machine = self.machine
        if machine is None:
            return
        if ctrl & TIMER1_ENABLE:
            reload = ctrl & TIMER1_RELOAD_MASK
            per_tick = machine.config.instructions_per_tick
            # Scale the pacing unit by reload/240000 (reload 240000 = one 20 ms tick).
            self._timer_period = max(1, per_tick * max(reload, 1) // NOMINAL_RELOAD)
            if self._timer_deadline is None:
                self._timer_deadline = machine.clock + self._timer_period
        else:
            self._timer_deadline = None

    def tick(self, now: int) -> None:
        if self._timer_deadline is not None and now >= self._timer_deadline:
            # Latch (level) until the bit28 ACK; do not auto-clear on delivery (§7.2).
            self._timer_latched = True
            self.timer_irqs += 1
            self._timer_deadline += self._timer_period
            if self._timer_deadline <= now:  # don't accumulate backlog
                self._timer_deadline = now + self._timer_period
