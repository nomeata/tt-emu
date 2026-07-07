# ZC90B anti-clone auth chip — boot challenge/response

Model of the tiptoi 2N ("MT") pen's anti-clone authentication chip, a **Chomptech ZC90B**. The
firmware bit-bangs a challenge/response handshake against this chip early in boot and **powers the
pen off if it fails**. An emulator that does not answer correctly cannot boot the firmware. This
document specifies the wire protocol, the exact response algorithm (three S-box tables), and how to
model the device.

Evidence tags: **Observed** = read directly from firmware/decompilation or run on hardware.
**Inferred** = deduced (reasoning given). Companions: `gpio-buttons-led.md` (the two auth pins in the
full pin map), `memory-map-and-boot.md` (where this check sits on the boot path).

---

## 1. Role and boot gate

The ZC90B is a small two-wire slave chip whose only job is anti-cloning: it proves the pen contains
genuine hardware. It holds a secret transform; the firmware holds a matching copy of the same secret
(as three lookup tables in its own image). At boot the firmware sends the chip a randomized
challenge and checks that the chip's reply matches what the firmware computes independently. A cloned
pen without a genuine ZC90B cannot produce the reply and is rejected.

**The gate is fatal.** On the boot path an emulator rides (the low-battery / first-load "quiet"
path), the verify routine runs once, and on mismatch the firmware **releases the power-hold latch
(drives GPIO15 = 0) and spins forever** — the pen loses power. There is no retry and no degraded
mode on this path. Therefore the emulator **must** answer the challenge correctly for boot to
proceed. (On the alternative "healthy" battery path the same check runs later but a mismatch only
plays a failure voice prompt; the hard power-off is specific to the quiet path — the one the emulator
takes.) **Observed.**

On success the verify routine clears its fail flag and boot continues normally (posts a standby
continuation event). The check is a single one-shot exchange per boot. **Observed.**

---

## 2. Wire protocol

Two GPIO pins carry the exchange (see `gpio-buttons-led.md`):

- **GPIO10 = CLOCK** — always driven as an output by the firmware; toggled 1→0 once per bit.
- **GPIO5 = DATA** — bidirectional. The firmware drives it while clocking the challenge OUT, then
  switches it to an input to read the response back. (The event pump also idles GPIO5 around every
  dispatched event; that idling is unrelated bus activity, not part of this protocol.)

**Observed** (pin identity Inferred from the ZC90B being a 2-line data chip; the bit-bang on
GPIO10/GPIO5 is Observed).

### Framing

- **Bit order: MSB-first.** For a byte `v`, bit `i` (i = 0..7) is `(v >> (7 - i)) & 1`.
- 8 clocks per byte. **3 challenge bytes out, 3 response bytes back** = 24 clocks each direction.
- **Delimit by GPIO5 *direction*, never by edge count (Observed — demonstrated in emulation).** The
  firmware emits at least one **spurious GPIO10 falling edge before the challenge proper** (before it
  switches GPIO5 to an output). A model that instead counts 24 falling edges from an arbitrary start
  captures the challenge shifted by one bit, computes a wrong response, fails auth — and on the quiet
  boot path the pen powers off, a failure easily misread as a "standby auto-off". The correct
  delimiters: GPIO5 direction switched to **output** = challenge start (discard any stale bits);
  switched back to **input** = challenge complete → compute the response.
- The whole exchange runs with CPU interrupts masked (the firmware masks IRQs before it and unmasks
  after), so a functional emulator model must not depend on interrupts firing mid-exchange.

**Challenge (send) phase** — for each of the 24 challenge bits, the firmware:
1. drives GPIO10 = 1 (clock high),
2. puts the data bit on GPIO5 (while clock is high),
3. short delay,
4. drives GPIO10 = 0 (clock low) — **the falling edge is the latch; the chip samples DATA here**,
5. short delay.

Challenge byte order sent: `c1`, then `c2`, then `c3`.

