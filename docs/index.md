# tt-emu hardware documentation — index

This directory documents the hardware of the Ravensburger tiptoi pen (2nd generation,
"MT") at the level of detail needed to **emulate it** and boot the pen's real firmware
**unmodified**. The SoC is a **Chomptech ZC3202N = Anyka AK1050** ("Snowbird2"), a 64-pin
LQFP part with an **ARM926EJ-S** (ARMv5TEJ, ARM + Thumb) core, little-endian, effectively
identity-mapped (no MMU emulation needed). The firmware runs OS-less: a hand-written PROG
startup, a QHsm statechart, an event-pump main loop, plus a resident boot blob that stays
mapped as the HAL for the pen's whole life.

Each file is **self-contained**: it describes one hardware component's programming
interface (registers, behaviour, timing, data formats) well enough to implement a model of
it without consulting any other source. This index gives the cross-cutting quick-reference
so you can locate facts without opening every file, then open only the component file(s)
you need.

Every fact below is drawn from the component docs (each marks items **Observed** vs.
**Inferred**); nothing here is external. Where a value is Inferred it is flagged.

---

## How to use these docs

**Which doc do I need for X?**

- *Set up the CPU, address space, and boot handoff* → `memory-map-and-boot.md` (the entry
  point; also holds the complete "recipe that boots").
- *Constant/self-clearing core registers, clocks, chip-ID gate, standby, power-off model* →
  `system-control-and-clock.md`.
- *Deliver interrupts, drive the 20 ms tick, pace time* → `interrupts-and-timers.md`.
- *Serve NAND reads/writes at the register or function seam* → `nand-and-nfc-controller.md`
  (the chip + controller + the row/AU **address decode**).
- *Build a bootable flash image (what bytes go where)* → `nand-image-layout.md`.
- *Get a book to actually load and play (drive `b:` mount, the autonomous discovery
  scan, booklist, the tap sequence)* → `nand-image-layout.md` §7.
- *Inject an OID tap the firmware decodes itself* → `oid-sensor.md`.
- *Capture audio / drive the DMA-done interrupt* → `audio-dac-dma.md`.
- *Present pins: buttons, power-hold, amp, straps* → `gpio-buttons-led.md` (the
  authoritative pin map).
- *Dead-bus USB defaults, or a scripted USB-PC mass-storage host* → `usb-musb-device.md`.
- *Keep the battery "healthy" and detect power-off* → `battery-and-power.md`.
- *Pass the anti-clone boot gate* → `zc90b-auth.md`.

**Recommended build order** (each step boots a bit further):

1. `memory-map-and-boot.md` — CPU core, address map, load the artifacts, enter at
   `0x08039100`. Its §5 is the master recipe; §5.3 lists the minimum MMIO contract.
2. `system-control-and-clock.md` + `battery-and-power.md` — the constants that pass the
   boot self-tests (chip-ID, clock latch bits, battery ADC).
3. `nand-and-nfc-controller.md` + `nand-image-layout.md` — the storage mount is a hard
   boot precondition; do these together (controller model + image bytes).
4. `zc90b-auth.md` — the anti-clone gate (fatal on the quiet boot path the emulator takes).
5. `interrupts-and-timers.md` — deliver the timer IRQ so time advances and the statechart
   runs.
6. `gpio-buttons-led.md` + `oid-sensor.md` — input: buttons and taps.
7. `audio-dac-dma.md` — output audio.
8. `usb-musb-device.md` — optional PC content provisioning (mandatory §1 dead-bus defaults
   aside).

---

## Component documents

