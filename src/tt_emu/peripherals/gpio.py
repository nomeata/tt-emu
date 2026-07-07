"""GPIO block: registers, pin model, power-hold detection.

Implements ``gpio-buttons-led.md`` §1/§2/§8:

* RAM-backed registers; ``GPIO_OUT`` reads back the last-written word (§1.1);
* ``GPIO_INT_EN``/``GPIO_INT_POL`` read 0 until written (§6);
* ``GPIO_IN`` recomposed on every read: static idle base ``0x00003201``
  (buttons released, no USB, no headphones, OID data idle high, straps high),
  per-pin overrides from attached device models (ZC90B data, OID data, buttons,
  USB detect), and bit16 mirroring the amp-enable output latch (§1.1);
* the **GPIO15 power-hold latch**: a 1→0 transition of output bit 15 is the
  pen's clean power-off and terminates the run (§4, ``battery-and-power.md`` §5).

Device models (ZC90B, later OID/buttons) subscribe to output-latch and
direction-register changes and override input pin levels.
"""

from __future__ import annotations

import logging
from typing import Callable

from ..peripheral import MmioRegion, WordRegisterPeripheral

log = logging.getLogger(__name__)

# Register offsets within the SoC core block (gpio-buttons-led.md §1).
GPIO_DIR0 = 0x7C  # bit = 1 -> input
GPIO_OUT0 = 0x80  # output latch, reads back
GPIO_DIR1 = 0x84
GPIO_OUT1 = 0x88
GPIO_PULL0 = 0x9C
GPIO_PULL1 = 0xA0
GPIO_IN0 = 0xBC
GPIO_IN1 = 0xC0
GPIO_INT_EN0 = 0xE0
GPIO_INT_EN1 = 0xE4
GPIO_INT_POL0 = 0xF0
GPIO_INT_POL1 = 0xF4

#: Composite idle GPIO_IN word for a normal retail boot (§2).
GPIO_IN_IDLE = 0x0000_3201

#: Boot seeds for DIR/PULL from memory-map-and-boot.md §5.3
#: ("0x0400007c, 0x0400009c: seed 0x3100 / 0x4").
GPIO_DIR0_SEED = 0x3100
GPIO_PULL0_SEED = 0x4

PIN_POWER_HOLD = 15  # 1 = stay powered, 0 = power off (§4)
PIN_AMP_ENABLE = 16  # output latch mirrored into GPIO_IN (§1.1)

#: Callback signature for pin watchers: (pin, old_level, new_level).
PinWatcher = Callable[[int, int, int], None]