**Ready handshake** — after the 3 challenge bytes, the firmware switches GPIO5 to an input and polls
it up to **0x30 (48)** times, breaking as soon as it reads non-zero. A genuine chip signals
"response ready" by pulling DATA high. Model: assert GPIO5 = 1 as soon as the 3rd challenge byte is
captured.

**Response (read) phase** — for each of the 24 response bits the firmware pulses GPIO10 (1 then 0)
and samples GPIO5 while the clock is low; a non-zero read sets that bit (MSB-first). The chip must
present each response bit and hold it stable across the firmware's sample. Response byte order read:
`R1`, then `R2`, then `R3`.

Timing (the exact busy-delay lengths) is **not checked** — a functional model that returns the
correct bits under the clock passes; no cycle accuracy is needed. **Observed / Inferred (timing
irrelevance verified by the working emulator model).**

---

## 3. The algorithm

### 3.1 What the firmware sends and expects

The firmware draws a random nonce (three `rng_below(n) + seed` draws) purely to randomize *which*
challenge is sent — it is a live challenge, not a replayable fixed handshake. From the nonce it
computes three **expected response bytes** by indexing its own copies of the three secret tables,
and it clocks out a challenge derived from the same nonce. The chip must transform the challenge back
into those three expected bytes.

Crucially, **the response is fully determined by the 3 challenge bytes alone** — the chip needs no
RNG, no seed, and no state. The nonce is fully recoverable from the challenge (see 3.2), so a genuine
chip (and the emulator) just applies three fixed table lookups. Verified over 1000 random nonces: a
device that answers from the challenge alone passes every time. **Observed + Inferred.**

### 3.2 The response function

The three secret tables are three **256-byte bijective S-boxes** (each a permutation of 0x00..0xFF).
They live at fixed addresses inside the loaded firmware image:

| table  | address       | indexed by            |
|--------|---------------|-----------------------|
| tableA | **0x080b0078** | `c3 & 0xd7`           |
| tableB | **0x080b0178** | `c2 & 0xbe`           |
| tableC | **0x080b0278** | `(c1 ^ B) & 0xff`     |

The three blocks are contiguous (0x080b0078 / 0178 / 0278, 256 bytes each). An emulator does **not**
need to hardcode the 768 bytes — it can read them straight out of the firmware image at these
addresses at startup. **Observed.**

Given the challenge bytes `c1, c2, c3`, the response bytes `R1, R2, R3` are:

```c
B  = tableB[c2 & 0xbe];        // recover expected-B first
C  = tableC[(c1 ^ B) & 0xff];  // un-mix the C index from c1 using B
A  = tableA[c3 & 0xd7];

R1 = C;   R2 = B;   R3 = A;    // firmware checks R1==C, R2==B, R3==A
```

Why it works: the firmware forms `c1 = expB ^ nonceC` and `c2` carries the B-index while `c3` carries
the A-index. The chip recovers B from `c2`, then `c1 ^ B` recovers the C-index, so `C`, `B`, `A` are
reproduced exactly. **Note the ordering:** the challenge is sent `c1,c2,c3` and the response read
`R1,R2,R3`, but the byte carrying the A-index (`c3`) is sent *last* and expected back *last* (`R3`);
B is the middle byte; C is first. The mapping above is the correct one. **Observed + Inferred
(verified).**

The pass condition the firmware checks is exactly `R1 == C && R2 == B && R3 == A`; any mismatch sets
the fail flag and (on the quiet boot path) powers the pen off. **Observed.**

### 3.3 Worked examples

Computed from the actual S-box contents in the shipping image:

| c1   | c2   | c3   | → R1 (=C) | R2 (=B) | R3 (=A) | (indices: B=tableB[c2&0xbe], C=tableC[(c1^B)&0xff], A=tableA[c3&0xd7]) |
|------|------|------|-----------|---------|---------|---|
| 0x00 | 0x00 | 0x00 | 0x16      | 0xa3    | 0xd3    | B=tableB[0x00]=0xa3, C=tableC[0xa3]=0x16, A=tableA[0x00]=0xd3 |
| 0x9d | 0x42 | 0x17 | 0x74      | 0x76    | 0xab    | B=tableB[0x02]=0x76, C=tableC[0xeb]=0x74, A=tableA[0x17]=0xab |
| 0xff | 0xff | 0xff | 0xe5      | 0x70    | 0x70    | B=tableB[0xbe]=0x70, C=tableC[0x8f]=0xe5, A=tableA[0xd7]=0x70 |

