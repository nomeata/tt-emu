"""Battery ADC — the "healthy battery" constant (``battery-and-power.md`` §7).

* ``0x04000070`` (ADC data) reads the constant ``0x000C0000``: bits [19:10] =
  raw 0x300 → scaled 0x600, far above every threshold. This passes the boot
  calibration (identical averages above the 0x155 floor), keeps the runtime
  monitor permanently OK, and satisfies the firmware-update gate — no timer,
  noise, or discharge curve needed (§7 item 1).
* ``0x04000064`` (enable/channel) is RAM-backed, preset ``0x200`` (bit 9);
  the firmware read-modify-writes bit 9 itself anyway (§7 item 1).
* ``0x04000060`` (channel select) is plain scratch (§1 of
  ``system-control-and-clock.md`` table: ADC block offsets).
"""

from __future__ import annotations

from ..peripheral import MmioRegion, WordRegisterPeripheral

ADC_CHANNEL = 0x60
ADC_ENABLE = 0x64
ADC_DATA = 0x70

#: Constant ADC word: bits[19:10] = 0x300 (healthy; battery-and-power.md §7).
ADC_DATA_HEALTHY = 0x000C_0000
#: Battery-OK comparator preset (bit 9; memory-map-and-boot.md §5.3).
ADC_ENABLE_SEED = 0x0000_0200


class BatteryAdc(WordRegisterPeripheral):
    """The battery ADC slice of the SoC core block."""

    name = "battery"
    base = 0x0400_0000

    def __init__(self) -> None:
        super().__init__()
        self.reset()

    @property
    def regions(self) -> tuple[MmioRegion, ...]:
        return (
            MmioRegion(self.base + ADC_CHANNEL, 0x08),  # 0x60, 0x64
            MmioRegion(self.base + ADC_DATA, 0x04),  # 0x70
        )

    def reset(self) -> None:
        super().reset()
        self._regs[ADC_ENABLE] = ADC_ENABLE_SEED

    def read_reg(self, offset: int) -> int:
        if offset == ADC_DATA:
            return ADC_DATA_HEALTHY
        return super().read_reg(offset)

    def write_reg(self, offset: int, value: int) -> None:
        if offset == ADC_DATA:
            return  # read-only from the firmware's view (§1)
        super().write_reg(offset, value)
