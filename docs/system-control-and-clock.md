# System control, clock/PLL, and reset (SoC core block at 0x04000000)

Implementation reference for the **system-control sub-block** of the SoC core register
window `0x04000000–0x040000FF` on the tiptoi 2N ("MT") pen SoC (Chomptech ZC3202N =
Anyka AK1050 class, ARM926EJ-S). This chip has no public datasheet; everything below is
reverse-engineered from the firmware and verified in emulation.

This file covers: the **chip-ID register** the firmware gates boot on, the **clock/PLL
divider**, the **audio-clock divider and clock-apply strobe**, the **per-module clock
gate**, the **standby/wake handshake**, **analog-power and pin-mux configuration**, the
**boot-source scratch register**, and **reset/power-off** behaviour.

Not covered here (same 0x040000xx window, own documents):

| offsets | block | document |
|---|---|---|
| `+0x18`, `+0x34`, `+0x38`, `+0x4C`, `+0xCC` | timer 1, IRQ enable/pending/status | see `interrupts-and-timers.md` |
| `+0x7C/84`, `+0x80/88`, `+0x9C/A0`, `+0xBC/C0`, `+0xE0/E4`, `+0xF0/F4` | GPIO direction/out/pull/in/int banks | see `gpio-buttons-led.md` |
| `+0x60`, `+0x64`, `+0x70` | ADC channel select / control / data (battery) | see `battery-and-power.md` |
| `+0x68` | codec volume / audio path enables | see `audio-dac-dma.md` |

Facts are marked **Observed** (read from firmware disassembly, a live-pen dump, or
demonstrated in emulation) or **Inferred** (deduction from register wiring). Unmarked
statements are Observed. Registers are 32-bit, little-endian, at
`0x04000000 + offset`.

---

## 1. Register map (this sub-block)

| off | name | access | reset/expected | meaning |
|---|---|---|---|---|
| `0x00` | `REG_CHIP_ID` | R (const) | **`0x30393031`** = ASCII "1090" | Chip identification. **Boot gates on this exact value** (§2). Writes occur once from the mask ROM during init (config use, Inferred) — ignoring writes is proven safe. |
| `0x04` | `REG_CLK_DIV` | R/W, self-clearing bits 13/14/21 | seed `0x0000004D` (faithful) or `0` (works) | PLL multiplier + system-clock dividers + sleep request + VBAT-ADC mode (§3). |
| `0x08` | `REG_CLK_AUDIO` | R/W | 0 | Audio/DAC and ADC-path clock dividers and enables (§4). |
| `0x0C` | `REG_CLK_GATE` | R/W | ROM writes `0x63` at reset | Per-module clock gate, bit=1 → module clock **off** (§5). **Not a watchdog — the SoC has no watchdog anywhere; there is nothing to kick.** |
| `0x10` | `REG_ANALOG_PD` | R/W, self-clearing bit 8 | 0 | Analog power-down + DAC clock-apply strobe (§4). bit9: 1 = DAC powered down. |
| `0x3C` | `REG_WAKE_POLARITY` | R/W | 0 | Wake-source polarity/level select. bit9 = USB-cable wake source. (Inferred role; Observed writes.) |
| `0x40` | `REG_WAKE_STATUS` | R/W | 0 | Wake status; firmware writes `0xFFFFFFFF` then `0` to clear all before standby. (Inferred W1C-style; Observed writes.) |
| `0x44` | `REG_WAKE_ENABLE` | R/W | 0 | Wake-source enable mask (compact pin ids). bit9 = USB-detect wake source. |
| `0x50` | analog/pad control | R/W | 0 | Boot-time clock/periph init ORs in `0x280000`; bit3 pulsed together with `0x58` bit17 as an analog-block enable strobe. |
| `0x54` | `REG_BOOT_TAG` | R/W (plain storage) | 0 | **Software scratch.** The mask ROM records its boot source here (§8); the loaded firmware never reads it. Not hardware-latched. |
| `0x58` | `REG_ANALOG_PWR` | R/W | 0 | Multi-field analog/power control: bit2(+bit4) = USB-detect/PHY enable; bit5 = USB-PLL power-up; bits[13:11] = mic/analog input select; bit16 = inverted power-down; bit17 = analog-block enable; **bits[26:24] = OID-sensor power** (firmware writes `&0xF9FFFFFF | 0x1000000`); bits[30:28] = ADC channel select. |
| `0x5C` | `REG_CODEC_BIAS` | R/W | 0 | Codec analog bias/ramp fields (bits[15:13], [22:21]; composite value `0xE1190C1F` written during codec init); bit30 set during USB-PLL power-up and standby entry. |
| `0x74` | `REG_PIN_SHARE` | R/W | 0 | Pin-mux / pin-share. bit0 = UART0 console pins. Firmware does read-modify-write only, never depends on a read-back value beyond its own writes → RAM-like backing suffices. NAND routing: `&= ~0xBA`; USB routing: `&= ~0x198, |= 0x110`. Mask ROM clears bit0 (`&= ~1`) before sampling boot straps. |