| file | what it covers (key registers / addresses / facts) |
|------|-----------------------------------------------------|
| `memory-map-and-boot.md` | ARM926EJ-S core; full address map; artifacts in `update3202MT.upd`; the **boot recipe** — enter PC=`0x08039100`, SP=`0x08400000` (stacks must stay inside `[0x08000000,0x08400000)` — the `Utl_UStr*` rule), map PROG@`0x08009000` + nandboot@`0x08000000`/`0x07ff8000`; the RAM globals table; the from-entry seeds (geometry struct `0x08008ca8`, QHsm byte `0x08007e80`, `0x07ffe740`→1); boot self-tests & health checkpoints. |
| `system-control-and-clock.md` | Core block `0x04000000–FF`: chip-ID `+0x00`=`0x30393031`; clock/PLL `+0x04` (bits 13/14/21 read 0); audio clock `+0x08`; clock gate `+0x0C` (**not** a watchdog — there is none); analog-PD `+0x10` bit8 reads 0; standby handshake; boot-tag `+0x54`; **no reset register — power-off is GPIO15**. |
| `interrupts-and-timers.md` | Three IRQ lines (0 audio, 6 USB, 10 timer/GPIO); regs `0x04000034` enable / `0x040000cc` pending / `0x0400004c` 2nd-level / `0x04000018` timer1 (reload 240000 = 20 ms, ack bit28); vector `0x08000018`, IRQ SP `0x083F0000`; system tick `0x08008d24`; 6-slot software-timer table `0x0800895c`; pacing rules. |
| `nand-and-nfc-controller.md` | NFC `0x0404A000` (cmd-list `+0x100`, GO/status `+0x158` bit31), ECC `0x0405B000` (answer `0x7000040`), L2/DMA `0x04010000` + SRAM window `0x08006800`; logical geometry; the **(row,col)→byte-offset AU decode**; primitive ops; the reactive register-level model. |
| `nand-image-layout.md` | On-flash layout: boot blob, system bins (PROG+codepage, linear), block-0 metadata rows (zone table @row 255, bin-info @253/254, maps @200/201, bad-block bitmap @row 2 — zero-filled, never erased); NFTL 8-byte spare tags (1-MiB logical blocks = 8-block spans, raw-row tag keys); two FAT16 superfloppy partitions A: (system) / B: (user `.gme`); the image-builder recipe; **§7 the runtime mount→discovery→tap chain** (drive registration, the autonomous standby-entry discovery scan, `a:/oidfilelist.lst`, booklist, the product-product-content tap sequence, failure signatures). |
| `oid-sensor.md` | Two-wire sensor on GPIO2 (clock) / GPIO9 (data); 32-bit frame `((0x400000\|(N&0x3FFFF))<<9)\|0x100\|0xF0`; capture state `0x08008c08`; tap posts event `0x1060`; 40 ms poll; the inject-a-tap state machine. |
| `audio-dac-dma.md` | DMA engine `0x04010000` (kick bit16, start bit13, spurious-check `0x0401001c`=0); DAC `0x04080000`; S16LE stereo, GME=22050 Hz; line-0 done IRQ; ring `0x08008d30` (vol Q10 `+0x14`=0x108, size `+0x40`); **pace completion = bytes/(4·rate)**; never touch the tick. |
| `gpio-buttons-led.md` | GPIO regs (dir `0x7C`, out `0x80` reads-back, in `0xBC`, int-en/pol `0xE0/E4/F0/F4` read 0); **full pin map**; idle `GPIO_IN` = `0x00003201`; buttons → event `0x105F`; power-hold GPIO15; test-mode chord; no LED, no watchdog. |
| `usb-musb-device.md` | MUSB `0x04070000`: §1 **mandatory dead-bus defaults** (reads 0, never 0xFFFFFFFF; GPIO8=0); §2–6 opt-in scripted host: register map, endpoints, descriptors (VID/PID 0x2546/0xE301), BOT/SCSI, LUN 0 = partition B; BOT phase byte `0x08122718`. |
| `battery-and-power.md` | Battery ADC `0x04000070` bits[19:10] (serve `0x000C0000`), enable `0x04000064`=`0x200`; thresholds 0x300 warn / 0x2C0 final; auto-off > 300 heartbeats (~30 s); **power-off = GPIO15→0** (all shutdown paths); no PMU peripheral. |
| `zc90b-auth.md` | Anti-clone chip on GPIO10 (clock) / GPIO5 (data); 3 challenge bytes → 3 response bytes; S-boxes at `0x080b0078/0178/0278`; `B=tableB[c2&0xbe]; C=tableC[(c1^B)&0xff]; A=tableA[c3&0xd7]`, reply R1=C,R2=B,R3=A; **fatal on the quiet boot path**. |

---

## Quick reference

### MMIO memory map

Bases the firmware touches (from `memory-map-and-boot.md` §2/§2.2 and each peripheral doc).
Sizes are the emulator's safe mapping cover, not a hardware property.

