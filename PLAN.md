# tt-emu build plan

Built in three steps, orchestrated as small sequential tasks (one deliverable each).

## Step 1 — Hardware documentation (`docs/`)

Self-contained, implementation-oriented docs for each independent hardware component.
Goal: sufficient *on their own* to implement the emulator. Distilled from prior RE work,
but each file stands alone (no external references).

- [x] `docs/memory-map-and-boot.md` — address space, RAM/ROM/MMIO regions, reset & boot chain, how the firmware image is loaded/entered, the resident HAL
- [x] `docs/system-control-and-clock.md` — SoC system-control block: chip-ID gate, clock/PLL, reset
- [x] `docs/interrupts-and-timers.md` — interrupt controller (pending/enable/mask), CPSR delivery, the periodic timer(s)
- [x] `docs/nand-and-nfc-controller.md` — NAND chip geometry + the NAND flash controller: command interface, DMA, SRAM buffer, spare/ECC
- [x] `docs/nand-image-layout.md` — the on-flash image the firmware expects to boot: boot blob / PROG / codepage placement, zone/partition table, NFTL tag format, the A:/B: FAT filesystems
- [x] `docs/oid-sensor.md` — the optical-ID sensor: 2-wire serial capture, the frame format, how an OID "tap" is injected
- [x] `docs/audio-dac-dma.md` — the audio DAC + DMA path: registers, the done-interrupt, ring buffering, volume, sample format/rate
- [x] `docs/gpio-buttons-led.md` — the GPIO block: input/output latches, the physical pin map (buttons, USB-detect, amp, power-hold, LED, sensor/auth pins)
- [x] `docs/usb-musb-device.md` — the USB device controller (MUSB): registers, enumeration, BOT/SCSI mass-storage, LUN→partition mapping
- [x] `docs/battery-and-power.md` — battery ADC + power management: thresholds, auto-off, charger/USB-detect
- [x] `docs/zc90b-auth.md` — the anti-clone authentication chip: 2-wire challenge/response protocol and algorithm
- [x] `docs/index.md` — finalized: per-file summaries + 6 quick-reference tables

## Step 2 — The emulator (`src/tt_emu/`)

Implemented from **only** `docs/`. High-quality, cross-platform Python.

- [x] CPU + memory map skeleton (Unicorn ARMv5), MMIO dispatch framework, .upd loader, boot recipe, ZC90B; boots to the storage-mount checkpoint (16 tests)
- [x] Hardware peripherals modelled: syscon, interrupts/timer, GPIO, battery, ZC90B, NAND+NFC, OID, audio DAC/DMA (USB = default dead-bus per its doc)
- [x] Boot: authentic NAND image + NAND/NFC controller; firmware mounts A:, reads codepage, passes auth, reaches the event-pump statechart (39 tests)
- [x] Headless / scripted mode: load a GME, boot via FLAG.bin resume, tap product+content, capture audio → WAV (byte-identical media)
- [x] Interactive TUI (Textual): background emulation thread, tap OIDs, real-time audio (sounddevice), state/audio/log panels, buttons (tt-emu-tui)
- [x] Test suite (90 pytest) + in-repo cross-compiled test-firmware toolchain (tt_test.h + Makefile + 7 bare-metal ARM peripheral-contract blobs, run headless)
      (arm cross-compiler only, no external checkout) building tiny dumper.gme-style
      test GMEs, exercised through headless mode

## Step 3 — Firmware awareness / GME debugger

- [x] `docs/firmware-2n-mt.md` — statechart, GME interpreter/registers, recognition fingerprint, tttool-YAML join, live-read checklist
- [x] TUI debugger (firmware-aware): live statechart tree, transition log, GME interpreter + register table, OID->script-line routing, tttool-YAML symbolic names, hook-free (d toggles, --yaml)

## Orchestration conventions

- One subagent per deliverable; **sequential** (avoid session-limit exhaustion; reliable overnight).
- Fable-first; some tasks may be flagged to Opus — that's fine.
- Clean-room: Step-1 agents may read `../firmware-re/`; Step-2 agents read **only** `tt-emu/docs/`.
- Commit after every deliverable (conventional commits; no internal session URLs).

## Doc corrections

All build-discovered doc corrections have been folded into docs/ (see git history).
