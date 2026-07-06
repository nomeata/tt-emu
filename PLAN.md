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
- [ ] `docs/battery-and-power.md` — battery ADC + power management: thresholds, auto-off, charger/USB-detect
- [ ] `docs/zc90b-auth.md` — the anti-clone authentication chip: 2-wire challenge/response protocol and algorithm
- [ ] `docs/index.md` — finalize: accurate per-file summaries + quick-reference tables (memory map, register blocks, IRQ lines, GPIO pins)

## Step 2 — The emulator (`src/tt_emu/`)

Implemented from **only** `docs/`. High-quality, cross-platform Python.

- [ ] CPU + memory map skeleton (Unicorn ARMv5), MMIO dispatch framework
- [ ] Each hardware peripheral as a model (one module per component doc)
- [ ] Boot: present an authentic NAND image; run the real firmware to a running state
- [ ] Headless / scripted mode (load a GME, inject taps, capture audio → WAV)
- [ ] Interactive TUI (Textual): tap OIDs, live audio (sounddevice), controls
- [ ] Test suite (pytest) + **in-repo test-firmware toolchain**: a C header + Makefile
      (arm cross-compiler only, no external checkout) building tiny dumper.gme-style
      test GMEs, exercised through headless mode

## Step 3 — Firmware awareness / GME debugger

- [ ] `docs/firmware-<id>.md` — the specific firmware we target: statechart layout, the GME interpreter model, register semantics, the symbols the TUI can surface
- [ ] TUI enrichment when the firmware is recognized: live hierarchical-state view, a state-transition log, GME interpreter state + "registers", a rich GME debugger; use tttool `.yaml` (when present) for symbolic OID / script-line names

## Orchestration conventions

- One subagent per deliverable; **sequential** (avoid session-limit exhaustion; reliable overnight).
- Fable-first; some tasks may be flagged to Opus — that's fine.
- Clean-room: Step-1 agents may read `../firmware-re/`; Step-2 agents read **only** `tt-emu/docs/`.
- Commit after every deliverable (conventional commits; no internal session URLs).
