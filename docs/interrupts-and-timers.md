# tiptoi 2N ("MT") pen — interrupt controller and timers

Implementation reference for IRQ delivery and the periodic system timer. Everything an
emulator needs to raise interrupt lines, vector the CPU into the firmware's own handlers,
and drive time forward is in this file. Peripheral-side event generation (when the audio
DMA finishes a chunk, what the USB controller does) lives in the peripherals' own
documents (`audio-dac-dma.md`, `usb-musb-device.md`); CPU/boot basics are in
`memory-map-and-boot.md`.

Facts are marked **Observed** (byte-verified disassembly of the shipped firmware, a
live-pen RAM dump, or demonstrated end-to-end in emulation) or **Inferred** (deduction).
Unmarked statements are Observed. All addresses are runtime physical addresses,
little-endian; registers are 32-bit.

---

## 1. Architecture overview

There is one small interrupt controller in the SoC core register block (`0x04000000`),
plus a second-level status register for the timer/GPIO sub-block. The chain, end to end:

```
peripheral event
  └─ top-level PENDING 0x040000cc (per-line bits)
       & top-level ENABLE 0x04000034 (same bit layout)
         └─ ARM IRQ (if CPSR.I == 0)
              └─ vector 0x08000018 (loaded image's vector table)
                   └─ entry stub 0x08000110: r = PENDING & ENABLE, dispatch per bit
                        ├─ bit0  → audio/L2-DMA-done ISR   0x080039d4
                        ├─ bit6  → USB (MUSB) ISR          0x0803db24
                        └─ bit10 → timer/GPIO aggregate    0x08005534
                             └─ 2nd-level status 0x0400004c
                                  ├─ bit17 → timer dispatch 0x08005c8c
                                  │            (ack 0x04000018 bit28, tick++,
                                  │             run 6 software-timer slots)
                                  └─ bit20 → GPIO-interrupt pin scan
```

Only three top-level lines are ever used by the firmware. Everything periodic in the
system (system tick, OID sensor poll, key scan, GME script timers, heartbeat, auto-off)
hangs off **one hardware timer** ("timer1") through a 6-slot software-timer table.

---

## 2. Interrupt-controller registers

| addr | name | r/w | function |
|---|---|---|---|
| `0x04000034` | `INT_ENABLE` | R/W | Top-level interrupt enable, one bit per line (layout below). Write 0 = mask all. |
| `0x040000cc` | `INT_PENDING` | R | Top-level pending, same bit layout as `INT_ENABLE`. De-asserted by acking the *source* (see per-line ACK below), not by writing this register on the hot path. |
| `0x0400004c` | `TIMER_STAT_CTRL` | R/W | Mixed config/status for the line-10 sub-block. **Low bits are firmware-written config**: bit1 = software-timer tick enable, bit4 = GPIO-interrupt scan enable. **High bits are hardware status**: bit17 (`0x20000`) = timer1 fired, bit20 (`0x100000`) = GPIO-interrupt cause. |
| `0x04000018` | `TIMER1_CTRL` | R/W | Timer1 control **and the timer ACK** (§4). |
| `0x04000038` | — | W | Only ever written 0, together with `0x34/0x4c/0xcc`, in reset paths. Possibly a second enable bank. **Inferred.** |

### 2.1 Line assignment

| bit in `0x34`/`0xcc` | line | source | second-level status | ACK (what de-asserts pending) |
|---|---|---|---|---|
| bit0 (`0x001`) | 0 | **Audio/L2-DMA transfer done** (DAC playback chunk finished, or mic-capture buffer full — the ISR distinguishes by a DMA-tag byte; see `audio-dac-dma.md`) | DMA status word `0x0401001c` | ISR clears the DMA kick/GO bit: `0x04010000 &= ~0x10000` |
| bit6 (`0x040`) | 6 | **USB device controller** (MUSB, block `0x04070000`) | MUSB's own interrupt registers | reading the MUSB status registers |
| bit10 (`0x400`) | 10 | **Timer/GPIO block** (timer1 tick *and* GPIO pin-change scan share this line) | `0x0400004c` bit17 / bit20 | timer: write `0x04000018 \|= 0x10000000` (bit28) |
| all others | — | never referenced by the firmware | — | — |

### 2.2 Read/write/ACK semantics

