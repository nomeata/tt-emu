"""System control / clock / PLL block (``system-control-and-clock.md``).

The minimum conforming model of §9:

* ``REG_CHIP_ID`` 0x00 — constant ``0x30393031`` ("1090"); **the boot gate**
  (§2); writes ignored;
* ``REG_CLK_DIV`` 0x04 — RAM-backed, seeded ``0x4D`` (faithful, 232 MHz PLL);
  self-clearing latch/busy bits 13/14/21 always read 0 (§3.3) — this satisfies
  every latch poll, the early-boot spin, and makes standby a no-op passthrough
  (§3.4 simplest conforming model);
* ``REG_ANALOG_PD`` 0x10 — RAM-backed, bit8 always reads 0 (the DAC rate-apply
  strobe, §4);
* everything else (0x08 audio clock, 0x0C clock gate — **not a watchdog**,
  0x3C/0x40/0x44 wake, 0x50/0x58/0x5C analog, 0x54 boot tag, 0x74 pin-share) —
  plain read-back-what-you-wrote storage (§1/§9).
"""

from __future__ import annotations

from ..peripheral import MmioRegion, WordRegisterPeripheral

REG_CHIP_ID = 0x00
REG_CLK_DIV = 0x04
REG_ANALOG_PD = 0x10

#: The chip-ID constant ("1090") the boot gates on (§2).
CHIP_ID = 0x30393031
#: Faithful REG_CLK_DIV seed: M=13, A=1, B=0 -> PLL 232 MHz, sysclk 116 MHz (§3.3).
CLK_DIV_SEED = 0x0000_004D
#: Self-clearing latch/busy bits of REG_CLK_DIV. The doc §3.1/§3.3 names bits
#: 13, 14, 21; bit 12 is added because the early-boot clock-latch helper (PROG
#: ``0x08009320``) strobes bit 12 and spins until it reads 0 — a doc gap (bit 12
#: is part of the §3.4 "bits 12-14 sleep/clock-stop request" field, so treating
#: it as self-clearing is consistent and lets the early-boot spin exit).
CLK_DIV_SELF_CLEARING = (1 << 12) | (1 << 13) | (1 << 14) | (1 << 21)
#: Self-clearing rate-apply strobe of REG_ANALOG_PD (bit 8; §4).
ANALOG_PD_SELF_CLEARING = 1 << 8


class SysCon(WordRegisterPeripheral):
    """SoC core system-control registers at 0x04000000 (§1 register map)."""

    name = "syscon"
    base = 0x0400_0000

    def __init__(self) -> None:
        super().__init__()
        self.reset()

    @property
    def regions(self) -> tuple[MmioRegion, ...]:
        # The rest of the 0x040000xx window belongs to intc (0x18/0x34/0x38/
        # 0x4C/0xCC), gpio (0x7C..0xF4), and battery (0x60/0x64/0x70); offsets
        # not claimed by anyone fall back to the machine's RAM-like scratch.
        return (
            MmioRegion(self.base + 0x00, 0x14),  # 0x00, 0x04, 0x08, 0x0C, 0x10
            MmioRegion(self.base + 0x3C, 0x0C),  # 0x3C, 0x40, 0x44 (wake)
            MmioRegion(self.base + 0x50, 0x10),  # 0x50, 0x54, 0x58, 0x5C (analog/boot-tag)
            MmioRegion(self.base + 0x74, 0x04),  # pin-share
        )

    def reset(self) -> None:
        super().reset()
        self._regs[REG_CLK_DIV] = CLK_DIV_SEED

    def read_hook_addrs(self) -> tuple[int, ...]:
        # CHIP_ID is a constant (writes ignored — a stray store must not stick);
        # CLK_DIV and ANALOG_PD have self-clearing latch bits that read back 0.
        # None is a busy-poll target, so keeping their read callback is free.
        return (self.base + REG_CHIP_ID, self.base + REG_CLK_DIV,
                self.base + REG_ANALOG_PD)

    def read_reg(self, offset: int) -> int:
        if offset == REG_CHIP_ID:
            return CHIP_ID
        value = super().read_reg(offset)
        if offset == REG_CLK_DIV:
            return value & ~CLK_DIV_SELF_CLEARING
        if offset == REG_ANALOG_PD:
            return value & ~ANALOG_PD_SELF_CLEARING
        return value

    def write_reg(self, offset: int, value: int) -> None:
        if offset == REG_CHIP_ID:
            return  # constant; ignoring writes is proven safe (§1)
        super().write_reg(offset, value)