class GpioBlock(WordRegisterPeripheral):
    """The bank-0/bank-1 GPIO register block plus the behavioural pin model."""

    name = "gpio"
    base = 0x0400_0000

    def __init__(self) -> None:
        super().__init__()
        self._out_watchers: dict[int, list[PinWatcher]] = {}
        self._dir_watchers: dict[int, list[PinWatcher]] = {}
        self._in_overrides: dict[int, int] = {}
        #: Memoized :meth:`input_word` (None = recompose). GPIO_IN is the
        #: firmware's hottest poll target; the composition only changes on
        #: set_input/clear_input or an output-latch write.
        self._in_word: int | None = None
        self.reset()

    @property
    def regions(self) -> tuple[MmioRegion, ...]:
        return (
            MmioRegion(self.base + GPIO_DIR0, 0x10),  # 0x7C, 0x80, 0x84, 0x88
            MmioRegion(self.base + GPIO_PULL0, 0x08),  # 0x9C, 0xA0
            MmioRegion(self.base + GPIO_IN0, 0x08),  # 0xBC, 0xC0
            MmioRegion(self.base + GPIO_INT_EN0, 0x08),  # 0xE0, 0xE4
            MmioRegion(self.base + GPIO_INT_POL0, 0x08),  # 0xF0, 0xF4
        )

    def reset(self) -> None:
        super().reset()
        # Reset values: OUT = 0, INT_EN/POL = 0 (§8 item 1); DIR/PULL boot seeds (§5.3).
        self._regs[GPIO_DIR0] = GPIO_DIR0_SEED
        self._regs[GPIO_PULL0] = GPIO_PULL0_SEED
        self._in_overrides.clear()
        self._in_word = None

    # --- device-model API ------------------------------------------------------------

    def watch_output(self, pin: int, callback: PinWatcher) -> None:
        """Call ``callback(pin, old, new)`` when output-latch bit ``pin`` changes."""
        self._out_watchers.setdefault(pin, []).append(callback)

    def watch_direction(self, pin: int, callback: PinWatcher) -> None:
        """Call ``callback(pin, old, new)`` when direction bit ``pin`` changes (1 = input)."""
        self._dir_watchers.setdefault(pin, []).append(callback)

    def set_input(self, pin: int, level: int) -> None:
        """Drive input pin ``pin`` to ``level`` (device-model override of GPIO_IN)."""
        self._in_overrides[pin] = level & 1
        self._in_word = None

    def clear_input(self, pin: int) -> None:
        """Stop driving ``pin``; it falls back to the idle base word."""
        self._in_overrides.pop(pin, None)
        self._in_word = None

    def out_level(self, pin: int) -> int:
        """Current output-latch level of bank-0 ``pin``."""
        return (self._regs.get(GPIO_OUT0, 0) >> pin) & 1

    def is_input(self, pin: int) -> int:
        """1 if bank-0 ``pin`` is configured as an input (DIR bit = 1)."""
        return (self._regs.get(GPIO_DIR0, 0) >> pin) & 1

    def input_word(self) -> int:
        """Compose the live GPIO_IN bank-0 word (§8 item 2; memoized)."""
        word = self._in_word
        if word is None:
            word = GPIO_IN_IDLE
            for pin, level in self._in_overrides.items():
                word = (word & ~(1 << pin)) | (level << pin)
            # bit16 mirrors the amp-enable output latch (§1.1).
            word = (word & ~(1 << PIN_AMP_ENABLE)) | (
                self.out_level(PIN_AMP_ENABLE) << PIN_AMP_ENABLE
            )
            self._in_word = word
        return word

    def gpio_int_trigger(self) -> int:
        """Armed-and-triggering pin mask for the timer-line GPIO scan.

        ``interrupts-and-timers.md`` §6: for every pin enabled in GPIO_INT_EN,
        trigger on mismatch between the input level and the polarity bank
        (polarity bit set = trigger when the pin reads LOW). Bank 0 only —
        no bank-1 pin is ever used (``gpio-buttons-led.md`` §1).
        """
        enabled = self._regs.get(GPIO_INT_EN0, 0)
        if not enabled:
            return 0
        polarity = self._regs.get(GPIO_INT_POL0, 0)
        return enabled & (polarity ^ self.input_word()) & 0xFFFFFFFF

    # --- register behaviour ------------------------------------------------------------

    def read_reg(self, offset: int) -> int:
        if offset == GPIO_IN0:
            return self.input_word()
        if offset == GPIO_IN1:
            return 0  # no bank-1 pin is used (§1)
        return super().read_reg(offset)

    def write_reg(self, offset: int, value: int) -> None:
        if offset == GPIO_IN0 or offset == GPIO_IN1:
            return  # input registers are read-only
        old = self._regs.get(offset, GPIO_DIR0_SEED if offset == GPIO_DIR0 else 0)
        super().write_reg(offset, value)
        if offset == GPIO_OUT0 and old != value:
            self._in_word = None  # bit16 mirrors the amp-enable latch (§1.1)
            self._notify(self._out_watchers, old, value)
            if (old >> PIN_POWER_HOLD) & 1 and not (value >> PIN_POWER_HOLD) & 1:
                # GPIO15 1->0: the pen released its own supply (§4).
                log.info("GPIO15 power-hold released -> pen powered off")
                if self.machine is not None:
                    self.machine.request_stop("pen powered off (GPIO15 power-hold released)")
        elif offset == GPIO_DIR0 and old != value:
            self._notify(self._dir_watchers, old, value)

    def _notify(self, watchers: dict[int, list[PinWatcher]], old: int, new: int) -> None:
        changed = old ^ new
        for pin, callbacks in watchers.items():
            if (changed >> pin) & 1:
                old_level = (old >> pin) & 1
                new_level = (new >> pin) & 1
                for callback in callbacks:
                    callback(pin, old_level, new_level)