| base | block | doc |
|------|-------|-----|
| `0x00000000` | Mask ROM (64 KiB, "SNOWBIRD2-BIOS"; only if booting from reset) | memory-map-and-boot |
| `0x04000000` | SoC core block: chip-ID, clock/PLL, clock gate, timer1, IRQ enable/pending/2nd-level, ADC/battery, pin-mux, GPIO banks | system-control-and-clock, interrupts-and-timers, gpio-buttons-led, battery-and-power |
| `0x04010000` | L2 buffer / DMA controller (audio-out DMA channel **and** NAND page staging, buffer 4) | audio-dac-dma, nand-and-nfc-controller |
| `0x04036000` | UART / audio-clock block (boot console; `+0x04` bit19 must read 1 for audio bring-up) | system-control-and-clock, audio-dac-dma |
| `0x0404A000` | NAND flash controller (NFC) | nand-and-nfc-controller |
| `0x0405B000` | NAND ECC engine | nand-and-nfc-controller |
| `0x04070000` | USB device controller (MUSB) | usb-musb-device |
| `0x04080000` | Internal audio DAC | audio-dac-dma |
| `0x040A0000` | Dormant peripheral bank (any benign constant works — Inferred) | system-control-and-clock |
| `0x05000000` | HW config block, written once by mask ROM (purpose unconfirmed — Inferred) | memory-map-and-boot, system-control-and-clock |
| `0x06000000` | Second config block (same — Inferred) | memory-map-and-boot, system-control-and-clock |
| `0x07ff0000` / `0x07ff8000` | Resident HAL / boot-SRAM window (nandboot.bin aliased here) | memory-map-and-boot |
| `0x08000000` | Main RAM: boot blob + low globals, then PROG flat from `0x08009000` | memory-map-and-boot |
| `0x08400000` | Headroom (keep mapped); stacks live *below* it — SVC top `0x08400000`, IRQ top `0x083F0000`, both inside `[0x08000000,0x08400000)` (the `Utl_UStr*` range rule) | memory-map-and-boot |

NAND L2 SRAM buffers live inside RAM: buffers 0–4 at
`0x08006000/6200/6400/6600/6800` (buffer 4 = the 512-byte NAND window) plus 64-byte
buffers at `0x08006A00..` (`nand-and-nfc-controller.md` §7).

### Key fixed RAM addresses (load-bearing globals)

| address | what lives there | doc |
|---------|------------------|-----|
| `0x08006800` | NAND L2 buffer 4 (512-B circular SRAM window; tag at offset 0) | nand-and-nfc-controller |
| `0x08006400` / `0x08006600` / `0x08006A40` | USB EP0-TX / bulk-PIO staging; EP0-RX SETUP shadow (pool-derived — verify) | usb-musb-device |
| `0x08007e78` | Audio DMA transaction-tag byte (`0x76`=DAC, `0x75`=mic) | audio-dac-dma |
| `0x08007e80` | QHsm initial-frame byte (state leaf; seed = 1 = splash) | memory-map-and-boot |
| `0x08008874` | Active object (AO) | memory-map-and-boot |
| `0x08008898` | AO event ring (16 × 12-B records; head u16 @+0xC0 = `0x08008958`, tail @+0xC2 = `0x0800895a`) | memory-map-and-boot, interrupts-and-timers |
| `0x0800895c` | Software-timer slot table (6 slots, stride 0xC) | interrupts-and-timers |
| `0x08008c08` | OID capture state struct (`bit_count` @`0x08008c09`, raw word @`0x08008c14`) | oid-sensor |
| `0x08008c60` | Audio chain flag byte (bit0 idle, bit2 active); `0x08008c64` optional cb; `0x08008c91` swallow flag | audio-dac-dma |
| `0x08008c94` | irq_mask push/pop save stack (4-deep) | interrupts-and-timers |
| `0x08008ca8` | NAND driver state block; **device geometry object at `0x08008cc4`** | memory-map-and-boot, nand-and-nfc-controller |
| `0x08008d24` | **System tick** (+1 per timer IRQ, unit 20 ms; ~90 consumers) | interrupts-and-timers |
| `0x08008d2c` | Audio-output singleton ptr (structure body at `0x08008d30`) | audio-dac-dma |
| `0x080089a4` | Game context base (OID working-buffer ptr @+0x20 = `0x080089c4`) | memory-map-and-boot, oid-sensor |
| `0x08121d44` | Statechart state-descriptor table (static .data, in PROG.bin) | memory-map-and-boot |
| `0x08122718` | USB BOT phase byte (.data initializer = 3) | usb-musb-device |
| `0x081da07c` | Standby auto-off counter (u16; > 300 heartbeats → power off) | battery-and-power |
| `0x081da080` | Booklist head (non-NULL ⇔ the standby entry action ran; count u16 at +0) | memory-map-and-boot, nand-image-layout §7 |
| `0x081db730` | Codepage-active flag byte | memory-map-and-boot |
| `0x081db904` | `g_state` | memory-map-and-boot |
| `0x081db984` | Mem-driver vtable "keystone" (.bss; self-built by NFTL init) | memory-map-and-boot |