- The IRQ entry stub computes `*0x040000cc & *0x04000034` once per exception and services
  **every** set bit (in the order bit10, bit0, bit6) before returning. Pending must
  therefore behave as a **level**: a line stays asserted until its source-specific ACK,
  and drops immediately on that ACK (a stuck level = IRQ storm; the stub would re-enter
  forever).
- `INT_ENABLE` (`0x34`) must **persist writes and be honored for delivery**. The
  firmware's critical-section primitives `irq_mask_push` / `irq_mask_pop` (runtime
  `0x0800339c` / `0x080033e4`, 4-deep save stack at `0x08008c94`) save/zero/restore this
  register around timing-sensitive code (OID bit-shift, the anticlone bit-bang exchange).
  Delivering an IRQ while `0x34` is zeroed corrupts those exchanges. **Observed** (both
  the primitive and the corruption).
- Per-line enable writers found in the firmware: `hal_audio_irq_gate` (runtime
  `0x08003430`) sets/clears **bit0**; `usb_detect_disable` (`0x080413fc`) clears bit0 and
  bit6 on the way out of USB mode. **Who sets bit0/bit6/bit10 initially has not been
  located** — presumably the resident boot code's HAL init on real hardware
  (**Inferred**; see §7 for the pragmatic consequence).
- Reset/teardown: the firmware's soft-reboot path writes **0** to `0x4c`, `0xcc`, `0x38`
  and `0x34` (in that order) — i.e. it treats all four as writable and expects 0 to mean
  "everything off/clear".

### 2.3 Pitfalls — phantom lines (defaults that matter)

These are proven failure modes, not hypotheticals:

1. **Never assert `0xcc` bit6 without a real USB event.** Two independent consumers make
   a permanently-set bit6 harmful: (a) the entry stub runs the USB ISR on *every*
   delivered IRQ (it survives only via a data-dependent early-out that is not guaranteed
   to hold), and (b) `usb_wait_host_sof` (`0x08041ce4`) *polls* `0x040000cc & 0x40` and a
   stuck bit6 fakes "USB host alive" on its first iteration. **Observed.**
2. **`INT_PENDING` must read 0 when nothing is pending** — a free-running or garbage
   value (e.g. an incrementing counter) causes spurious USB detection and IRQ storms.
   **Observed.**
3. **The GPIO-interrupt enable/polarity banks `0x040000e0/e4/f0/f4` must read 0 if never
   written** (never 0xFFFFFFFF): the line-10 aggregate handler's pin scan would otherwise
   treat every GPIO pin as an enabled active-low interrupt. **Observed** (see §6 and
   `gpio-buttons-led.md`).
4. **The DMA status word `0x0401001c` must read 0** when the audio ISR runs: the ISR's
   first act is a spurious-IRQ check on it; nonzero makes the ISR return *without*
   clearing the DMA kick bit → line 0 never de-asserts → storm. **Observed.**

---

## 3. CPU-side delivery

The CPU is an ARM926EJ-S (ARMv5TEJ); standard ARM exception semantics apply and the
firmware relies on them (banked SP/LR/SPSR per mode). See `memory-map-and-boot.md` §1.

**When to deliver.** Take an IRQ exception when *all* of:
`(*0x040000cc & *0x04000034) != 0`, **CPSR.I == 0**, and the CPU is **not already in IRQ
mode** (mode != 0x12). The firmware's handler is not written to nest.

**Exception entry** (what the emulator must do, exactly the architectural sequence):

```
SPSR_irq := CPSR
CPSR     := (CPSR & ~0x3F) | 0x12 | 0x80     # mode = IRQ (0x12), I = 1, T = 0 (ARM state)
LR_irq   := interrupted_PC + 4
SP_irq   := IRQ-mode banked stack (0x083F0000 on first delivery — see below)
PC       := 0x08000018
```

- **Vector = `0x08000018`** — the IRQ slot of the loaded image's vector table at
  `0x08000000` (it holds `b 0x08000110`). On real silicon the mask ROM owns address 0 and
  its vectors are `ldr pc, …` trampolines that forward every exception to
  `0x08000000 + offset`, so jumping straight to `0x08000018` is equivalent. **Observed**
  (both the ROM trampolines and the resident vector table).
