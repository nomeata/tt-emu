# tiptoi 2N ("MT") pen — CPU, memory map, and boot

Implementation reference for the CPU/memory core of the emulator. Everything needed to
load the firmware, set initial CPU state, and reach a running system is in this file.
Peripheral register *behaviour* (NAND controller, DAC/DMA, USB, OID sensor) is specified
in its own documents; this file gives only their bases and the minimum contract the boot
path needs.

Facts are marked **Observed** (byte-read from artifacts, seen in a live-pen RAM dump, or
demonstrated in emulation) or **Inferred** (deduction). Unmarked statements are Observed.

Address convention: all addresses are **runtime physical addresses** (the firmware runs
with an effectively identity mapping, see §1.3). Byte order is little-endian throughout.

---

## 1. SoC / CPU

| Item | Value |
|---|---|
| SoC | Chomptech **ZC3202N** = Anyka **AK1050** ("Snowbird2"), 64-pin LQFP |
| CPU core | **ARM926EJ-S**, ARMv5TEJ |
| Endianness | little-endian |
| Instruction sets | ARM + **Thumb** (interworking used heavily: libc `memset`/`memcpy`/`sprintf` and many veneers are Thumb; `bx`/`blx` with bit0 must work) |
| Reset state | PC = 0x00000000 (mask ROM), ARM state, Supervisor mode |
| Chip-ID | MMIO word `*0x04000000` reads `0x30393031` (ASCII "1090"); firmware **gates boot on it** (see §5.4) |

### 1.1 What the emulator must implement

- ARMv5TE ARM + Thumb, all standard exception entry/return semantics (banked
  SP/LR/SPSR per mode; `subs pc, lr, #4` IRQ return restoring CPSR from SPSR). The
  firmware's IRQ path relies on real mode banking (§5.5).
- `blx`/`bx` interworking, `ldr pc, …` vectors, the usual ARMv5 long-multiply/clz etc.
- **SVC/SWI:** the firmware's fault handlers print diagnostics via **semihosting
  `svc 0xab`** (e.g. "Arithmetic exception: Divide By Zero"). Implementing the
  semihosting write call (or at least logging and returning) is useful for diagnosis;
  it is not on the happy path.

### 1.2 CP15 / caches

- The mask ROM never touches MMU or caches (runs physical, caches off).
- The boot blob's reset handler executes `mcr p15, c1` to force the MMU **off**, later
  builds MMU **section tables** (page-table area `0x08004000`/`0x08004400`) and enables
  I/D caches via SCTLR. One init step enables the D-cache bit while the MMU is still
  off. **Observed.**
- PROG contains a software page-table walker (`hal_virt_to_phys`, nandboot
  `0x08001990`) used to hand physical addresses to the DMA engine, and the NAND driver
  toggles per-4K page attributes as a cache-coherence measure. **Observed.**

**Emulator guidance:** do **not** emulate the MMU. The effective mapping is identity for
all RAM and MMIO the firmware uses (the software page-walk resolving virt==phys on the
relevant ranges confirms this — Observed in emulation; the only non-identity artifact is
the HAL alias, §2 note). Accept and **ignore/neutralize all CP15 writes** (SCTLR, cache
ops, TLB ops). Note for QEMU/Unicorn-class cores: "D-cache enable with MMU off" can raise
a spurious data abort — neutralizing the SCTLR write is required and behaviour-neutral.

### 1.3 Why identity works

The whole system (boot blob, PROG) is linked at its physical load addresses; all literal
pools bake absolute `0x08xxxxxx` / `0x07ffxxxx` / `0x04xxxxxx` addresses. Emulating with
a flat physical address space and the blob mapped at those addresses reproduces exactly
what the code expects. **Observed** (a complete cold boot to the main loop runs this way).

---

## 2. Address-space map

Every region the emulator must map or route. Sizes marked * are the emulator's mapping
size (safe covers of what the firmware touches), not a hardware property.