All of these except `REG_CHIP_ID` (constant) and the self-clearing bits in
`REG_CLK_DIV`/`REG_ANALOG_PD` behave as plain read-back-what-you-wrote storage from the
firmware's point of view.

---

## 2. The chip-ID gate (0x04000000)

`REG_CHIP_ID` must read the constant **`0x30393031`** — the four ASCII bytes
`'1' '0' '9' '0'` in memory order, i.e. the string **"1090"** (the Anyka-internal chip
name is "snowbird2"). Three independent firmware sites check it (all Observed):

1. **Boot blob (SPL/`nandboot`)**: its hardware-init `main` compares `*0x04000000`
   against "1090" and **hangs** on mismatch, before it dispatches to the main firmware.
2. **`app_init_main`** (main firmware init, runtime `0x08038f5c`): boots only if the
   compare succeeds; on mismatch the statechart tables are never installed and the pen
   never reaches the event pump. This is the gate every emulated boot passes through.
3. **Storage/FAT library self-check** (`anyka_chipid_check`, runtime `0x081150c4`, and a
   debug twin at `0x080fd53c`): a case table of known Anyka chips keyed by a
   `get_chip_type` callback; **case 5 = value "1090" at `0x04000000`** is this chip. On
   mismatch it zeroes the storage/FAT operation tables (log string: "your ic isn't anyka
   ic!") — the pen would boot but all storage would be dead.

**Emulator contract:** return the constant `0x30393031` for every read of `0x04000000`;
ignore writes. Anything else aborts or cripples boot.

---

## 3. Clock / PLL divider (0x04000004, `REG_CLK_DIV`)

One register carries the PLL multiplier, two system-clock dividers with self-clearing
latch strobes, the sleep/clock-stop request, and the battery-ADC mode bit.

### 3.1 Field layout (Observed from the clock driver functions)

| bits | field | meaning |
|---|---|---|
| [5:0] | PLL multiplier **M** | PLL output frequency = **`4·M + 180` MHz**. Live pen: M = 13 → **232 MHz**. |
| [8:6] | divider **A** | system clock ÷ `2^A`. New value takes effect when latch bit14 is written. |
| [14] | latch A (self-clearing) | write 1 to apply bits[8:6]; hardware clears it; firmware **polls until it reads 0**. |
| [15] | VBAT-ADC mode | set/queried by `adc_vbat_mode_set/_get`; also set during phase 2 of the battery self-check (see `battery-and-power.md`). No clock effect known. |
| [13] (with [14:12] written as `0x7000`) | sleep / clock-stop request + busy | standby handshake, §3.4. Bit13 reads busy while asleep and **self-clears on wake**. |
| [25:22] | divider **B** | system clock ÷ `(B + 1)`. Takes effect when latch bit21 is written. |
| [21] | latch B (self-clearing) | write 1 to apply bits[25:22]; hardware clears; firmware polls until 0. |

Effective CPU/system clock = `PLL / 2^A / (B+1)`.

### 3.2 What the firmware does with it

- **`clk_get_pll_mhz`** (runtime `0x080090dc`): returns `(*0x04000004 & 0x3F) * 4 + 180`.
- **`clk_set_index`** (runtime `0x08009134`): table-driven clock switch. A constant table
  (runtime `0x08007974`) packs `{M, B, A}` per index; indices 0..10 give system clocks
  **116 / 58 / 38.7 / 29 / 19.3 / 14.5 / 9.67 / 7.25 / 3.625 / 1.8125 / 96 MHz** (all with
  M=13 → PLL 232 MHz; index 0 = ÷2 = 116 MHz is the normal running speed). The switch
  writes the divider fields, strobes the latch bits (14 and/or 21), and spins until they
  read back 0. **A runtime change of M is refused** — if the requested table entry's M
  differs from the current one the function deliberately hangs (`hang_forever`); the PLL
  multiplier is set once and never reprogrammed.
- **`clk_boost_max` / `clk_boost_release`** (runtime `0x0800b0f8` / `0x0800b138`): a
  refcounted temporary boost to index 0 around audio decode; same latch mechanics.
- Decision logic compares **table entries**, not the live register, when choosing an
  index — so an "impossible" register value cannot make it hang. The only boot-path
  consumer that branches on the raw value tests `== 0xC0` and both branches converge
  (Observed: booting with the register reading constant 0 takes the 180 MHz path and is
  behaviourally identical).
- **Early-boot spin**: shortly after entry the firmware executes a
  `while (mask & *0x04000004)` wait-for-clear loop (Observed at runtime `0x08000338`).
  If unmapped MMIO reads all-ones this spins forever — the register must exist and its
  latch/busy bits must read 0.

### 3.3 Emulator contract for 0x04000004

- Back it with RAM (writes stored, reads return the stored value), **except** bits
  **13, 14, 21 always read 0** (self-clearing latch/busy semantics). That single rule
  satisfies every latch poll, the early-boot spin, and the standby handshake.
- **Faithful seed: `0x0000004D`** (M=13, A=1, B=0 → PLL 232 MHz, sysclk 116 MHz —
  matches the live pen). **Reads-as-0 also boots** (firmware computes PLL = 180 MHz and
  proceeds identically; Observed working in emulation) but is not faithful.
- No frequency needs to be *enforced*: nothing measures real time against this register.
  Its value only feeds `clk_get_pll_mhz` arithmetic and the table compare.

### 3.4 Standby handshake (sleep request via bit13)

`enter_standby` (runtime `0x0800927c`) performs, in order (Observed):

1. Mask interrupts (IRQ-enable save/zero — see `interrupts-and-timers.md`).
2. Arm wake sources: program `0x3C` (polarity), clear `0x40` (write `0xFFFFFFFF` then
   `0`), set the enable mask in `0x44` (e.g. bit9 = USB-cable detect).
3. Analog prep: `0x58 |= 0x20`, `0x5C |=` bit30.
4. **`*0x04000004 |= 0x7000`** (bits 12–14 = sleep/clock-stop request), then
   **spin until bit13 reads 0**. On real hardware bit13 stays set while the core clock
   is stopped and auto-clears when an armed wake source fires.

**Emulator contract:** when the firmware sets bit13 via the `0x7000` write, hold it
readable as 1 (or simply let the spin run) until a modelled wake event (e.g. a GPIO
wake pin change, USB cable) occurs, then have bit13 read 0 so the spin exits. The
simplest conforming model — bit13 always reads 0 — makes standby a no-op passthrough,
which boots and runs but never actually sleeps (acceptable for a book session; Observed
working).

---

## 4. Audio clock (0x04000008) and the clock-apply strobe (0x04000010 bit8)

The audio-side clock generation lives in this block; the DAC/DMA data path is in
`audio-dac-dma.md`. Field layout of `REG_CLK_AUDIO` (`0x08`), Observed from
`dac_set_rate`/`dac_bringup`:

| bits | meaning |
|---|---|
| [11:4] | ADC-input-path divider |
| [20:13] | DAC rate divider (22050 Hz → divider `0x46`) |
| [21] | divider latch |
| [12] + [23] | codec-type-3 path selects |
| [24] | DAC clock enable |
| [25] | mic/ADC-in enable |
| [29] | pulsed during standby entry |

`REG_ANALOG_PD` (`0x10`) doubles as the **rate-apply strobe**: the DAC rate-set routine
sets **bit8 and spins until hardware clears it** (Observed). bit9 = DAC analog
power-down (1 = down; cleared during DAC bring-up).

**Emulator contract:** RAM-backed; **bit8 of `0x10` must always read 0** or
`dac_set_rate` spins forever. Decoding the achieved sample rate from the `0x08` divider
field is the faithful way to learn the playback rate (see `audio-dac-dma.md`).

---

## 5. Per-module clock gate (0x0400000C, `REG_CLK_GATE`)

Bit = 1 → the module's clock is **gated off**. Known bit assignments (Observed from
`hal_clkgate_set`, runtime `0x080077d0`, and its callers):