- **IRQ stack:** on real hardware the boot code's reset handler sets the per-mode banked
  stacks; an emulator that enters at the firmware's main entry (see
  `memory-map-and-boot.md` §5.2) skips that and must set `SP_irq` itself —
  **`0x083F0000`** (below the main SVC stack top `0x08400000`) is the proven choice; the
  exact hardware value is unverified (**Inferred**). The handler's push/pop is balanced,
  so any stable top works **as long as it lies inside `[0x08000000, 0x08400000)`**
  (Observed): the firmware's `Utl_UStr*` helpers silently reject pointers outside
  that range, so an out-of-range stack breaks discovery's on-stack path building —
  see the stack-placement rule in `memory-map-and-boot.md` §5.2.

**Handler prologue/epilogue** (what the firmware's own code does — the contract the entry
state above satisfies): the entry stub `0x08000110` does `push {r0-r12, lr}`, reads
`PENDING & ENABLE` (pointer literals to `0x040000cc`/`0x04000034` sit in its pool),
`bic`s each serviced bit from its working copy and `bl`s the per-line handler, then
`pop`s and returns with **`subs pc, lr, #4`** — which in IRQ mode restores
`CPSR := SPSR_irq` and resumes at the interrupted instruction. The emulator's CPU core
must implement that exception-return (CPSR restore from SPSR) correctly; nothing else is
read from the entry state.

**CPSR.I management by the firmware.** The firmware masks interrupts two ways: globally
via CPSR.I (enabled once during init, restored by every exception return) and, far more
often, at the controller via `0x34` (`irq_mask_push/pop`, §2.2). Boot the CPU with IRQs
enabled (I = 0) per `memory-map-and-boot.md` §5.2; deliver nothing until the firmware has
something enabled and a line actually pends.

---

## 4. Timer1 — the hardware tick

One down-counting reload timer generates the entire system's time base.

**Register `0x04000018` (`TIMER1_CTRL`):**

| bits | function |
|---|---|
| [25:0] | reload value, in timer-clock cycles |
| 26 | load (latch the reload) |
| 27 | enable (timer runs while set) |
| 28 | **IRQ ACK** — the ISR writes `\|= 0x10000000` once per tick; this clears the latched timer interrupt (drops `0xcc` bit10 and `0x4c` bit17) |

**The values used:** the firmware arms it exactly once, lazily, on the first software-timer
registration: `*0x04000018 = 240000 | 0x0C000000` (reload 240000 + load + enable).
**Reload 240000 = 20 ms** at a 12 MHz timer clock — the timer clock being 12 MHz is
**Inferred** (from the reload value and the fact that all consumers divide milliseconds
by 20); everything else in this section is Observed. The pen therefore runs a **50 Hz
system tick, one IRQ per 20 ms**.

**Per-tick sequence** (all firmware code, byte-verified): line 10 pends → entry stub →
aggregate handler `0x08005534` re-reads `0xcc` bit10, reads `0x4c`; bit17 set → tail-calls
**`timer_dispatch` `0x08005c8c`**, which:

1. increments the **system tick** `*0x08008d24` (+1 per IRQ, unit = 20 ms — the
   firmware-wide time source: `sys_get_tick` `0x08030c88` has ~90 callers, and GME
   opcode 0xFF00 uses it as entropy);
2. **acks**: `*0x04000018 |= 0x10000000` — *before* running callbacks;
3. walks the 6 software-timer slots (§5) and `blx`es every expired one.

A second timer register may exist (the block is at `0x18` with possible siblings); the
firmware uses only this one. **Inferred.**

---

## 5. The software-timer layer (what the timer IRQ drives)

The resident HAL multiplexes timer1 into **six software-timer slots** — a fixed table at
**`0x0800895c`**, stride 0xC bytes:

| offset | size | field |
|---|---|---|
| +0 | u8 | status: low 7 bits = state (1 = active), bit7 = flag |
| +1 | u8 | enable/argument byte (0 = enabled) |
| +2 | u16 | **reload** — period in 20 ms ticks |
| +4 | u16 | count — incremented each tick; callback fires when count ≥ reload, then count resets |
| +8 | ptr | callback, invoked `blx cb` with `r0 = slot_index` (r1 = reload) |

**API** (resident HAL, called by the main firmware throughout its life):

