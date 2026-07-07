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
- [ ] Each hardware peripheral as a model (one module per component doc)
- [x] Boot: authentic NAND image + NAND/NFC controller; firmware mounts A:, reads codepage, passes auth, reaches the event-pump statechart (39 tests)
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

## Doc corrections to fold in (found during Step 2, code already handles them)

Applied to docs already: 2a's clock bit-12 self-clear; the geometry seed byte +1 = 0.

Deferred (fold into docs in a final pass — the emulator code already accounts for them):
- **nand-and-nfc-controller.md** — the row-address-cycle count: the boot blob's static
  value is **2** (blob offset 0x79E0), not the probe's 3; a from-entry boot must seed it
  to 3, else rows ≥256 truncate to 16 bits, the NFTL scan sees duplicate chain heads, and
  the system bins get erased. (A §5.6-class seed the docs omit.)
- **nand-image-layout.md** — the factory **bad-block bitmap at row 2**: `mtd_init` reads a
  0x1000-byte bitmap (1 bit/block, set = bad) before partition build; an erased (0xFF) page
  marks every block bad → the mount loops forever. Zero-fill a bitmap at row 2 = no bad
  blocks. (Runtime home of the ASA data; "omittable" is true for the boot loader, not the mount.)
- **nand-and-nfc-controller.md §7** — streaming record size: the boot loader's metadata/scan
  reads use **1024-byte** ECC records (payload from the ECC config word), not a fixed 512.

Found during Step 2c/2d (code fixed or precisely characterized):
- **zc90b-auth.md** — the challenge is delimited by the **GPIO5 direction** (output = start /
  discard stale bits, input = complete → compute), NOT a raw 24-edge count. A spurious
  pre-challenge clock fall exists; an edge-counting model shifts the challenge by one, fails
  auth, and the pen powers off — easily misread as a "standby auto-off". (Fixed in code.)
- **nand-image-layout.md §7.2** — the A: FAT/NFTL **write/create path must commit a directory
  entry that round-trips**: discovery creates a:/oidfilelist.lst (+ log/profile); currently the
  writes reach NAND but a re-read shows no new dir entry, so discovery persists 0 games. (Open —
  the milestone blocker.)
- **oid-sensor.md §7.3** — the standby **decode-vs-(+0x1d re-arm) timing**: the model must
  deliver pen-down and the classified OID in one dispatch (or classify on the first gameplay
  poll), else the first-load gate loses the fresh-standby race. (Open.)