(An implementer can regenerate these from the tables at the addresses above to confirm the S-boxes
were located correctly.) **Observed.**

---

## 4. Emulator device model

Model the ZC90B as a small state machine that watches the GPIO clock/data lines. It needs no timing
fidelity — only correct bit order and response order.

**Startup:** read the three S-boxes from the firmware image:
`tableA = image[0x080b0078 .. +256]`, `tableB = 0x080b0178`, `tableC = 0x080b0278`.

**During the challenge phase** GPIO5 is a firmware *output*. Begin capture at the direction-register
write that makes GPIO5 an output (**clearing any previously shifted bits** — this discards the
spurious pre-challenge clock edge, §2 Framing). On each GPIO10 falling edge (1→0), shift the current
GPIO5 output level into a bit buffer, MSB-first. Keep the last 24 bits (`c1,c2,c3`).

**When the challenge is complete** (the firmware switches GPIO5 to an input after 24 bits — detect
this **via the direction-register write** that sets GPIO5 to input; do **not** end the phase by
counting 24 clocked bits, which mis-frames the challenge on the spurious edge, §2 Framing),
compute the response:

```
c1,c2,c3 = last 24 challenge bits, MSB-first, as three bytes
B = tableB[c2 & 0xbe]
C = tableC[(c1 ^ B) & 0xff]
A = tableA[c3 & 0xd7]
response bits = MSB-first bits of C, then B, then A   # R1=C, R2=B, R3=A  (24 bits)
```

Then drive GPIO5 = 1 for the **ready handshake** (so the firmware's ≤48-try poll loop breaks), and
present the 24 response bits on GPIO5 as the firmware clocks them: drive each bit's value into
GPIO5's *input* register (the value the firmware reads back) as the clock pulses, holding it stable
across the firmware's sample. Present the bits in order R1,R2,R3, MSB-first.

**Release:** after all 24 response bits have been read (the firmware switches GPIO5 back to input a
second time on function exit), stop driving GPIO5 and return to idle. Do **not** release on the last
falling edge — the firmware samples the final bit *after* that edge, so an early release drops the
last bit.

Implementation notes from a working model:
- During the challenge, GPIO5 is driven by the firmware *while the clock is high*; sample it on the
  clock's falling edge (the firmware's last write before the edge is the intended bit).
- During the response, present each bit around the clock's rising edge and hold it through the
  firmware's low-phase sample.
- Require 24 *fresh* challenge bits per exchange: clear the bit buffer on the GPIO5
  direction-to-output write (challenge start) and after each response — this both handles a second
  boot-time exchange and immunizes against the spurious pre-challenge clock edge (§2 Framing).
- The firmware masks interrupts across the exchange; the model must work purely from the GPIO
  edges, independent of interrupt delivery.

With this device wired to GPIO10/GPIO5, the firmware's real verify routine runs unmodified, reads
back `R1==C, R2==B, R3==A`, clears its fail flag, and boot proceeds — no hook or patch of firmware
behavior is needed.

---

## 5. Generation dependence

The S-box addresses **0x080b0078 / 0x080b0178 / 0x080b0278** and the challenge index masks
(`0xd7`, `0xbe`) are specific to this firmware image (the 2N "MT" update). A different firmware
generation would place the tables at different addresses and could in principle use different masks,
so those constants would need to be re-located for another image. The *structure* — three bijective
S-boxes, the `B → C-index → C`, `A`, `C/B/A` response ordering, and the GPIO10/GPIO5 bit-bang — is
expected to carry across generations but is only verified for this one. Because the emulator reads
the tables from the image rather than hardcoding them, only the three addresses (and masks) are
generation-specific. **Inferred.**