The OID working buffer (`akoid_buf`, `+4` = current OID) is a heap block reached via the
game context; not a fixed address (`oid-sensor.md` §4).

### IRQ lines (only three are used)

| bit in `0x34`/`0xcc` | line | source | ACK (what de-asserts) | doc |
|---|---|---|---|---|
| bit0 | 0 | Audio / L2-DMA transfer done | ISR clears `0x04010000` bit16 (kick) | interrupts-and-timers, audio-dac-dma |
| bit6 | 6 | USB device controller (MUSB) | reading MUSB status regs | interrupts-and-timers, usb-musb-device |
| bit10 | 10 | Timer1 tick **and** GPIO pin-scan (shared) | timer: write `0x04000018` bit28 | interrupts-and-timers, gpio-buttons-led |

Vector = `0x08000018`; deliver on `(pending & enable) != 0` ∧ CPSR.I==0 ∧ not already in
IRQ mode; return via `subs pc, lr, #4`. Pending must be **level** (0 when idle, drops on
ACK). 2nd-level status `0x0400004c`: bit17 = timer1 fired, bit20 = GPIO cause.

### GPIO pin map (bank 0; from `gpio-buttons-led.md` §2)

Idle retail `GPIO_IN` word = **`0x00003201`**. Dir reg `0x0400007C`: **bit=1 → input**.
Out latch `0x04000080` reads back what was written.

| pin | function | dir | active level | boot value | doc / cross-ref |
|---|---|---|---|---|---|
| 0 | Volume-up button | in | active-LOW | 1 (released) | gpio-buttons-led |
| 1 | Volume-down button | in | active-HIGH | 0 | gpio-buttons-led (also test chord) |
| 2 | OID sensor CLOCK | out | — | 0 | oid-sensor |
| 5 | ZC90B auth DATA (bidir) | out at boot | — | 1 (driven) | zc90b-auth |
| 6 | USB power-path / shutdown strobe | out | 1 = asserted | 0 | usb-musb-device (role Inferred) |
| 7 | Headphone detect | in | 1 = plugged (mutes amp) | 0 | audio-dac-dma |
| 8 | USB / VBUS detect | in (pull) | 1 = cable present | 0 (standalone) | usb-musb-device, battery-and-power |
| 9 | OID sensor DATA | in (idle high) | 0 = frame pending | 1 | oid-sensor |
| 10 | ZC90B auth CLOCK | out | — | 0 | zc90b-auth |
| 11 | Power button | in | active-HIGH | 0 | gpio-buttons-led (static 1 = reboot loop) |
| 12 | Audio mute / pop-suppress | out | 1 = asserted | 0 | audio-dac-dma (role Inferred; ROM strap) |
| 13 | Codec / enable strobe | out | — | 1 | audio-dac-dma (role Inferred; ROM strap) |
| 15 | **POWER-HOLD latch** | out | 1 = stay on, 0 = off | driven 1 | battery-and-power, gpio-buttons-led |
| 16 | Speaker AMP enable | out, read-back | 1 = amp on | 0 | audio-dac-dma (GPIO_IN bit16 mirrors latch) |
| 0xFF | dummy "no-pin" sentinel | — | — | reads 0 | gpio-buttons-led (no-op) |

Pins 3, 4, 14, 17+ and all bank-1 pins are never used. **There is no SoC-driven LED.**

### NAND geometry constants (logical — the contract every layer assumes)

| quantity | value | doc |
|---|---|---|
| data page | 2048 B (4 sectors) | nand-and-nfc-controller §2 |
| spare / OOB per page | 64 B (firmware uses only an 8-byte tag per program unit) | nand-and-nfc-controller §2/§9 |
| pages per block | 64 → block = 128 KiB data + 4 KiB spare | nand-and-nfc-controller §2 |
| blocks per device | 4096 → 512 MiB | nand-and-nfc-controller §2 |
| sector | 512 B | nand-and-nfc-controller §2 |
| **allocation unit (AU)** | **4096 B = 8 sectors = 2 pages**, 32 AU/block (`dev[0x1c]=0x1000`) | nand-and-nfc-controller §2/§3 |
| device geometry struct | `0x08008cc4`: page size `+0x10`=0x800, row stride `+0x14`=256, AU mult `+0x18`=1, AU size `+0x1c`=0x1000 | nand-and-nfc-controller §3 |

Row/AU decode: `block = row>>8`, `au = row & 0xFF` (0..31), `phys_sector = 256·block +
8·au + sub`, `byte_offset = phys_sector·512`. Do **not** treat row as a flat sector index —
that only works under the inauthentic `dev[0x1c]=0x200` fiction.

