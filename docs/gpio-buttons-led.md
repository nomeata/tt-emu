# GPIO block, buttons, and LED (tiptoi 2N "MT" — ZC3202N / AK1050-class SoC)

This document is the **authoritative physical pin map** for the pen. It covers the GPIO
register block, every pin the firmware touches, the three buttons and their key-scan/event
pipeline, the power-hold latch, the test-mode entry chord, and the (non-)LED — everything an
emulator needs to present correct pin states so the unmodified firmware boots, plays books,
and shuts down observably.

Sub-protocol details for pins that carry serial buses live in their own docs and are only
cross-referenced here: `oid-sensor.md` (OID clock/data), `audio-dac-dma.md` (amp/codec
pins in the audio path), `usb-musb-device.md` (USB detect and the USB controller),
`zc90b-auth.md` (anti-clone challenge/response), `interrupts-and-timers.md` (the 20 ms
system tick that paces key scanning, and IRQ delivery), `battery-and-power.md` (battery
ADC — note: the battery level is an ADC reading, **not** a GPIO).

Facts are tagged **Observed** (read from firmware disassembly or the live-pen RAM dump) or
**Inferred** (deduced; reason given). Addresses are runtime addresses.

---

## 1. GPIO registers

All GPIO registers live in the SoC core-control window at `0x04000000` (see
`system-control-and-clock.md` for the rest of that window). Two banks: bank 0 = pins 0–31,
bank 1 = pins 32+. **Only bank-0 pins are ever used**; the firmware never writes any bank-1
output. (Observed)