| bit | module |
|---|---|
| 2 | NAND / NFC |
| 4 | USB-detect |
| 5 | DAC / audio |
| 6 | analog / ADC |
| 20 | USB controller |

The mask ROM writes the initial gate mask **`0x63`** once at reset. The HAL setter takes
`(module, on)`: `on=1` **clears** the bit (module running), `on=0` sets it; the special
module id 9 writes the whole register (`0x77` / `0x7F`).

**This register was historically misidentified as a watchdog. It is not one, and the
SoC has no watchdog at all** — no kick loop exists anywhere in the mask ROM, boot blob,
or main firmware, and an emulator must not impose one. (Observed.)

**Emulator contract:** plain RAM-backed storage. Gating has no behavioural consequence
worth modelling (the firmware never relies on a gated module failing); model only if you
want warnings for "access to gated module".

---

## 6. Analog power and wake registers (0x3C/0x40/0x44/0x50/0x58/0x5C)

All are write-mostly configuration the firmware read-modify-writes; RAM-backed storage
is sufficient. Points of contact with other models:

- `0x58` bits[26:24] power the **OID sensor** (`oid_sensor_enable` writes
  `&0xF9FFFFFF | 0x1000000`) — a natural hook point if the OID model wants to track
  sensor power (see `oid-sensor.md`); not required (the firmware never verifies it).
