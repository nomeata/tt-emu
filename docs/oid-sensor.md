# OID sensor — optical-ID decoder, serial capture, and tap injection

The tiptoi pen reads tiny printed dot patterns ("OID codes") through an optical sensor in
the tip. This document describes the sensor's interface to the SoC — the two-wire serial
link, the frame format, the firmware's capture paths, and how a tapped code becomes an
event — at the level needed to implement OID input in an emulator: inject a tap so the
**unmodified** firmware decodes it through its own code and reacts.

Facts are tagged **Observed** (byte-verified in the firmware's code, or seen in a live RAM
dump from a real pen) or **Inferred** (deduced; reason given). Addresses are runtime
addresses in the firmware's address space (see `memory-map-and-boot.md`).

Related: `gpio-buttons-led.md` (the GPIO register block the link is bit-banged through),
`interrupts-and-timers.md` (the software-timer slot that paces the poll, and the
IRQ-mask push/pop used around the capture).

---

## 1. Physical model — the decoding happens in the sensor, not the SoC

The sensor is a **separate decoder ASIC** (a Sonix OID decoder — hardware teardown reports
an SN9P601FG-301 "OID 1.5" decoder with an SNM9S102 optical module; Inferred from the
hardware side, the firmware never identifies the part). The ASIC contains its own DSP that
performs the entire image capture → dot-pattern descramble → index decode, and outputs the
**finished 18-bit OID index** over a proprietary **bidirectional two-wire serial link**.

Consequences for an emulator (Observed — verified across the whole firmware):

- **The SoC never sees dots or raw pixels.** There is no camera interface, no OID MMIO
  data register, no sensor IRQ, no DMA. The link is a pure GPIO bit-bang.
- **The firmware performs no raw→OID translation.** It only checks framing (type bits,
  valid bit, check byte) and masks (`& 0x3FFFF`, final `& 0xFFFF`). The index field in the
  serial frame **is** the script code the GME games consume. The well-known raw↔code
  scramble between printed dot geometry and script codes lives entirely inside the sensor
  ASIC. So injecting "the decoded OID number as the frame's index field" is exactly what
  the real hardware does — authenticity is a question of the *transport*, not the value.

### Wiring (firmware view — all Observed)

| element | value |
|---|---|
| clock line | **GPIO 2** — host (SoC) output. GPIO output register `0x04000080` bit 2 |
| data line | **GPIO 9** — bidirectional. Input register `0x040000BC` bit 9, output register `0x04000080` bit 9, direction register `0x0400007C` bit 9 (**1 = input, 0 = output**) |
| attention | same GPIO 9: idle **high**; the sensor **pulls it low** when it has a code pending |
| sensor power | `0x04000058` bits [25:24] := `01` at sensor init |
| shift order | **MSB first**, data sampled by the SoC during the **clock-low** phase |

See `gpio-buttons-led.md` for the GPIO register block itself. A sensor-type byte in the
capture state (§4) selects among three sensor drivers; this pen sets **type 0 = the Sonix
two-wire part**. The alternate I²C-configured sensor types (device address `0x94`) are dead
code on this hardware (Observed — the type byte is hard-set to 0 at power-on).

---

## 2. The frame format

The sensor emits one frame per decoded code. Full frame, 32 bits, **MSB first** (Observed —
one format satisfies every check in both firmware decoders):

```
bit 31..30   type       10 = decoded OID (data frame);  11 = status/special
bit 29..27   reserved   000 (the firmware masks these bits out)
bit 26..9    index      the 18-bit OID code (0..262143; in practice ≤ 0xFFFF)
bit  8       valid      must be 1
bit  7..0    check      check byte b: low nibble + high nibble must equal 15,
                        i.e.  ((b + (b >> 4) + 1) & 0xF) == 0
```

