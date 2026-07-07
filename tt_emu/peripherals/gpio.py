"""GPIO block: registers, pin model, power-hold detection.

Implements ``gpio-buttons-led.md`` §1/§2/§8:

* RAM-backed registers; ``GPIO_OUT`` reads back the last-written word (§1.1);
* ``GPIO_INT_EN``/``GPIO_INT_POL`` read 0 until written (§6);
* ``GPIO_IN`` recomposed on every read: static idle base ``0x00003201``
  (buttons released, no USB, no headphones, OID data idle high, straps high),
  per-pin overrides from attached device models (ZC90B data, OID data, buttons,
  USB detect), and bit16 mirroring the amp-enable output latch (§1.1);
* the **GPIO15 power-hold latch**: a 1→0 transition of output bit 15 is the
  pen's clean power-off and terminates the run (§4, ``battery-and-power.md`` §5);
* the **boot-held power button** (``nand-image-layout.md`` §7.3.1a): on a real
  power-on the user's finger is still on the power button (GPIO11) when the
  early app-init samples it and latches game-context ``+0x24 = (GPIO11 == 1)``
  — the byte that makes standby auto-descend into book(13). The model holds
  GPIO11 = 1 from reset and releases it right after the first ``GPIO_IN`` read
  that follows the firmware driving the power-hold latch (GPIO15 = 1): the
  app-init sample is exactly that read ("right after configuring the
  power-hold pin"). The release matters — after boot, GPIO11 == 1 in standby
  idle means "power button pressed" and triggers a rescan + soft reboot
  (§7.2's "keep GPIO11 = 0" rule applies *after* the boot-time sample).

Device models (ZC90B, later OID/buttons) subscribe to output-latch and
direction-register changes and override input pin levels.
"""

from __future__ import annotations

import logging
from typing import Callable

from unicorn import UC_HOOK_MEM_READ

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