- `0x58` bits[30:28] and `0x50`/`0x5C` fields select ADC channels / bias for the battery
  and mic paths (see `battery-and-power.md`).
- `0x3C/0x40/0x44` only matter for the standby handshake (§3.4): whatever wake model you
  implement should consult the `0x44` enable mask (bit9 = USB cable) when deciding to
  clear `0x04000004` bit13. Their precise hardware semantics (W1C vs level in `0x40`,
  polarity encoding in `0x3C`) are **Inferred** — the firmware's usage pattern
  (arm-before-sleep, clear-all writes) is the only evidence.

---

## 7. The chip-ID and "clock self-test" boot gates — what actually runs

At boot (see `memory-map-and-boot.md` §5.4 for the boot-level view) two checks in this
area historically caused emulator aborts. Precisely:

1. **Chip-ID gate** — §2. Pure constant compare. Constant register solves it exactly.

2. **The "clock/crystal calibration self-test" is not a clock test.** Disassembly proved
   the fatal-blink routine formerly attributed to clock calibration is
   **`power_battery_check`** (runtime `0x08038da8`), a three-phase **battery-voltage ADC
   check**, and the HAL leaf polled during it is the **generic GPIO-input read**, not a
   timer probe. (Observed; this corrects older working notes that described it as a
   crystal calibration.) Its only interactions with *this* block are:
   - it switches the system clock between indices (14.5 MHz and 58 MHz phases) via
     `clk_set_index` → the **latch bits must self-clear** (§3.3), and
   - phase 2 sets `0x04000004` **bit15** (VBAT-ADC mode) — plain stored bit, no
     behaviour needed.
   The honest pass is a healthy battery ADC reading, which belongs to the ADC model:
   constant `0x04000070 = 0x00080000` (raw 512 ≥ threshold 341) passes all three phases
   with no hooks. Details in `battery-and-power.md`.

   **Consequence for this block:** there is **no calibration loop against any clock
   register**. Nothing measures elapsed time against `0x04000004`. An emulator needs no
   cycle-accurate clock — only the constant chip-ID, the self-clearing latch bits, and a
   sane battery ADC elsewhere.

---

## 8. Boot-source scratch (0x04000054) and reset / power-off

### 8.1 `REG_BOOT_TAG` (0x54)

Written **only by the mask ROM** as a software record of the chosen boot source
(value in bits[31:24]): `0x05` = SPI, `0x04` = NAND, `0x06` = Massboot (USB mass-storage
recovery), `0x01` = Usbboot, `0x02` = UART; `0xAA` = NAND read error. It is **not** a
hardware-latched strap register — boot mode is decided in ROM software from GPIO straps
(input register `0xBC` bits 13/12/9; see `gpio-buttons-led.md` and
`memory-map-and-boot.md` §4). The loaded firmware (boot blob + PROG) never reads `0x54`.
**Emulator contract:** RAM-backed; nothing depends on it.

### 8.2 Reset and power-off — there is no reset register

Observed, and important to get right:

- **No software-reset / reboot register exists in this block** (or anywhere the firmware
  touches). Hard reset only happens by power cycling.