- `hal_timer_register(arg, period_ms, periodic, callback)` — runtime **`0x0800780c`**.
  Converts `period_ms / 20` into the slot reload (unsigned divide by 0x14), stores the
  callback, returns the **slot index**, or **0xFF when all 6 slots are taken** (the
  caller gets no timer, silently). First call ever also arms timer1 (§4).
- `hal_timer_unregister(slot)` — runtime **`0x08005be8`**. Clears the slot, returns 0xFF.

**Known slot users** (a live pen at standby shows 4 armed slots — Observed from a RAM
dump):

| callback | period | role |
|---|---|---|
| `oid_timer_cb` `0x080057cc` | reload 2 = **40 ms** | OID sensor poll (clocks a frame out of the sensor; see `oid-sensor.md`) |
| key-scan `0x080058f0` | — | input scan |
| poster `0x08003994` | varies | generic event poster: on fire, posts **event `0x30 {slot}`** into the firmware's event ring for GME-script timers (GME opcode 0xFE00 registers this callback with `period = m×100 ms`), while the firing of the *current poll slot* yields the **heartbeat event `0x1046`** |
| (dynamic) | — | settle/one-shot timers registered and freed at runtime |

Consequences worth knowing when validating an emulator: the heartbeat drives the standby
auto-off counter (> 300 heartbeats idle → power-off) and the "Random" GME opcode; the
system tick gates OID tap classification settle times. If the tick doesn't advance, the
statechart freezes in boot-settle; if it advances too fast, auto-off fires early.

Everything in this layer is ordinary firmware code operating on ordinary RAM — the
emulator implements **none** of it. It exists here so you can verify the chain: after
boot, `*0x08008d24` must increment once per delivered timer IRQ, and the slot table must
show live registrations.

---

## 6. GPIO interrupts — polled, via line 10

There are **no dedicated top-level GPIO interrupt lines**. While `0x4c` bit4 (GPIO-scan
enable) is set, a pin-change condition raises **line 10 with `0x4c` bit20** (instead of
bit17), and the aggregate handler scans: for every pin whose bit is set in the
GPIO-interrupt enable banks (`0x040000e0` pins 0–31 / `0xe4` pins 32+), it compares the
input level (`0x040000bc`/`0xc0`) against the polarity banks (`0x040000f0`/`0xf4`,
**bit set = trigger when the pin reads LOW**) and dispatches a per-pin callback on
mismatch. Armed via `hal_gpio_int_arm` (`0x08003c38`, writes the inverted current level
as the polarity). Used for wake sources (USB-cable change on pin 8). Details of the GPIO
banks: `gpio-buttons-led.md`.

Emulator model: while `0x4c` bit4 is set, evaluate `enabled & (level != polarity)`; if
any pin matches, assert `0xcc` bit10 + `0x4c` bit20. A book-session emulator can defer
this entirely (nothing on the happy path needs it) — but the bank defaults of §2.3
item 3 must hold regardless.

---

## 7. Emulator model — what to implement

### 7.1 Registers

| register | model |
|---|---|
| `0x04000034` | RAM-backed; **honor it at delivery time** (`pending & enable`). Because the code that initially sets bit0/bit10 has not been located (§2.2), the **proven-working default is `0xFFFFFFFF`** at boot, overwritten by every firmware write. That default is safe *only* because the pending model below never asserts a line without a real event. (Starting at 0 is more faithful to hardware but is not known to boot — gap, §8.) |
| `0x040000cc` | Read-only status, computed: bit10 while the timer is latched, bit0 while an audio-DMA completion is latched, bit6 only on a modelled USB event. **0 when idle** (§2.3). |
| `0x0400004c` | Low bits RAM-backed (firmware config writes); reads return `config_low_bits \| (0x20000 if timer latched) \| (0x100000 if GPIO cause latched)`. |
| `0x04000018` | RAM-backed for bits [27:0]. **On any write with bit28 set: clear the timer latch** (drops `0xcc` bit10 and `0x4c` bit17). Run the periodic timer while bit27 is set, period = `reload / 12 MHz` (= 20 ms for the firmware's 240000). |
| `0x04000038` | RAM-backed, no behaviour. |
| `0x040000e0/e4/f0/f4` | RAM-backed, **read 0 until written**. |

### 7.2 Raising the lines

- **Timer (line 10):** while `0x18` bit27 is set, every timer period: latch → assert
  `0xcc` bit10 + `0x4c` bit17. Stay asserted (level) until the bit28 ACK write. Do not
  auto-clear on delivery — the firmware's dispatch re-reads both registers and acks
  explicitly.
- **Audio DMA done (line 0):** when the audio DMA channel's kick/GO bit (`0x04010000`
  bit16) has been set with the DAC as destination and the modelled transfer completes,
  assert `0xcc` bit0. The firmware's own ISR (`0x080039d4`) clears `0x04010000` bit16 —
  treat that write as the line-0 ACK. Keep `0x0401001c` reading 0 (§2.3 item 4). Full DMA
  model: `audio-dac-dma.md`. Let the ISR do all the work (it chains the next ring chunk
  from interrupt context itself); the emulator only completes transfers and raises the
  line.