| Base | Size | Type | Contents / purpose |
|---|---|---|---|
| `0x00000000` | 64 KiB | ROM | **Mask ROM** ("SNOWBIRD2-BIOS"). Exception vectors at 0x0 are 8× `ldr pc,[pc,#168]` trampolines forwarding exceptions 1–7 to `0x08000000 + offset` (reset handled in ROM at 0x20). Only needed if you emulate from reset; the from-entry boot never executes it. |
| `0x04000000` | 2 MiB* | MMIO | **Peripheral register space** (datasheet: 0x04000000–0x040AFFFF). Sub-blocks below. |
| `0x05000000` | 128 KiB* | MMIO | HW config block written once by the mask ROM during init. Purpose unconfirmed (memory-controller config — **Inferred**). RAM-like stub suffices. |
| `0x06000000` | 128 KiB* | MMIO | Same as above (second config block). RAM-like stub suffices. |
| `0x07ff0000` | 64 KiB | RAM | **Resident HAL / boot-SRAM window.** The boot blob's HAL entry points live at `0x07ff8000 + off`; PROG makes ~1800 `bl` calls to 72 targets here (hottest: `0x07ffa1d0`, `0x07ffe740`). Map the **boot blob (nandboot.bin, 0x7e80 bytes) at `0x07ff8000`**. |
| `0x08000000` | 4 MiB | RAM | Main RAM, low part. `0x08000000–0x08009000` = **resident boot blob + low globals** (map nandboot.bin at `0x08000000` too — same bytes as the HAL alias; its PC-relative code is coherent at both addresses). `0x08009000+` = **PROG, flat**. |
| `0x08400000` | 256 KiB* | RAM | Stack/heap headroom above the PROG image. Main SVC stack top `0x08420000`, IRQ stack top `0x0841f000` (emulator-chosen tops inside real RAM — the from-entry boot must supply SP itself, §5.2). |

Total RAM: the firmware uses `[0x07ff0000, ~0x08440000)`. The physical SDRAM is at
least ~4.5 MiB; exact chip size unconfirmed (**Inferred**: 8 MiB in-package, window
based below `0x08000000` so the 0x07ffxxxx HAL region is ordinary RAM). The mask ROM
itself only assumes RAM up to `0x0802ef00` (its stack top) — consistent with a small
always-available on-chip SRAM at the bottom of the window before SDRAM init.

Note on the HAL alias: on real hardware `0x07ff8000` presumably aliases (or holds a
copy of) the resident boot blob — mechanism unknown (MMU alias or SPL-made copy,
**Inferred**). A live-pen RAM dump of `0x07ff8000` reads zeros through the firmware's
*data* path while the CPU demonstrably *executes* there — treat that as a quirk, not
evidence the region is empty. The emulator simply maps the same file at both addresses;
this is proven to work.

### 2.1 RAM sub-map (fixed, load-bearing addresses)