PIN_POWER_BUTTON = 11  # active HIGH; held from reset through the app-init sample
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
        #: Power button still held from the press that powered the pen on
        #: (``nand-image-layout.md`` §7.3.1a). Released right after the first
        #: GPIO_IN read that follows the power-hold latch (GPIO15) going high —
        #: i.e. the app-init ``+0x24 = (GPIO11 == 1)`` sample sees 1, everything
        #: later (test chord, key scan, standby idle) sees 0.
        self._boot_power_button = True
        #: A firmware write to the read-only GPIO_IN registers is pending
        #: correction: on a RAM-backed page the raw store lands in the backing
        #: RAM, so a one-shot read hook restores the composed word before the
        #: next read (see :meth:`_arm_in_restore`). One flag suffices — GPIO_IN
        #: writes are vanishingly rare (the real firmware never issues one).
        self._in_restore_armed = False
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
        self._boot_power_button = True
        self._invalidate_in()

    # --- RAM-backed register support (machine.py core page) ---------------------------

    def read_hook_addrs(self) -> tuple[int, ...]:
        # GPIO_IN is the firmware's hottest poll target and its read value is a
        # pure function of state the model already tracks — it is served natively
        # from RAM (re-``poke``d whenever an input changes; see _invalidate_in).
        # The boot-held power button's read-once release is handled by a one-shot
        # UC_HOOK_MEM_READ_AFTER armed when the power-hold latch is driven high.
        return ()

    def seed_ram(self, poke: Callable[[int, int], None]) -> None:
        poke(self.base + GPIO_DIR0, self._regs.get(GPIO_DIR0, GPIO_DIR0_SEED))
        poke(self.base + GPIO_PULL0, self._regs.get(GPIO_PULL0, GPIO_PULL0_SEED))
        poke(self.base + GPIO_IN0, self.input_word())

    def _arm_boot_release(self) -> None:
        """Arm a brief read hook reproducing :meth:`read_reg`'s boot-button
        release, but without a permanent callback on the 1.2M GPIO_IN reads.

        The register page is RAM-backed, so ``read_reg`` never runs for GPIO_IN;
        this hook stands in for it across the two reads where it matters. The
        *first* GPIO_IN read after the power-hold latch went high is the
        app-init ``+0x24`` sample (§7.3.1a): it must see the button still held —
        the backing RAM already does, so the hook only clears the boot flag
        (matching ``read_reg``'s "return held, then release"). The *next* read
        pushes the released word and removes the hook, so every subsequent read
        is native again."""
        assert self.machine is not None
        uc = self.machine.uc
        addr = self.base + GPIO_IN0
        fires = [0]

        def on_read(_uc: object, _access: int, _addr: int, _size: int,
                    _value: int, _ud: object) -> None:
            fires[0] += 1
            if fires[0] == 1:
                # The sample: leave the held word the RAM already holds in place
                # (this load reads it); latch the release for the next read.
                self._boot_power_button = False
            else:
                self._invalidate_in()  # push the released word for this load on
                log.info("GPIO11 power button released after the app-init sample")
                uc.hook_del(handle)

        handle = uc.hook_add(UC_HOOK_MEM_READ, on_read, begin=addr, end=addr + 3)

    def _arm_in_restore(self) -> None:
        """Undo a firmware write to a read-only GPIO_IN register: a one-shot read
        hook re-pushes the composed input word (GPIO_IN0) and 0 (GPIO_IN1) to the
        backing RAM before the next read samples them, restoring read-only
        semantics without a permanent callback on the hot GPIO_IN read path."""
        assert self.machine is not None
        self._in_restore_armed = True
        uc = self.machine.uc
        in0 = self.base + GPIO_IN0

        def restore(_uc: object, _access: int, _addr: int, _size: int,
                    _value: int, _ud: object) -> None:
            self.machine.poke_core_reg(in0, self.input_word())  # type: ignore[union-attr]
            self.machine.poke_core_reg(self.base + GPIO_IN1, 0)  # type: ignore[union-attr]
            self._in_restore_armed = False
            uc.hook_del(handle)

        # Cover both GPIO_IN words so a write to either is corrected on any read.
        handle = uc.hook_add(UC_HOOK_MEM_READ, restore, begin=in0,
                             end=self.base + GPIO_IN1 + 3)

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
        self._invalidate_in()

    def clear_input(self, pin: int) -> None:
        """Stop driving ``pin``; it falls back to the idle base word."""
        self._in_overrides.pop(pin, None)
        self._invalidate_in()

    def _invalidate_in(self) -> None:
        """Drop the memoized GPIO_IN word; when the register page is RAM-backed
        (native reads), eagerly recompose it and push the value into the backing
        RAM so the firmware's next native read sees the current input state."""
        self._in_word = None
        if self.machine is not None and self.machine.ram_page_active:
            self.machine.poke_core_reg(self.base + GPIO_IN0, self.input_word())

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
            if self._boot_power_button:
                # The power-on press is still held (§7.3.1a); an explicit
                # set_input(11, …) below would win, as any later press does.
                word |= 1 << PIN_POWER_BUTTON
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
            word = self.input_word()
            if self._boot_power_button and self.out_level(PIN_POWER_HOLD):
                # This is the first GPIO_IN read after the firmware drove the
                # power-hold latch (GPIO15 = 1, first thing in app_init_main) —
                # the app-init sample that latches game-context +0x24 =
                # (GPIO11 == 1) "right after configuring the power-hold pin"
                # (nand-image-layout.md §7.3.1a). The sample sees the button
                # still held; the finger lifts right after.
                self._boot_power_button = False
                self._in_word = None
                log.info("GPIO11 power button released after the app-init sample")
            return word
        if offset == GPIO_IN1:
            return 0  # no bank-1 pin is used (§1)
        return super().read_reg(offset)

    def write_reg(self, offset: int, value: int) -> None:
        if offset == GPIO_IN0 or offset == GPIO_IN1:
            # Input registers are read-only. On a RAM-backed page the CPU store
            # has already landed in the backing RAM (the write hook cannot veto
            # it); restore the composed word before the next read.
            if (not self._in_restore_armed and self.machine is not None
                    and self.machine.ram_page_active):
                self._arm_in_restore()
            return
        old = self._regs.get(offset, GPIO_DIR0_SEED if offset == GPIO_DIR0 else 0)
        super().write_reg(offset, value)
        if offset == GPIO_OUT0 and old != value:
            self._notify(self._out_watchers, old, value)
            if (old ^ value) & (1 << PIN_AMP_ENABLE):
                self._invalidate_in()  # bit16 mirrors the amp-enable latch (§1.1)
            if (self._boot_power_button and self.machine is not None
                    and self.machine.ram_page_active
                    and not (old >> PIN_POWER_HOLD) & 1
                    and (value >> PIN_POWER_HOLD) & 1):
                # GPIO15 0->1: the app-init just configured the power-hold pin;
                # its GPIO11 sample is the next GPIO_IN read (§7.3.1a). Arm the
                # one-shot that releases the boot-held button right after it.
                self._arm_boot_release()
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