- **Power-off is a GPIO**: the pen holds itself powered through **GPIO pin 15** ("power
  latch", output latch `0x80` bit15, 1 = stay on). `sys_reset` (runtime `0x080508f8`) is
  actually the **power-off** routine: subsystem teardown, then `gpio_write(15, 0)` —
  dropping the latch cuts the supply — then it polls a dummy GPIO read waiting for the
  supply to die. **An emulator should treat "GPIO15 output latch written 0" as the
  power-off event** (and the latch must be readable back, or the firmware's own shutdown
  paths become invisible hangs). See `gpio-buttons-led.md`.
- `soft_reboot` (runtime `0x08051d60`) is a quiesce-and-halt: zeroes the interrupt
  registers (`0x4C`, `0xCC`, `0x38`, `0x34`), switches the CPU clock, and halts — it
  relies on writes to those registers being honoured (RAM-backed IRQ registers; see
  `interrupts-and-timers.md`).
- At hardware reset the mask ROM's only touch of this sub-block is
  `*0x0400000C = 0x63` (clock-gate init) plus strap-sampling GPIO setup; **the ROM never
  reads `0x00` and performs no PLL writes on the NAND boot path** (Observed from the
  dumped ROM).

---

## 9. Emulator model summary

Minimum conforming model of this sub-block (everything Observed working end-to-end
unless noted):

| register | model | value / behaviour |
|---|---|---|
| `0x00` | constant | read `0x30393031`; ignore writes. **Mandatory.** |
| `0x04` | RAM-backed; bits 13/14/21 read 0 | seed `0x4D` (faithful, 232 MHz PLL) or `0` (works, 180 MHz path). Optional: hold bit13=1 during standby until a wake event. **The self-clearing bits are mandatory** (latch polls + early-boot spin). |
| `0x08` | RAM-backed | audio divider store; decode for sample rate if desired. |
| `0x0C` | RAM-backed | clock gate; no side effects needed. **Never treat as a watchdog.** |
| `0x10` | RAM-backed; **bit8 reads 0** | else `dac_set_rate` spins. |
| `0x3C/0x40/0x44` | RAM-backed | consult `0x44` for the standby wake decision if you model sleep. |
| `0x50/0x58/0x5C/0x74` | RAM-backed | pure config stores; `0x58`[26:24] = OID power if you want to track it. |
| `0x54` | RAM-backed | scratch; unused by the running firmware. |

General rules:

- **Back the whole window with RAM-like registers** (writes stored, reads return
  last-written). Dropping writes silently breaks `soft_reboot`/IRQ-mask semantics
  elsewhere in the window; returning all-ones for unwritten registers hangs the
  wait-for-clear loops (§3.2) and poisons the GPIO-interrupt scan (see
  `gpio-buttons-led.md`).
- **Nothing in this sub-block requires timing behaviour.** The only "active" behaviours
  are: three read-as-zero bits (`0x04` bits 13/14/21), one read-as-zero bit
  (`0x10` bit8), one constant (`0x00`), and — only if you model sleep — clearing
  `0x04` bit13 on wake.

---

## 10. Inferred / unknown configuration the firmware (or ROM) touches

Listed so an implementer isn't surprised by accesses; all are safe as RAM-like stubs:

| range | who touches it | what is known |
|---|---|---|
| `0x05000000` (± ~128 KiB) | mask ROM, once during init | Hardware config block, written only. Purpose unconfirmed — memory/SDRAM-controller configuration (**Inferred**). RAM-like stub suffices; the loaded firmware never touches it. |
| `0x06000000` (± ~128 KiB) | mask ROM, once during init | Second config block, same treatment (**Inferred**). |
| `0x04036000` block | boot console + audio-clock enable | UART / audio-clock block (outside this window). One trap worth knowing: `hal_audio_clk_enable` spins `while (!(*0x04036004 & 0x80000))` — **bit19 of `0x04036004` must read 1** or audio bring-up hangs. See `audio-dac-dma.md`. |
| `0x040A0000` | dormant peripheral bank (gauge/analog?) | Read a handful of times by dormant code paths; any benign constant works (**Inferred**). |
| `0x04000050` `\|= 0x280000` | boot-time `clock_periph_init` | Exact field meaning unknown (analog/pad related, **Inferred**); store-and-return suffices. |
| `0x3C` polarity encoding, `0x40` clear semantics | standby path | Usage pattern Observed; exact hardware semantics **Inferred** (§6). |

Everything else the boot and book-play paths touch in `0x04000000–0x040000FF` is
accounted for here or in the sibling documents listed at the top.
