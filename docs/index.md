# tt-emu hardware documentation — index

This directory documents the hardware of the Ravensburger tiptoi pen (2nd generation,
"MT" — Chomptech ZC3202N, an Anyka AK1050-class SoC with an ARM926EJ-S core) at the level
of detail needed to **emulate it** and boot the pen's real firmware unmodified.

Each file is **self-contained**: it describes one hardware component's programming
interface (registers, behaviour, timing, data formats) well enough to implement a model of
it without consulting any other source. Read this index first, then open only the
component file(s) you need.

> Status: skeleton — file summaries and the quick-reference tables below are finalized once
> the component docs are written.

## How to use these docs

- **Implementing the CPU/memory core?** Start with `memory-map-and-boot.md`.
- **Implementing a peripheral?** Open its file; each documents its own MMIO registers,
  side effects, interrupts, and any DMA or data-format details.
- **Preparing a bootable NAND image?** See `nand-image-layout.md` (what bytes go where)
  together with `nand-and-nfc-controller.md` (how the firmware reads them).

## Component documents

| file | component |
|------|-----------|
| `memory-map-and-boot.md` | Address space, memory regions, reset & boot chain, resident HAL |
| `system-control-and-clock.md` | SoC system control: chip-ID gate, clocks/PLL, reset |
| `interrupts-and-timers.md` | Interrupt controller and periodic timer(s) |
| `nand-and-nfc-controller.md` | NAND chip + flash controller (commands, DMA, spare/ECC) |
| `nand-image-layout.md` | On-flash boot image: partitions, NFTL, FAT, placement |
| `oid-sensor.md` | Optical-ID sensor: serial capture, frame format, tap injection |
| `audio-dac-dma.md` | Audio DAC + DMA, done-interrupt, ring buffer, volume, format |
| `gpio-buttons-led.md` | GPIO block: input/output latches, physical pin map |
| `usb-musb-device.md` | USB device controller (MUSB): enumeration, BOT/SCSI mass storage |
| `battery-and-power.md` | Battery ADC + power management, auto-off, charger |
| `zc90b-auth.md` | Anti-clone authentication chip: challenge/response protocol |

## Quick reference

### MMIO region overview (to be confirmed in `memory-map-and-boot.md`)

| base | block |
|------|-------|
| `0x04000000` | System control / clock / GPIO / timer / interrupt / battery-ADC |
| `0x04010000` | DMA engine (audio + NAND channels) |
| `0x0404A000` | NAND flash controller (NFC) |
| `0x04070000` | USB device controller (MUSB) |
| `0x07FF8000` | Resident HAL (boot-code aliased into RAM) |
| `0x08000000` | Main firmware (PROG) load base |

### Conventions used in these docs

- Addresses are byte addresses, hexadecimal, in the running firmware's address space.
- Register widths are 32-bit unless noted; values are little-endian.
- "Runtime address" = the address in the live firmware; noted where it differs from any
  file offset.
- Each component doc marks facts as **Observed** (seen in hardware/firmware behaviour) vs.
  **Inferred** where relevant.