Physical chip varies by production run: live pen = Hynix **HY27UF084G2B** (ID `0xAD 0xDC`);
the update-image boot probe expects Samsung **K9GAG08U0M** ID `0x9551D3EC` (`nand-and-nfc-controller.md`
§2/§8.5). Only the logical geometry above matters for emulation.

### Boot-critical constants the emulator must present

Compact checklist — each is a hardware-model answer, never a firmware patch.

| what | value | doc |
|---|---|---|
| CPU entry / stacks | PC=`0x08039100`, SP(SVC)=`0x08400000`, SP(IRQ)=`0x083F0000` (stacks must lie inside `[0x08000000,0x08400000)`), SVC mode, IRQs enabled | memory-map-and-boot §5.2 |
| Chip-ID `0x04000000` | constant `0x30393031` ("1090") — **boot gates on it** | system-control-and-clock §2 |
| Clock `0x04000004` | seed `0x4D` (or 0); bits 13/14/21 must read 0 (latch/busy) | system-control-and-clock §3 |
| Analog-PD `0x04000010` bit8 | reads 0 (else `dac_set_rate` spins) | system-control-and-clock §4 |
| Audio-clock `0x04036004` bit19 | reads 1 (else audio bring-up hangs) | audio-dac-dma §5 |
| Battery ADC `0x04000070` | `0x000C0000` (raw 0x300 → scaled ≫ thresholds) | battery-and-power §7 |
| Battery enable `0x04000064` | `0x00000200` (bit9) | battery-and-power §7 |
| INT enable `0x04000034` | default `0xFFFFFFFF` at boot, honored at delivery (who sets bit0/10 first is Inferred) | interrupts-and-timers §7.1 |
| INT pending `0x040000cc` | 0 when idle; assert only on a real event | interrupts-and-timers §2.3 |
| GPIO-int banks `0xE0/E4/F0/F4` | read 0 until written | gpio-buttons-led §6, interrupts-and-timers §2.3 |
| GPIO_IN `0x040000BC` | idle `0x00003201` (GPIO8=0, GPIO11=0) | gpio-buttons-led §2 |
| NFC status `0x0404A158` | read `0x80000000` (ready) | nand-and-nfc-controller §5 |
| ECC status `0x0405B000` | read `0x7000040` (complete + both dirs + pass) | nand-and-nfc-controller §6 |
| NAND status byte | `0xC0` | nand-and-nfc-controller §8.4 |
| NAND READ-ID | `0x9551D3EC` (update image) at `0x0404A150`; `+0x154`=0 | nand-and-nfc-controller §8.5 |
| DMA `0x0401000c` bit13 / `0x0401001c` | bit13 clear when idle; `0x0401001c` reads 0 | audio-dac-dma §2, interrupts-and-timers §2.3 |
| USB window `0x0407xxxx` | reads 0 (never 0xFFFFFFFF); GPIO8=0 | usb-musb-device §1 |
| ZC90B response | S-boxes `0x080b0078/0178/0278`; R1=C,R2=B,R3=A (algorithm above) | zc90b-auth §3 |
| NAND geometry struct | seed 96 B at `0x08008ca8` **or** run the boot probe (RESET/READ-ID) at reset | memory-map-and-boot §5.6, nand-and-nfc-controller §3 |
| QHsm frame `0x08007e80` | = 1 (splash) | memory-map-and-boot §5.6 |
| HAL leaf `0x07ffe740` | return 1 — only if the timer HW isn't cycle-faithful | memory-map-and-boot §5.4/§5.6 |
| Timer1 `0x04000018` | RAM-backed; run while bit27, ack on bit28 write; reload 240000 = 20 ms | interrupts-and-timers §4 |
| Power-off signal | treat **GPIO15 out-latch 1→0** as clean power-off | battery-and-power §5, gpio-buttons-led §4 |

Storage mount (`fs_storage_mount_init`) must return 0 — a hard boot precondition served
entirely by the NAND model + image (`nand-image-layout.md`); no RAM-side seeding
substitutes for it.

---

## Conventions used in these docs

- Addresses are byte addresses, hexadecimal, in the running firmware's address space
  (runtime physical addresses; the firmware is effectively identity-mapped).
- Register widths are 32-bit unless noted; values are little-endian.
- "Runtime address" = the address in the live firmware; noted where it differs from any
  file offset.
- Each component doc marks facts as **Observed** (seen in hardware/firmware behaviour, a
  live-pen RAM dump, or demonstrated in emulation) vs. **Inferred** (deduction) where
  relevant.