- Bits 31..9 form the 23-bit **code word**: for a data frame this is
  `0x400000 | index` (bit 22 = the "10" type's high bit; bit 21 = 0).
- The **check byte is self-contained** — a nibble/complement constraint only. The firmware
  never mixes the index into it, so any byte whose nibbles sum to 15 validates
  (`0xF0`, `0x0F`, `0x69`, …). Whether the real ASIC derives it from the index is
  unobservable to the firmware (Observed for the firmware check; the canonical emulator
  choice is `0xF0`).

**The authentic on-wire word for OID number N:**

```
frame32(N) = ((0x400000 | (N & 0x3FFFF)) << 9) | 0x100 | 0xF0
```

Worked example, N = 42 (`0x2A`):

```
code word  = 0x400000 | 0x2A          = 0x40002A
frame32    = (0x40002A << 9) | 0x100 | 0xF0 = 0x800055F0
on the wire, MSB first:
  1000 0000 0000 0000 0101 0101 1111 0000
  ^^type=10, res=000    ^index=42  ^valid=1, check=0xF0
```

After a successful capture the firmware ends up holding `0x40002A`
(= `0x400000 | 42`) as the raw decoded word, and `42` as the OID the game logic sees (§5).

### Special frame values (Observed)

| value | meaning |
|---|---|
| code word `0x60FFF8` or `0x60FFF1` (type `11`) | **status frame** from the sensor; the firmware answers with the sleep command sequence and stops polling (§4.2). Payload semantics unknown (battery-low vs sleep-ack — Inferred candidates) |
| index `0x3FFFC` (code word `0x43FFFC`) | **filler/invalid** — silently dropped by the decoder. A live pen's idle capture state shows exactly this word: the sensor idles emitting it |
| index `0xFF00..0xFFFE` | **system-code family** (factory codes) — routed differently by the classifier; 18 consecutive such taps power the pen off. Avoid when injecting |

---

## 3. The wire protocol

All Observed (traced instruction-by-instruction), except the absolute time scales.

### 3.1 Bus idle

GPIO9 → input (latched high), GPIO2 → output, **clock low**. This is the rest state. The
sensor signals "code pending" by pulling GPIO9 **low** (attention). Active game states also
read idle-high GPIO9 as "pen not on paper" — the attention line doubles as a touch
indicator.

### 3.2 Sensor → host frame capture (the shift-in)

Runs with **IRQs masked** (see `interrupts-and-timers.md` — the firmware saves/zeroes/
restores the IRQ-enable register around it). The number of bits clocked comes from the
`bit_count` field of the capture state (§4): **23** on the gameplay path, **32** (`0x20`)
on the status polls. Sequence:

```
if GPIO9 != 0: abort                    # attention must be LOW (code pending)
clk = 1
dir9 = input (write 1)                  # release the data line
wait (bounded) for GPIO9 == 1           # sensor ACKs by releasing data HIGH
if still low: abort
dir9 = output, data = 0                 # host ACK: pull data low ...
clk = 0 ; clk = 1                       # ... and pulse the clock low->high
dir9 = input (write 1)                  # release data again
raw = 0
repeat bit_count times:                 # 23 or 32
    clk = 1
    raw <<= 1
    clk = 0
    if GPIO9: raw |= 1                  # sampled in the clock-LOW phase, MSB first
frame_ready = 1
return to bus idle
```

Bit half-period ≈ 5–10 µs → serial clock roughly 50–100 kHz; a full frame ≈ 1 ms with
handshake (Inferred scale from the delay-loop constants). **The firmware never measures
pulse widths or timing** — an emulator may treat the whole protocol as purely edge/
read-driven (Observed: no timing checks exist anywhere in the read path).

### 3.3 Host → sensor commands

The firmware also bit-bangs 8-bit command bytes *to* the sensor (GPIO9 switched to output;
MSB first; data set while the clock is high, so the sensor samples on the **falling** clock
edge; framed by a data-high/clock-fall start condition and a return to bus idle):

| byte(s) | when | meaning (Inferred from context) |
|---|---|---|
| `0x56` | on entering active states, USB, reset | wake / (re)enable reporting; also clears the firmware's done-latch |
| `0xA0, 0xAC, 0xA6` (spaced) | when a status frame arrives | sensor sleep/power-down handshake; sets the done-latch |
| `0xA6` alone | per-tap in the system-code (0xFF00+) counting loop | per-tap acknowledge/rearm |

### 3.4 The trigger pulse

The 32-bit status polls (§4.2) are preceded by a long **GPIO2-high pulse of ~100 ms**
(clock held high, then dropped) before the shift-in loop. The sensor needs no visible
response to it — likely its scan/hold-off window (Inferred). Emulators can ignore it, but
see §6 for a fragility note.

---

## 4. The firmware's two capture paths

Both share one **capture state struct at `0x08008C08`** (Observed, confirmed live):

```c
struct OidCaptureState {      // @ 0x08008C08
  u8  frame_ready;   // +0    set by the shift-in after a full frame
  u8  bit_count;     // +1    bits to clock: 0x17 = 23 (gameplay) or 0x20 = 32 (polls)
  u8  decode_valid;  // +2    set after a data frame passes validation
  u8  pad[4];
  u8  done_latch;    // +7    1 = sensor was sent to sleep; gates the 32-bit polls ONLY
  u32 timer_handle;  // +8    poll soft-timer slot id (0xFF = none)
  u32 raw_word;      // +0xC  last shifted frame, MSB-first (i.e. @ 0x08008C14)
  u8  sensor_type;   // +0x10 0 = Sonix two-wire (this pen)
};
```

and one OID working buffer (`akoid_buf`, a ~0xDF0-byte heap block reached via the game
context; fields the emulator cares about): `+0` = pen-down/event-pending flag, `+4` = the
**current OID** all game handlers read, `+8` = the decoded code word `0x400000|index`,
`+0x14` = capture enable (init 1), `+0x18`/`+0x1A` = u16 first/last OID of the mounted GME.

### 4.1 Gameplay path — the 23-bit periodic capture (this one posts taps)

A software-timer slot (see `interrupts-and-timers.md` §5; **reload 2 = one poll every
40 ms**, confirmed on a live pen) runs the OID poll callback (`0x080057CC`):

1. Gate: capture enable (`akoid_buf+0x14`) set **and GPIO9 == 0** (attention asserted).
   Note: this path **ignores the done-latch**.
2. `bit_count = 23`; run the shift-in (§3.2) → `raw` = frame bits 31..9 = the code word.
3. Validate: require `(raw & 0x600000) == 0x400000` (type bits `10`); drop the filler
   `(raw & 0x43FFFF) == 0x43FFFC`; store the code word `0x400000|index` at `akoid_buf+8`;
   set `decode_valid`.
4. On valid: set `akoid_buf[0] = 1` (pen-down / event pending) and **post event `0x1060`**
   into the firmware's event ring, argument = the code word `0x400000|index`.

### 4.2 Status watch — the 32-bit polls (never posts events)

From standby/splash entry code (not from gameplay), gated on the **done-latch** being
clear: the ~100 ms trigger pulse (§3.4), then with IRQs off a loop of up to ~20 frames:

1. `bit_count = 32`; shift-in → full 32-bit frame.
2. Check **valid bit** (`raw & 0x100`) and the **check byte**
   (`((b + (b >> 4) + 1) & 0xF) == 0` with `b = raw & 0xFF`).
3. On pass, store `raw >> 9` (the **unmasked** code word, e.g. `0x400000|index`) at
   `akoid_buf+4`.
4. If the code word is a status word (`0x60FFF8` / `0x60FFF1`): send the sleep command
   sequence `0xA0, 0xAC, 0xA6` and set the done-latch — all further 32-bit polls are
   skipped until a `0x56` wake clears it.

Both widths read **the same on-wire frame**: 23 clocks yield the code word, 32 clocks the
full frame. A single authentic frame therefore serves both decoders with no mode tracking.

---

## 5. From raw frame to a tap event

The `0x1060` event flows through the statechart (Observed):

1. A global classifier transition action runs first on every `0x1060`. For a normal code
   (type `10`, index not in `0xFF00..0xFFFE`) it stores
   **`akoid_buf+4 = code_word & 0xFFFF`** — the bare OID number. System-family codes set
   `akoid_buf[0] = 2` instead and feed a consecutive-tap counter.
2. The active state's handler then reacts to `event == 0x1060 && akoid_buf[0] != 0`:
   it reads the OID from `akoid_buf+4`, range-checks it against the mounted GME's
   first/last OID (`akoid_buf+0x18/+0x1A`), and drives the script interpreter
   (`gme_oid_dispatch` → play scripts). At standby, a product/cover code instead triggers
   book mount.

**What the emulator must guarantee after a tap:** the firmware's own capture produced
`akoid_buf+8 = 0x400000|N`, event `0x1060` was posted with that word, and the classifier
left `akoid_buf+4 = N` with `akoid_buf[0] = 1`. All of this happens by itself if the
authentic frame is presented at the GPIO boundary — nothing needs to be written into
firmware RAM.

Note the two paths store different forms at `+4`: the 32-bit poll leaves the unmasked
`0x400000|N` there provisionally; the classifier overwrites it with the bare `N` per tap.
Both are correct hardware states at their respective moments.

---

## 6. Emulator model — injecting a tap

Model the sensor as a small state machine attached to three GPIO bits (clock = output
register bit 2, data = input register bit 9, direction register bit 9) and let the
firmware's own shift-in and decoders run unmodified. This is a proven working model
(verified end-to-end: cold boot → GPIO tap → firmware decodes → event `0x1060` → the game
plays the right script).

**Frame to serve** (one frame, both capture widths):

```
frame32(N) = ((0x400000 | (N & 0x3FFFF)) << 9) | 0x100 | 0xF0     # N = OID number
```

Do **not** shortcut the frame: serving the bare code word as a 32-bit frame puts the OID's
own low byte where the check byte lives, and for unlucky N (low nibbles summing to 15,
bit 8 set) the status poll "validates" a **wrong** OID (`raw >> 9` ≠ N). Only the full
authentic frame is correct for every N on both paths.

**State machine** (phases advanced purely by observing the firmware's own GPIO writes):

1. **IDLE** — no tap pending: GPIO9 input bit reads **1**. (Game states read this as
   pen-up; the poll's capture gate stays closed, so no spurious frames.)
2. **PRE** — tap armed: GPIO9 reads **0** (attention). The 40 ms poll's gate opens; the
   shift-in's own attention check passes.
3. **ACK** — when the firmware raises GPIO2 (clock high): GPIO9 now reads **1** (the
   sensor's ready-ACK the shift-in busy-waits for). Then tolerate the host-ACK: the
   firmware flips GPIO9's direction to output, drives it low, pulses the clock.
4. **BITS** — when the firmware flips GPIO9's direction back to **input** (after having
   had it as output in ACK): start serving data. On the k-th GPIO9 read (k = 0, 1, …)
   return bit `31 − k` of `frame32(N)`. Serve for however many bits the firmware clocks —
   either hard-code "until the firmware returns to bus idle", or read the firmware's own
   `bit_count` byte at `0x08008C09` (23 or 32). A 23-bit capture reads bits 31..9 = the
   code word; a 32-bit poll reads the whole frame — same data either way.
5. **DONE** — after the last bit: back to IDLE (GPIO9 = 1) until the next tap. This makes
   the pen look lifted immediately after the tap.

Serving one bit **per GPIO9 read** (instead of per clock edge) is safe because the
firmware reads the pin exactly once per bit (Observed); tracking clock edges works too.

**Timing / when to tap:**

- No real-time constraints — the firmware busy-waits with fixed delays and never checks
  pulse widths. Respond per observed write/read, at emulated-instruction speed.
- Arm taps **while the firmware is idle** (event pump drained, standby or book state
  reached). The 40 ms poll picks the frame up on its next firing.
- A perfectly decoded tap can still be **dropped by the statechart**: at standby the
  book-opening tap is gated on state bytes, one of which (`game_ctx+0x1d`) the firmware
  itself invalidates ~one heartbeat (~100 ms) after standby entry. Getting the frame
  decoded is the *transport* half only — for the tap→book-mount sequence, the required
  state, the three-tap order (product, product, content) and inter-tap pacing, see
  `nand-image-layout.md` §7.3–§7.3.2.
- Avoid arming during a 32-bit status-poll window: the ~100 ms GPIO2 trigger pulse (§3.4)
  hitting an armed PRE frame advances it to ACK, where attention already reads 1 — the
  frame strands unconsumed. Arming between polls (i.e. when the firmware is at the pump
  idle) avoids this.

**Repeat / anti-repeat behaviour:**

- The real sensor **re-reports ~every 40 ms while the pen is held on a code** (the poll
  cadence exists for this). One-frame-then-idle injection means "tap and immediately
  lift"; to emulate press-and-hold semantics, re-arm the same frame each time the previous
  one completes for the hold duration.
- Active handlers poll GPIO9 directly and treat idle-high as pen-lift — keep GPIO9 = 1
  strictly whenever no frame is pending, or the firmware will see a phantom touch.
- Values to avoid injecting: `0x3FFFC` (filler — silently dropped), `0xFF00..0xFFFE`
  (system/factory family — special routing; 18 consecutive ones power the pen off).

**Commands (optional fidelity):** decode the firmware's command writes (§3.3: GPIO9 as
output, bit valid on the falling clock edge): on `0xA0, 0xAC, 0xA6` stop asserting
attention (sensor asleep); on `0x56` resume. A minimal emulator can ignore commands
entirely — the gameplay (23-bit) path ignores the done-latch, so taps keep working — but
then the lone-`0xA6` acknowledge that the system-code loop clocks out will pass through an
armed frame's state machine; make sure command traffic cannot desync a pending frame
(simplest: cancel/re-arm the pending frame if a command is observed).

**Status frames (optional):** to exercise the standby sleep handshake, answer a 32-bit
poll with `(0x60FFF8 << 9) | 0x100 | 0xF0`. Never needed for tap injection.

---

## 7. Summary of magic numbers

| item | value |
|---|---|
| clock pin / data pin | GPIO2 (out reg bit 2) / GPIO9 (in reg bit 9, dir reg bit 9, 1=input) |
| data frame, 32-bit | `((0x400000 \| (N & 0x3FFFF)) << 9) \| 0x100 \| check` |
| check byte rule | `((b + (b >> 4) + 1) & 0xF) == 0` (nibbles sum to 15); use `0xF0` |
| code word (23-bit capture) | `0x400000 \| N` — also the value stored at `akoid_buf+8` and posted with event `0x1060` |
| final OID for game logic | `akoid_buf+4 = code_word & 0xFFFF` |
| type check | `(code_word & 0x600000) == 0x400000` |
| filler (dropped) | code word `0x43FFFC` |
| status words (→ sleep) | code words `0x60FFF8`, `0x60FFF1` |
| system-code family | `0xFF00..0xFFFE` |
| capture state / raw word | struct @ `0x08008C08`; raw shifted word @ `0x08008C14`; bit_count @ `0x08008C09` |
| tap event id | `0x1060` (argument = `0x400000 \| N`) |
| poll cadence | 40 ms (soft-timer slot, reload 2 × 20 ms tick) |
| sensor wake / sleep cmds | `0x56` / `0xA0, 0xAC, 0xA6` |