| address | name | r/w | semantics |
|---|---|---|---|
| `0x0400007C` | GPIO_DIR (bank0) | R/W | Direction, one bit per pin. **bit = 1 → input**, bit = 0 → output. (The firmware's HAL API takes the inverted convention — arg 1 = output — but the hardware bit is as stated.) Bank1 = `0x04000084`. |
| `0x04000080` | GPIO_OUT (bank0) | R/W | **Output latch.** Writing drives output pins; **reading must return the last value written** (see §1.1). Bank1 = `0x04000088`. Reset value 0. |
| `0x0400009C` | GPIO_PULL (bank0) | R/W | Pull enable, 1 = pull on. Bank1 = `0x040000A0`. |
| `0x040000BC` | GPIO_IN (bank0) | R | **Input level, one bit per pin.** This is the register the emulator's pin model feeds (§7). Also carries the boot-mode straps sampled by the mask ROM (bits 13/12/9, see `memory-map-and-boot.md`). Bank1 = `0x040000C0`. |
| `0x040000E0` | GPIO_INT_EN (bank0) | R/W | Per-pin GPIO-interrupt enable. Bank1 = `0x040000E4`. **Must read 0 when unwritten** (§6). |
| `0x040000F0` | GPIO_INT_POL (bank0) | R/W | Per-pin interrupt polarity: **bit set = trigger when the pin reads LOW**. Bank1 = `0x040000F4`. **Must read 0 when unwritten** (§6). |

All firmware GPIO access funnels through one HAL dispatcher (`hal_gpio_op`, with leaf
helpers `hal_gpio_read` `0x08007740`, `hal_gpio_write` `0x08007734`, `hal_gpio_set_dir`
`0x0800774C`) that selects bank 0/1 from the pin number and does read-modify-write on the
registers above. Pin number **0xFF is the firmware's "no-pin" sentinel**: the HAL's shift
degenerates so writes have no effect and reads yield 0 — the emulator gets this for free if
the registers are RAM-backed. (Observed)

### 1.1 The output latch must read back what was written

`GPIO_OUT` is not write-only. The firmware **reads the output latch back** in at least two
places that matter:

- **GPIO16 (speaker-amp enable)** is read back as an input each idle tick to gate a
  periodic quiet-path codec re-init and a book-mode USB re-poll. On real hardware the amp
  pad reads 1 while driven on. (Observed — byte-verified read at `0x080A6A0C`)
- **GPIO15 (power-hold)** read-back keeps the shutdown paths consistent. (Observed ops)

A model that returns a blanket constant (0 or all-ones) for `GPIO_OUT` reads is wrong:
all-ones happens to keep the happy path alive but makes power-off invisible and forces the
GPIO16 branch permanently on; all-zeros kills the amp branch. **Back the latch with RAM:
reads return the last written word, reset value 0.** Additionally, reflect the driven level
of output pins that are read via `GPIO_IN` (GPIO16, §2) into the input register. (Observed)

---

## 2. ★ Physical pin map (bank 0)

Complete list of every pin the firmware touches. "Boot value" = what the emulator must
present in `GPIO_IN` (inputs) or what the firmware drives (outputs) for a normal idle
retail boot: buttons released, no USB cable, no headphones, OID sensor idle.

| pin | function | dir | active level | boot value | detail / cross-ref | status |
|---|---|---|---|---|---|---|
| 0 | **Volume-up ('+') button** | in | **active-LOW** (0 = pressed) | **1** (released) | key code 5 (§3) | Observed |
| 1 | **Volume-down ('−') button** | in | active-HIGH (1 = pressed) | **0** (released) | key code 6 (§3); also half of the test-mode chord (§5) | Observed |
| 2 | **OID sensor CLOCK** | out | — | 0 | firmware bit-bangs the sensor serial link; → `oid-sensor.md` | Observed |
| 5 | **ZC90B auth DATA** (bidirectional) | out at boot, driven 1 | — | 1 | challenge/response data line; → `zc90b-auth.md`. The event pump also idles this pin (dir=out, pulse 1→0) around **every** dispatched event — bus idling, not an indicator. | Observed ops; device identity per `zc90b-auth.md` |
| 6 | USB power-path / shutdown strobe | out | 1 = asserted | 0 | driven high ~1 s before the power-latch drop in the button power-off path; otherwise only in charger/USB states → `usb-musb-device.md` | Observed ops / role Inferred |
| 7 | Headphone detect | in | 1 = plugged (mutes speaker amp) | **0** (no headphones) | polled by the audio tick; → `audio-dac-dma.md` | Observed |
| 8 | **USB / VBUS detect** | in (pull) | 1 = cable present | **0** (standalone) | 1 routes boot into the USB/charger states and gates OID off in ~40 handlers; **must be 0 for a book session**; → `usb-musb-device.md` | Observed |
| 9 | **OID sensor DATA** | in (idle; briefly out during handshake) | — | **1** (idle high) | 0 = "frame pending"; driven by the sensor model; also ROM boot strap; → `oid-sensor.md` | Observed |
| 10 | **ZC90B auth CLOCK** | out | — | 0 | → `zc90b-auth.md` | Observed |
| 11 | **Power button** | in | active-HIGH (1 = pressed) | **0** (released) | key code 8 (§3); sampled raw at boot: the boot task latches `gpio_read(11)` into a context flag (held ⇒ auto-mount/resume branch); in standby, 11==1 triggers a content rescan + soft reboot. A **static** 1 causes a reboot loop — present 0. | Observed |
| 12 | Audio mute / pop-suppression | out | 1 = asserted | 0 (pulsed in codec/USB reinit) | also ROM boot strap bit12 (must read 1 at ROM strap-sampling; carrying 1 in `GPIO_IN` into runtime is harmless — PROG never re-decodes straps) | Observed ops / role Inferred |
| 13 | Codec / enable strobe | out | — | 1 (Hi-Z during flash program) | also ROM boot strap bit13 (same note as pin 12) | Observed ops / role Inferred |
| 15 | **POWER-HOLD latch** | out | **1 = stay powered, 0 = power off** | firmware drives **1** early in boot | releasing it cuts the pen's own supply — the emulator's clean-shutdown signal (§4) | Observed |
| 16 | **Speaker AMP enable** | out, **read back as input** | 1 = amp on | 0 (off until playback) | `GPIO_IN` bit16 must mirror the driven latch value (§1.1); → `audio-dac-dma.md` | Observed |
| 0xFF | dummy "no-pin" sentinel | — | — | reads 0 | writes are no-ops; used by the vestigial fatal-blink loop (§5) and the post-power-off supply-death poll | Observed |

Pins **3, 4, 14, and 17+** are never read or written by any code path. No bank-1 pin is
used. (Observed — exhaustive sweep of all GPIO-write callers)

**Composite idle `GPIO_IN` word for a normal retail boot: `0x00003201`** — bits 0 (vol+
released, active-low), 9 (OID data idle high), 12, 13 (straps high). Bits 13/12/9 all high
also satisfies the mask ROM's "normal storage boot" strap sample, so the same word works
from reset. Bit 16 is then dynamic (mirrors the amp latch), bit 9 is dynamic once the OID
model runs, bit 5 is dynamic while the auth chip answers. (Observed)

---

## 3. Buttons and key scanning

The pen has exactly three buttons — power (GPIO11), volume-up (GPIO0, active-low),
volume-down (GPIO1) — read by **plain GPIO polling**. There is no keypad controller, and no
button travels through the OID decoder. (Observed)

### 3.1 The key reader

`hal_key_read` (resident HAL, `0x08006B2C`), in priority order:

```
if boot_latch:                        # power button still held since power-on
    if gpio_read(11) == 0: boot_latch = 0
    return 0xFF                       # all keys ignored until power is released
if gpio_read(11) == 1: return 8      # POWER        (active HIGH)
if gpio_read(0)  == 0: return 5      # VOLUME-UP    (active LOW)
if gpio_read(1)  == 1: return 6      # VOLUME-DOWN  (active HIGH)
return 0xFF                           # no key
```

The **boot latch** is set by the boot task right after main init returns, so the press that
powered the pen on can never fire a key event; scanning becomes live only after the power
button is first released. (Observed)

### 3.2 Scan → debounce → hold → event

Timings are in ticks of the 20 ms system timer (`interrupts-and-timers.md`); absolute
milliseconds are Inferred from that tick, the tick counts are Observed.

1. A periodic **~120 ms scan timer** calls `hal_key_read`. On a non-0xFF key it stores the
   key and arms a **~20 ms one-shot debounce** timer.
2. Debounce re-reads; if the same key is still down it hands off to a **~120 ms hold
   tracker**, else back to scanning.
3. The hold tracker counts while the key stays down:
   - at count == 5 (**~600 ms**) it posts a **HOLD** event `{code, sub=1}`;
   - past that, for the volume keys only, every 2nd tick (**~every 240 ms**) posts a
     **REPEAT** `{code, sub=3}`;
   - on release it posts `{code, sub=0}` if no hold event fired (i.e. **a short press is
     posted on release with sub=0**), else `{code, sub=2}` (release-after-hold); then it
     resets and re-arms the scan.

### 3.3 The key event

Every button reaches the firmware's statechart as **event id `0x105F` with a two-byte
payload `{code, sub}`** (posted with deduplication for sub ∈ {1,2}):

| field | values |
|---|---|
| `code` | **5** = volume-up, **6** = volume-down, **8** = power |
| `sub` | **0** = short press (posted on release), **1** = held ~600 ms, **2** = release after a hold, **3** = auto-repeat while held (volume keys only) |

Retail-mode dispatch (a state-global handler, active in every state): code 5/6 with sub 0
step the master volume index (0..5, boot default 3, RAM-only) with a feedback blip voice;
**code 8 with sub 1 (power held ~600 ms) triggers the clean power-off path** (goodbye voice,
then §4); a short power tap does nothing in retail mode. In factory test mode the same
events drive the test sequence instead (§5). (Observed)

---

## 4. Power-hold and power-off

**GPIO15 is the power-hold latch.** The firmware drives it to 1 early in boot ("keep my own
supply on") and every shutdown path — power-button hold, auto-off timeout, low-battery
final, auth failure, mount failure — ends by performing teardown and then **writing
GPIO15 = 0**, after which the code spins polling the dummy pin 0xFF waiting for the supply
to die. On real hardware the pen loses power at the 0-write. (Observed)

**Emulator contract:** treat the `GPIO_OUT` bit15 **1→0 transition** as the pen powering
off and terminate the run cleanly (otherwise every shutdown is an invisible spin loop).
The GPIO6 high-strobe ~1 s beforehand (button path) is a useful secondary observable but
not required. (Observed behaviour; termination policy Inferred/recommended)

---

## 5. The LED

**There is no SoC-driven LED.** (Observed — exhaustive)

- The OID-detect path performs **no GPIO write at all** on a successful decode — it only
  reads GPIO9, decodes, and posts the tap event. No event consumer pulses an output pin
  for indication.
- Every output pin the firmware ever writes has a known non-LED role (§2), and the
  unassigned pins (3/4/14/17+) are never written.
- The only LED-named routine — a fatal low-battery "blink" in the boot battery check —
  toggles the **dummy pin 0xFF** six times (~100 ms half-periods) and then drops GPIO15:
  a no-op vestige of vendor reference code; it drives nothing on this board.

The **visible flash on an OID tap** on real pens comes from **outside the SoC**: the OID
sensor module's own capture/illumination LED strobes when it images the paper (Inferred —
consistent with the sensor ASIC's autonomous decode and the total absence of any
decode-time GPIO write; see `oid-sensor.md`). The power-button PCB also carries a
fixed-cadence hardware indicator not under firmware control. (Inferred)

**Emulator contract: nothing to model.** Keep pin 0xFF as a harmless no-op (RAM-backed
registers give this for free). If cosmetic per-tap illumination is ever wanted it belongs
in the OID sensor model, not in a GPIO pin.

---

## 6. GPIO interrupts (summary)

GPIO "interrupts" are **timer-polled, not edge hardware**: while the timer block's
GPIO-scan enable is set, the periodic timer ISR compares each pin enabled in
`GPIO_INT_EN` against its `GPIO_INT_POL` expectation (polarity bit set = expect LOW) using
`GPIO_IN`, and dispatches on mismatch through the same top-level timer interrupt line.
The firmware arms this e.g. for **pin 8 as the USB-cable wake source**. Full mechanics in
`interrupts-and-timers.md`. (Observed)

**Model warning:** `GPIO_INT_EN`/`GPIO_INT_POL` (`0xE0/0xE4/0xF0/0xF4`) must read **0**
when never written. A default of all-ones makes the scan treat every pin as an enabled
active-low interrupt and storms the timer line. (Observed failure mode)

---

## 7. Test-mode entry chord

Factory test mode (PROD-TEST) is entered by the classic "hold volume-down while powering
on" gesture. In pin terms: early in the boot splash the firmware samples the **raw levels**
of **GPIO11 (power) AND GPIO1 (vol−)** four times, with ~5/5/20 ms between samples — both
must read **1** through the first ~30 ms of splash entry (well under a second after
power-on). All four samples high ⇒ test mode: the pen announces it by voice, checksums its
firmware image, and then volume-up presses step through OID / auth-chip / audio tests while
any OID tap is read out in digits; a long power press exits via the normal power-off.
(Observed)

Emulator recipe: present `GPIO_IN` bits 11 and 1 = 1 from reset (i.e. idle word `0x3201 |
0x802 = 0x3A03`), **keeping bit 0 = 1** (vol+ is active-low; an all-zero word would read
vol+ as permanently pressed). Once the test-mode voice is requested, drop bits 11 and 1
back to 0 — release both within one ~120 ms scan period of each other (or keep bit 1 high
past the ~600 ms hold threshold), otherwise the vol− release posts a short press that
immediately starts the first vol− test. While bit 11 stays 1 the boot latch (§3.1) keeps
the scanner muted, so nothing fires prematurely. (Observed gate; recipe Inferred from the
scan timings)

---

## 8. Emulator model (checklist)

1. **Registers:** back the whole GPIO register set with RAM (read returns last write;
   read-modify-write safe). Reset values: `GPIO_OUT` = 0; `GPIO_INT_EN/POL` = 0 (§6);
   `GPIO_DIR`/`GPIO_PULL` are RAM-like and their exact reset value is not load-bearing
   (the firmware configures every pin it uses).
2. **`GPIO_IN` composition** (recompute on every read):
   - static idle base **`0x00003201`** (buttons released, no USB, no headphones, OID data
     idle high, straps high);
   - **bit 9** ← the OID sensor model (`oid-sensor.md`);
   - **bit 5** ← the ZC90B model while it drives the data line (`zc90b-auth.md`);
   - **bit 16** ← mirror of `GPIO_OUT` bit 16 (amp read-back, §1.1);
   - **bit 8** ← 1 iff a USB cable is plugged (`usb-musb-device.md`);
   - **bits 0/1/11** ← button state (bit 0 inverted: pressed = 0).
3. **`GPIO_OUT` read-back:** return the RAM word (§1.1). Watch writes for:
   - **bit 15 falling edge → clean power-off**, terminate (§4);
   - **bit 2 edges** → feed the OID serial model (`oid-sensor.md`);
   - **bit 10 edges + bit 5 value** → feed the ZC90B model (`zc90b-auth.md`);
   - **bit 16** → amp on/off for audio capture (`audio-dac-dma.md`).
4. **Button injection:** a press = flip the pin's `GPIO_IN` bit to its active level for
   **~250–500 ms** (≥ 2 scan periods + debounce, < the ~600 ms hold threshold), then back —
   the short-press event posts on release. A hold = keep it active ≥ ~700 ms (sub=1 fires
   at ~600 ms; e.g. power-off needs this). Do not press anything before boot completes if a
   plain idle boot is wanted: **GPIO11 must be 0 at boot** or the firmware takes the
   held-power resume branch / standby reboot path.
5. **No LED, no watchdog, no keypad controller** — nothing else to satisfy in this block.