| Range / addr | What lives there |
|---|---|
| `0x08000000–0x08007e80` | Boot blob code (resident: pre-inits, HAL leaves, IRQ plumbing, NAND/NFC driver, OID shifter, timer dispatch). Vector table at `0x08000000` (reset `b` at +0, **IRQ vector at +0x18** → low-level entry `0x08000110`). |
| `0x08004000`, `0x08004400` | MMU section-table area built by the boot blob (unused if you skip the MMU — safe to leave as the blob wrote it). |
| `0x08006000–0x08006aff` | NAND L2-buffer staging SRAM (plain RAM; the NAND driver DMA-stages page data here). |
| `0x08007000–0x08009000` | Early heap + **low globals** (zeroed by the boot blob's reset handler): QHsm frame byte `0x08007e80`; active-object (AO) `0x08008874`; AO event ring `0x08008898` (16 × 12-byte records, head u16 @+0xC0, tail @+0xC2); game context base `0x080089a4` (OID buffer ptr @+0x20 = `0x080089c4`); OID sensor struct `0x08008c08`; **NAND chip/geometry struct `0x08008ca8` (device object `0x08008cc4`)**; system tick `0x08008d24`; audio-output singleton ptr `0x08008d2c`. |
| `0x08009000–0x08389000` | **PROG image, flat** (file offset = address − 0x08009000). Dense code/data to ~`0x0812xxxx`, then .data/.bss. |
| `0x0811e000–0x081ec000` (within PROG span) | High .data/.bss: statechart state-descriptor table `0x08121d44` (static .data, in the image); booklist head `0x081da080`; GME-engine globals `0x081da0xx`; `g_state` `0x081db904`; codepage-active flag byte `0x081db730`; **mem-driver vtable ("keystone") `0x081db984`** (.bss — built at runtime by the firmware itself, §5.6). |
| `0x08420000` | Main stack top (grows down). IRQ stack top `0x0841f000`. |

### 2.2 MMIO sub-blocks (bases + one-line purpose)

| Base | Peripheral |
|---|---|
| `0x04000000` | **SoC core block**: chip-ID (+0x00), clock/PLL (+0x04), per-module clock gates (+0x0C — *not* a watchdog; there is no watchdog), HW timers (+0x18, +0x3C..), top-level IRQ **enable** (+0x34), 2nd-level IRQ status (+0x4C), boot-source scratch tag (+0x54), ADC/battery data (+0x70), pin-mux (+0x74), pull-ups (+0x7C), **GPIO out latch (+0x80)**, misc (+0x9C), **GPIO in (+0xBC)**, top-level IRQ **pending** (+0xCC), GPIO-IRQ enable/polarity banks (+0xE0/+0xE4/+0xF0/+0xF4). |
| `0x04010000` | **L2 buffer / DMA controller** (NAND data staging *and* the audio-out DMA channel: +0x00 ctrl/kick bit16, +0x04 src, +0x08 dest, +0x0C len\|START bit13, +0x1C IRQ status). |
| `0x04036000` | UART / audio-clock block (clock-divider + enable words; the boot console lives here). |
| `0x0404A000` | **NAND flash controller (NFC)**: command list staged at +0x100.., GO/status +0x158 (bit31 = ready), data port +0x150/+0x154. |
| `0x0405B000` | NAND **ECC engine** (start/len word at +0x00, done bits 6/24/25). |
| `0x04070000` | **USB 2.0 device controller** (Mentor MUSB-style; PHY status at +0x0A). |
| `0x04080000` | **Internal audio DAC** (bit0 enable; rate divider set via the core block). |

---

## 3. Firmware artifacts

Everything ships in one distributed update container, `update3202MT.upd`
(11,303,276 bytes; magic `ANYKA106`; version trailer `ANYKA_ID` + `RAV_N0038` at
EOF−0x40). Offsets/sizes read from its 0xA4-byte header (all **Observed**):

| Artifact | .upd offset | Size | Load / destination | Role |
|---|---|---|---|---|
| **nandboot** (boot blob / SPL) | `0x20000` | `0x7e80` | RAM `0x08000000` (+ HAL alias `0x07ff8000`) | 2nd-stage boot loader **and resident HAL**: HW/clock init, NAND driver, timer/IRQ plumbing, low-level leaves PROG calls forever. Header: vectors at +0, generation magic at +0x20 (`ANYKANB1` in this .upd), NAND-geometry descriptor at +0x28, code from ~+0x7c. |
| **PROG** (main firmware) | `0x28000` | `0x380000` | RAM `0x08009000`, **flat, no relocation** | The entire product firmware (build `N0038MT`, 2013-10-09): OS-less main loop, QHsm statechart, FAT/NFTL storage stack, GME interpreter, audio pipeline. On NAND it is stored in the "system bins" area (linear, dual redundant copies), *not* in a FAT filesystem. |
| **codepage** | `0x3a8000` | `0xd6ccc` | **not loaded to RAM** — stays on NAND | Character-encoding conversion database (Win32-NLS-style, 61 codepages) used for FAT/UTF-16 filename conversion. `codepage_load` reads it from NAND on demand during storage-mount; a valid codepage read is a **hard boot precondition** (§5.7). |
| producer | `0x3400` | `0x1cafc` | (factory tool) | Factory/recovery flasher, runs on the chip at base 0x08000000. Not part of normal boot; useful only as ground truth for the NAND layout. |
| to_udisk payload | 7 files @ TOC `0x2800` | ~6.4 MiB | FAT partition **A:** | System-voice archive `VOIMG/Chomp_Voice.bin` + 6 `Language/*.wav` prompts. Optional — firmware boots without them (it just stays silent on system prompts). |

**What the emulator must place in memory to boot:** PROG at `0x08009000`, nandboot at
`0x08000000` *and* `0x07ff8000`. Nothing else is loaded into RAM; everything further
(codepage, FAT content, GME files) is *read through the NAND/storage model at runtime*.

The NAND behind the storage model (geometry for the device struct in §5.6, layout for
the NAND-model document): Hynix **HY27UF084G2B** (ID `0xAD 0xDC`), 2048+64-byte pages,
64 pages/block, 4096 blocks = 512 MiB. On-flash: boot blocks + metadata (blocks 59–63:
maps / bin-info / zone table), system bins (PROG + codepage, linear, dual copies,
blocks ~64–133), then the FAT area — partition **A:** = 30 MiB FAT16 superfloppy
(system/index), **B:** = the USB-exposed user drive (label `tiptoi`) holding the
`.gme` books. The system area is NFTL-**linear** (identity log→phys), which is why
PROG/codepage can be reconstructed flat from the .upd.

---

## 4. Authentic boot chain (real pen)

**Stage 0 — mask ROM** (64 KiB at `0x00000000`, "SNOWBIRD2-BIOS", dumped from the pen
and byte-verified). Reset enters ROM code at `0x20`: writes clock-gate init
`*0x0400000C = 0x63`, enters SVC mode, `sp = 0x0802ef00`, MMU/caches off. Samples boot
straps (GPIO in `0x040000BC`, bits 13/12/9, 5×, pull-ups enabled first): strap →
USB-mass-storage recovery / USB-serial boot / UART console; no strap → normal storage
boot, order **SPI-NOR then NAND**. The NAND path reads the boot image's first page,
requires the exact 8-byte magic **"ANYKANB2" at image+4** (SPI path: at +0x20) and an
image-size descriptor (+0xC NAND / +0x28 SPI); no checksum or signature. On match it
copies the image to **`0x08000000`** (≤ ~188 KiB — must fit below the ROM stack) and
does a hard `pc = 0x08000000` — SVC mode, caches off, the loaded image supplies its own
vectors (ROM trampolines forward all later exceptions to `0x08000000+off`). It records
the boot source in scratch reg `0x04000054`. On no match it falls through to USB
recovery.

**Stage 1 — boot blob ("nandboot" SPL), resident at `0x08000000`.** Reset handler at
`+0x88`: MMU off, zeroes low-globals `0x08008000–0x08009000`, sets per-mode stacks;
then GPIO/clock init, builds MMU section tables + enables caches, PLL up, **chip-ID
gate** (`*0x04000000 == "1090"` or hang), NAND/NFC + NFTL init (reads geometry, scans
blocks), and finally dispatches `ldr pc, [0x0800021c]` → **`0x08039100`**, the PROG
entry. The blob **stays resident**: its vector table keeps owning IRQs, and PROG calls
its HAL leaves (timers, NAND page ops, memcpy/memset, pin control) for the pen's whole
lifetime.

*Gap (matters only for from-reset fidelity):* the ANYKANB1 blob from the .upd
demonstrably does **not** copy PROG into RAM and does not populate `0x07ff8000` (zero
writes to either region when run — Observed in emulation). On the real pen PROG must be
streamed from the NAND system bins by the pen's own (different-generation, undumped)
boot blob, or by a mechanism we haven't captured (**Inferred**). This is irrelevant for
the emulator, which places PROG itself.

**Stage 2 — PROG.** Entry `0x08039100` is a hand-written startup (there is **no**
`.init_array`/`__main`; nothing generic runs before it): 8 pre-init calls **into the
resident boot blob** (targets `0x08003430`, `0x08003d08`, `0x08007798`, `0x08003110`,
`0x08003644`, `0x0800774c`, `0x08007734`, …), then `app_init_main` (`0x08038f5c`:
clock/codec/OID-sensor/heap/event-queue init → `fs_storage_mount_init` → returns),
then the ~10 Hz **event pump** main loop (`0x0800b4a4`) that drains the AO event ring
and drives the QHsm statechart (dispatcher `0x080f2d70`) — splash → standby → book
mode, OID taps, audio.

---

## 5. Practical emulator boot (the recipe that works)

The emulator does **not** run stages 0–1 (see §6). It performs the load the boot chain
would have produced and enters PROG at its real entry, with the resident boot blob
mapped so the pre-inits and HAL run for real. This boots the **unmodified** firmware to
its main loop; everything below is Observed working end-to-end (cold boot ≈ 9–15 M
instructions to the idle pump, then event-driven).

### 5.1 Memory setup

Map the regions of §2. Then:

```
write(0x08009000, PROG.bin)        # 0x380000 bytes; trailing zeros are the .bss
write(0x08000000, nandboot.bin)    # 0x7e80 bytes, resident boot blob
write(0x07ff8000, nandboot.bin)    # same bytes again = the HAL alias
```

Unwritten RAM must read as zero (that *is* the .bss initialization — nothing else
zeroes it in a from-entry boot).

### 5.2 CPU initial state

| Register | Value |
|---|---|
| PC | **`0x08039100`** (PROG entry; ARM state) |
| SP (SVC) | `0x08420000` |
| CPSR | SVC mode, ARM, IRQs initially enabled (the firmware masks/unmasks itself via `0x04000034` and CPSR.I) |
| Other regs | don't-care (the real handoff leaves garbage too) |
| SP (IRQ bank) | set `0x0841f000` when delivering the first IRQ (the from-entry boot skips the blob's per-mode stack setup) |

### 5.3 Minimum MMIO contract for boot

A pragmatic default: back the whole `0x04xxxxxx` space with RAM-like registers
(writes stored, reads return last-written), with these overrides. Values are the
proven-working set:

| Register | Behaviour |
|---|---|
| `0x04000000` | constant `0x30393031` ("1090"). **Boot gates on this.** |
| `0x04000004` | clock/PLL: reads-as-0 boots (180 MHz path). Faithful: seed `0x4d`, and self-clearing latch/busy bits 13/14/21 must read 0 (standby spins on bit13). |
| `0x04000070` | ADC data, battery in bits[19:10]: constant `0x000C0000` (raw 0x300 = healthy). Too low ⇒ shutdown cascade. |
| `0x04000064` | `0x00000200` (bit9 = battery-OK comparator). |
| `0x04000034` | top-level IRQ enable: **persist writes and honor it when delivering IRQs** (the anticlone bit-bang masks IRQs around itself; violating this corrupts the exchange). |
| `0x040000cc` | IRQ pending: bit10 = timer, bit0 = audio DMA-done, bit6 = USB. Assert per your IRQ model (§5.5); a free-running/garbage value causes IRQ storms. |
| `0x0400004c` | 2nd-level IRQ status: bit17 = timer, asserted while the timer IRQ is latched. |
| `0x04000018` | timer1 control: firmware acks by writing bit28 — clear your latched timer-pending on that write. |
| `0x040000bc` | **GPIO input — the behavioural pin model.** Working default `0x3300` adjusted: bit8 **= 0** (no USB cable → the statechart proceeds to standby/book instead of USB-PC mode); bit0 = 1 (VOL+ is active-*low*, released); bit11 = 0 (POWER not held), bit1 = 0 (VOL−); bit9 = OID-sensor serial data (idle 1, driven by the sensor model); bit5 = anticlone-chip data-in when it answers; bit16 mirrors the amp-enable output latch (firmware reads back its own output pad). |
| `0x04000080` | GPIO output latch: RAM-backed, and reads return the last-written value (firmware reads back power-hold/amp bits). |
| `0x040000e0/e4/f0/f4` | GPIO-IRQ enable/polarity banks: RAM-backed, **read 0 if never written** (0xFFFFFFFF makes the IRQ aggregator treat every pin as an active interrupt). |
| `0x0400007c`, `0x0400009c` | seed `0x3100` / `0x4` (pull-up / misc defaults). |
| `0x04010000` block | audio/NAND DMA: `+0x0c` must read not-busy (bit13 = 0), `+0x1c` must read 0 (else the audio ISR treats every IRQ as spurious → storm). Full model in the audio/NFC docs. |
| `0x0404a158` | NFC status: read `0x80000000` (ready). Full command-list model in the NFC doc. |
| `0x04070000` block | USB: unmodelled reads **must return 0** (all-ones reads as "host enumerating" and derails detection). |
| everything else | read `0xFFFFFFFF` is safe *except* the cases above; RAM-like is safer. |

One classic trap: any "wait for pins clear" loop (e.g. the early spin
`while (mask & *0x04000004)`) hangs forever if unmapped MMIO reads all-ones.

### 5.4 Boot self-tests and how to pass them honestly

These run on the unmodified boot path; each has a *hardware-model* answer (never patch
the firmware):

1. **Chip-ID gate** — `app_init_main` (and the SPL) require `*0x04000000 == "1090"`.
   Constant register.
2. **Crystal/clock calibration** — right after starting a HAL timer, PROG probes the
   HAL leaf `0x07ffe740` ("timer busy/expired?"). If the measured tick counts are
   implausible it takes a **fatal blink-LED-and-hang** branch. Making `0x07ffe740`
   return non-zero makes calibration self-abort cleanly ("timer HW unavailable"), which
   is the proven route when the timer HW isn't cycle-accurately modelled.
3. **Anticlone authentication (ZC90B)** — `anticlone_zc90b_verify` (`0x0804c47c`,
   called from `fwupdate_verify_image`) bit-bangs a challenge/response to an external
   crypto chip on GPIO10 (clock) / GPIO5 (data). Failure sets a flag that later hits a
   fatal `b .` at `0x0804e50c` (or a hard power-off on the quiet-boot path). The chip
   must be modelled as a GPIO device; its 3 S-boxes are readable at runtime from the
   loaded PROG image at `0x080b0078/0x080b0178/0x080b0278` (256 B each), response
   `B=tabB[c2&0xbe]; C=tabC[(c1^B)&0xff]; A=tabA[c3&0xd7]`, presented on clock edges.
   The exchange runs ~9 M instructions with IRQs masked — that freeze is authentic.
4. **Storage mount** — `fs_storage_mount_init` must return 0; see §5.7.

### 5.5 IRQ delivery (needed for an autonomous run)

The firmware is fully interrupt-driven once booted (system tick, OID poll, audio).
Deliver the periodic **timer IRQ** (~20 ms real time; ~20 k emulated instructions is a
working cadence):

1. Latch pending: `0x040000cc` bit10 + `0x0400004c` bit17 (gate on enable `0x04000034`
   bit10 and on CPSR.I).
2. Exception entry: `SPSR_irq = CPSR`; switch to IRQ mode (CPSR.I=1, ARM state);
   `LR_irq = interrupted_PC + 4`; `SP_irq = 0x0841f000`; `PC = 0x08000018` — the
   **loaded image's IRQ vector** (nandboot `b 0x08000110` → aggregate handler
   `0x08005534` → 2nd-level timer dispatch `0x08005c8c` → registered timer-slot
   callbacks: tick `0x08008d24`++, OID poll, event posting).
3. The handler acks by writing `0x04000018` bit28 (clear your latch) and returns via
   `subs pc, lr, #4`.

Audio DMA-done uses the same path with pending bit0; its handler clears the DMA kick
(`0x04010000` bit16) and chains the next 1 KiB chunk.

### 5.6 Initial state the from-entry boot must seed

Entering at `0x08039100` skips the mask ROM and most of the SPL's *runtime work*. Almost
everything self-builds — these are the only seeds proven necessary, and each reproduces
a value the skipped stage would have produced (not a behaviour patch):

1. **NAND chip/geometry struct at `0x08008ca8` (96 bytes).** On real HW the SPL's
   read-ID detection fills it; from PROG entry nothing rebuilds it, and a zero device
   object hits a fatal `b .` in a geometry validator. It is a deterministic constant of
   the known chip (Hynix HY27UF084G2B) — byte-verified against a live pen dump
   (32/32 words). Write these 24 little-endian words (96 bytes) at `0x08008ca8`:

   ```
   04010100 00000000 ec000000 00000000 00000000 ffffffff 00000000 02000000
   41000400 02000000 02000000 00080000 00010000 01000000 00100000 c4fc0008
   00fb0008 2cf90008 ccf90008 a4fa0008 68d80308 b8d70308 6cd70308 00000000
   ```
   (i.e. raw bytes `0401010000000000ec000000…`.) Key fields of the embedded device
   object `0x08008cc4` (= struct + 0x1c): +0x10 page
   size 0x800, +0x14 = 256 sectors/block, +0x18 = 1, +0x1c = 0x1000 (allocation-unit
   4 KiB), +0x20.. = read/write/erase leaf pointers into the resident blob.

   > **Caveat — byte `+1` (`0x08008ca9`) must be seeded `0`, not the dumped `1`.** The
   > first word above is `04010100`, so the dump has byte `+1 = 0x01`. That byte is
   > *driver state*, not a geometry constant (the 96 bytes are a **post-boot** dump). The
   > resident blob's NAND-clock-init leaf reads it and, **if it is nonzero, takes a "give
   > up" branch that drives GPIO15→0 (power-off)** ~230k instructions in; with `0` it
   > initialises normally and writes the `1` the dump shows. So seed the 96 bytes above
   > but clear byte `+1` to `0`.

2. **QHsm initial-frame byte `0x08007e80` = 1** (state leaf 1 = splash; 3 = standby
   also works but skips the splash-time OID-subsystem init). The real cold boot's
   startup pre-sets this so the *initial* statechart entry lands directly in
   splash/standby (entry-only, no exit actions); from .bss it is 0, which triggers a
   spurious full root→…→book descent whose state-0 *exit* action clobbers a
   product-load gate byte. Seed it before (or at the first call of) `sm_state_entry`
   `0x080f2e0c`.

3. **HAL leaf return override `0x07ffe740 → 1`** (see §5.4 item 2), *if* your timer HW
   model isn't faithful enough for the calibration to pass on its own.

4. **memcpy behaviour at `0x08003118`.** A Thumb copy veneer (`0x08009c71`, installed
   into the mem-driver vtable) makes a PC-relative `bl` that lands at `0x08003118`. On
   the pen that address holds a HAL memcpy; the .upd blob's bytes at that offset are
   not guaranteed to be it. If the vtable copy garbles (symptom: mount populates
   nothing, later bogus divide-by-zero), implement a plain memcpy leaf at
   `0x08003118` (args r0=dst, r1=src, r2=len, return via lr).

Explicitly **not** needed (they self-build; earlier beliefs to the contrary were wrong):
the mem-driver vtable "keystone" `0x081db984` (the firmware's own NFTL init writes
{+0 alloc `0x08038cf8`, +8 free `0x08038d04`, +0xC copy `0x08009c71`, +0x10 memset
`0x08009c85`} ~270 k instructions into the real mount), the statechart descriptor table
`0x08121d44` (static .data, already in PROG.bin), the FAT signature string, the
allocator (PROG's own heap works). **Zero RAM-dump seeding is required.**

### 5.7 Storage must answer

`app_init_main` → `fs_storage_mount_init` performs, through the resident blob's NAND
driver: geometry checks → a full ~4096-block NFTL scan → partition build →
**codepage load** (reads the codepage bin by physical row; a valid read is a hard
success precondition — an empty answer takes the mount-failure branch = LED-blink
hang) → FAT partition scan of A: (if no FAT16 signature is found the firmware
*formats* A: itself — presenting a valid A: image avoids that detour). All of this is
served by the NAND model (reconstructed from the .upd per §3); the seam can be at the
NFC-register level or at the blob's physical page-read leaves — both work; details in
the NAND/NFC documents. No RAM-side seeding substitutes for it.

### 5.8 Boot health checkpoints

| Milestone | Signal |
|---|---|
| Pre-inits + `app_init_main` **returns** | reaches `0x08038f5c` and comes back; no fatal `b .` |
| Keystone self-built | `*0x081db984 == 0x08038cf8` after the mount |
| Mount OK | `fs_storage_mount_init` returns 0; no format path, no LED-blink hang |
| Main loop idle | event pump spinning at `0x0800b4ac–0x0800b4d4` (ring head==tail at `0x08008958/0x0800895a`), ~9–15 M instructions in |
| Statechart alive | with timer IRQs + GPIO bit8=0: states walk 0→1(splash)→3(standby)→…→13(book) on an OID product tap |

---

## 6. Firmware-generation note (what the emulator runs, and why from-reset is skipped)

- The pen's silicon generation is **"ANYKANB2"** (the mask ROM publishes that string
  and demands it as the boot-image magic — exact 8-byte compare, single gate).
- The update container we possess carries an **"ANYKANB1"**-stamped boot blob (magic at
  +0x20, SPI-style header). The pen's mask ROM, run against it, cannot even locate the
  magic (it reads +4 on the NAND path) → **reject → USB recovery**. Observed by running
  the real dumped ROM. The pen's own ANYKANB2 boot blob lives in its NAND boot blocks
  and has not been dumped.
- This mismatch is **header-only**: the blob's *code* is byte-identical to the code
  resident on the live pen (verified at multiple offsets against RAM dumps). Using it
  as the resident blob/HAL is therefore correct.
- **PROG is the pen's build** (`N0038MT`, 2013-10-09; verified by a 101/106 match of
  the pen's live function-pointer tables — the older non-MT sibling matches 35/106 and
  is *not* interchangeable: same layout, different addresses).

Consequence: the emulator runs the **update3202MT** artifact set, enters at the PROG
entry `0x08039100` with the boot blob resident (§5), and does not attempt the mask-ROM
→ SPL path. Emulating from reset is possible for the ROM itself (it is dumped and its
behaviour above is Observed), but it dead-ends at the generation gate unless the pen's
ANYKANB2 boot blob is ever extracted; nothing downstream of §5 depends on it.