- **USB (line 6):** only if you model USB events at all (`usb-musb-device.md`);
  otherwise never.
- **GPIO scan (line 10 + `0x4c` bit20):** per §6, optional.

### 7.3 Delivery

Per §3: gate on `(pending & enable) != 0`, CPSR.I == 0, mode != IRQ; then the
architectural entry sequence to PC = `0x08000018` with `SP_irq = 0x083F0000`; the
firmware returns via `subs pc, lr, #4`. Between the latch and the ACK the level stays up;
because delivery is gated on "not already in IRQ mode", a still-pending line simply
re-delivers after the return — which is exactly how the firmware expects a level-triggered
controller to behave.

### 7.4 Pacing (making time advance realistically)

An emulator is rarely cycle-accurate; what matters is the *ratios*:

- **Timer cadence defines "20 ms".** Whatever unit you pace by (host time, emulated
  instructions), one timer IRQ = one 20 ms tick to the firmware. ~20,000 emulated
  instructions per tick is a proven-working cadence for an interpreter-class core
  (boot-to-standby ≈ 300–850 ticks); the exact number only shifts how much CPU work fits
  in a tick.
- **Audio completions must be derived from the DAC rate, not a constant.** A DMA chunk of
  `N` bytes of 16-bit stereo at sample rate `R` really takes `N / (4·R)` seconds — e.g.
  the standard 0x400-byte chunk at 22050 Hz = **11.6 ms ≈ 0.58 timer ticks** (~86 audio
  IRQs/s). Schedule the line-0 assert that long after the kick, in the same time unit as
  the timer. Completing instantly makes the ISR-chained refill drain the ring faster than
  the decoder fills it (underrun/stall); a fixed instruction constant that isn't derived
  from the rate skews audio-vs-time by the corresponding factor. **Observed** (both
  failure modes).
- **Never advance the system tick (`0x08008d24`) yourself.** Its only legitimate writer
  is the firmware's own `timer_dispatch`, +1 per delivered timer IRQ. Any emulator-side
  "compensation" writer runs firmware time at the wrong rate (auto-off, GME timers,
  settle gates all consume it). **Observed** failure mode.
- Boot phase: the timer only starts once the firmware arms it (first
  `hal_timer_register`), so no IRQs are deliverable before that — nothing to compensate.

### 7.5 Health checks

- After boot: `*0x04000018` low bits = `240000 | load/enable`; one bit28 write per tick.
- `*0x08008d24` increments by 1 per delivered timer IRQ; slot table `0x0800895c` shows
  ~4 live slots (OID poll with reload 2 among them).
- No line stays pending across more than one delivery without its ACK (else: storm).

---

## 8. Open questions / gaps

- **`INT_ENABLE` reset value and initial per-line enabling** — unknown who sets bit10
  (and bit0) first on real hardware; the all-ones boot default is a working model, not a
  measurement (§7.1).
- **Timer input clock** — 12 MHz is inferred from reload 240000 = 20 ms, not measured.
- **IRQ-mode stack location on real hardware** — set by boot code not exercised in the
  from-entry boot; `0x083F0000` is an emulator choice that works (it must lie below
  `0x08400000` — see `memory-map-and-boot.md` §5.2).
- **`0x04000038`** semantics (write-0-only companion register).
- Whether the entry stub re-reads `PENDING & ENABLE` in a loop before returning or
  services one snapshot per exception — both behaviours are compatible with a
  level-triggered pending model plus re-delivery, which is what the emulator should
  implement.
