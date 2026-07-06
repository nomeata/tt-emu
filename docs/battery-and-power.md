# Battery measurement and power management (tiptoi 2N "MT")

How the 2N pen's firmware measures its battery, when it warns/shuts down, how the
inactivity auto-off works, and how the pen actually powers itself off — with the
exact register model an emulator needs so the firmware sees a healthy battery,
never enters a low-battery path, and shuts down detectably instead of mysteriously.

Evidence tags: **[Observed]** = read from the firmware disassembly/decompilation
(update3202MT, image base 0x08009000), from a live-pen RAM dump, or verified in an
emulator run. **[Inferred]** = deduced, reason given.

Related docs: [gpio-buttons-led.md](gpio-buttons-led.md) (GPIO block, the power-hold
pin, the three buttons, USB/VBUS detect), [interrupts-and-timers.md](interrupts-and-timers.md)
(the timer IRQ and the 0x1046 heartbeat event that drives everything below),
[system-control-and-clock.md](system-control-and-clock.md) (the 0x04000000 core
register window the ADC lives in), [usb-musb-device.md](usb-musb-device.md)
(the USB controller at 0x04070000 and PC-vs-charger classification).

> **There is no dedicated PMU peripheral.** In particular, 0x04070000 is the
> MUSB USB device controller (see usb-musb-device.md), *not* a power-management
> unit — an earlier analysis mislabeled it. Power management on this pen is
> entirely discrete: a battery ADC inside the core register window
> (0x04000064 / 0x04000070), a handful of GPIO pins (power-hold, VBUS detect,
> USB power-path switch), and software counters driven by the heartbeat timer.
> **[Observed]**

---

## 1. Register and parameter summary

| Address / value | Role |
|---|---|
| `0x04000064`, bit 9 (`0x200`) | Battery-ADC enable/channel select. Firmware sets it (read-modify-write) before sampling, clears it after. **[Observed]** |
| `0x04000070`, bits **[19:10]** | 10-bit battery ADC sample (read-only from the firmware's view). **[Observed]** |
| `0x04000004`, bit 15 | Toggled on/off around ADC bursts ("measurement enable" of some kind). Write-only in practice; no readback gate. **[Observed writes; semantics unknown]** |
| scaled value | `battery_adc_read` (0x0800b3c8) = average of 4 samples of `0x04000070[19:10]`, **×2** → an 11-bit "scaled raw". All thresholds below are in this scale. **[Observed]** |
| volts | ≈ `scaled × 3.3 / 1024` (the firmware's own log prints `scaled*33/10240` as "Battery %d"). The ADC pin sees roughly Vbat/2 of the 2×AA pack. **[Observed formula; divider Inferred]** |
| **`0x300`** (≈2.47 V) | **Low-battery warn** threshold (3 consecutive heartbeats below) and the **firmware-update refusal** threshold. **[Observed]** |
| **`0x2C0`** (≈2.27 V) | **Final / power-off** threshold (10 consecutive heartbeats below). **[Observed]** |
| `0x155` | Boot-calibration **floor**: `power_battery_check` requires its two sample averages to be ≥ ~0x155 and to agree with each other (window ≈ ±0x1E–0x20). **[Observed]** |
| **300** ticks | Idle **auto-off** budget: standby counter (u16 at 0x081da07c) > 300 accepted heartbeats → power off. At the ~100 ms heartbeat that is **~30 s**. **[Observed counter/threshold; 100 ms/tick Inferred from the 20 ms timer IRQ]** |
| GPIO **15** | **Power-hold latch** (output): 1 = pen stays powered, 0 = pen loses power. Every shutdown path ends by driving it 0. See gpio-buttons-led.md. **[Observed]** |
| GPIO **8** | VBUS / USB-cable detect (input, 1 = cable present). See gpio-buttons-led.md. **[Observed]** |
| GPIO **6** | USB power-path switch (output; only driven in charger/USB states). **[Observed writes; semantics Inferred]** |
| Live-pen reference | A healthy real pen's sample ring held raw ≈ **0x3AA** scaled (≈3.0 V), monitor baseline 0x396, low-batt stage 0. **[Observed — RAM dump]** |

---

## 2. The battery ADC

### 2.1 Reading

`battery_adc_read` (0x0800b3c8) **[Observed]**:

1. `0x04000064 |= 0x200` (enable the battery ADC channel);
2. read `0x04000070`, take bits [19:10], four times; average;
3. clear the enable bit;
4. return `average × 2` — the **scaled raw** value all thresholds use.

A second reader variant (0x0800b428) is used only by the boot calibration
(§2.3). Helper code also maintains a 4-entry sample ring, converts to millivolts
via calibration bytes, and bins into a charge level 1..8 — that path only feeds a
charging-screen animation and gates nothing. **[Observed]**

### 2.2 What "healthy" means

The runtime monitor (§3) treats **scaled ≥ 0x300** as fully healthy. Anything a
constant register can produce ≥ that value keeps the pen out of every
low-battery path forever, because the monitor's baseline self-recalibrates from
the readings and a constant never sags.

### 2.3 Boot-time calibration and the "fatal blink"

`power_battery_check` (0x08038da8) runs once from `app_init_main` **[Observed]**:

- **Skipped entirely if GPIO 8 == 1** (USB cable present — the measurement would
  see charge voltage).
- Otherwise: 10 quick samples via `battery_adc_read` plus 1000 samples via the
  second reader, under the `0x04000004` bit-15 "measurement enable". The two
  averages must each be ≥ ~**0x155** (a floor, not a window) and must agree with
  each other within ≈0x1E–0x20.
- **Failure → `fatal_low_battery_blink` (0x08038d24)**: holds GPIO 15 = 1,
  toggles the *dummy* GPIO 0xFF six times (this board has no firmware-driven
  LED, so nothing visible happens), then drives **GPIO 15 = 0 and spins** — the
  pen silently turns itself off at boot. **[Observed]**
- Success stores the averages as the runtime monitor's baseline (it
  self-corrects afterwards) and boot continues.

**Passing it in an emulator is automatic** with any constant ADC register value
whose sample field is ≥ ~0x155: both averages come out identical (so the
cross-check passes trivially) and above the floor. No calibration modeling is
needed. **[Observed — emulator-verified]**

Note: the GPIO 11 / GPIO 1 check at splash entry (four consecutive reads of
both == 1) is **not** a battery self-test, despite an internal firmware name
suggesting so — those pins are the **POWER and VOLUME-DOWN buttons**, and the
gate is the factory prod-test entry chord ("hold vol-down while pressing
power-on"). A retail boot reads them 0. Battery health is judged only by the
ADC. See gpio-buttons-led.md. **[Observed]**

---

## 3. The low-battery / shutdown cascade

A monitor function runs on **every 0x1046 heartbeat** (~100 ms; the heartbeat is
described in interrupts-and-timers.md). It calls `battery_adc_read` and compares
against its baseline **[Observed]**:

| Condition | Effect |
|---|---|
| scaled ≥ 0x2C0 and steady | OK. Baseline refreshed from the reading (self-recalibrating). |
| scaled **< 0x300** for **3** consecutive ticks | **Warn** stage: plays voice 0x17 ("battery low"), then keeps running. |
| scaled **< 0x2C0** for **10** consecutive ticks (or erratic jumps) | **Final** stage: plays voice 0x1A ("battery empty"), then the power-off jingle (voice 0x14), posts the power-off event 0x1062, and drives **GPIO 15 = 0** — the pen dies. |

Details that matter for emulation **[Observed]**:

- The voices are suppressed while on USB power (cable state ≠ battery-only, or
  GPIO 8 == 1).
- If no product is loaded when a stage triggers, the firmware additionally
  forces a descent into book mode to speak the warning — i.e. a low battery can
  make an "idle" pen suddenly enter book state and talk.
- **Firmware-update gate:** before applying a `B:/*.upd`, the updater takes 5
  ADC readings; any scaled sample **< 0x300** aborts the update and plays the
  `BatLowUpdate` language voice ("battery too low to update"). Same healthy
  constant passes this too.

With a constant healthy ADC value none of this ever fires; the cascade needs no
emulator modeling beyond the register itself.

---

## 4. Auto-off (inactivity power-off)

There is exactly one firmware-global inactivity timeout, and it runs **only in
idle standby with nothing mounted** (no product, no USB) **[Observed]**:

- On the first accepted event after entering standby, the handler arms "idle
  mode": re-arms the OID sensor for wake-on-tap and zeroes a **u16 counter at
  0x081da07c**.
- Every subsequent accepted event — in practice the periodic **0x1046
  heartbeat, ~100 ms** — increments the counter (the firmware logs
  `Count %d.`).
- **Counter > 300** → inline power-off: state machine to 0, amp off, timers
  reset, **GPIO 15 = 0**, spin. That is **~30 s** of idle at the assumed 20 ms
  timer IRQ (if the IRQ were 10 ms it would be ~15 s — the tick rate is the one
  **[Inferred]** parameter here; the count of 300 is **[Observed]**).

**What resets it:** any activity that leaves standby — an OID pen-down/tap
(→ book mode), or USB plug-in (→ the classification state). The counter is not
"reset in place"; leaving standby stops it, and re-entering standby restarts it
from 0. **[Observed]**

**Exceptions to arming it** **[Observed]**:

- If the power button was still held when the application initialized
  (GPIO 11 == 1 latched at boot), or a post-update resume flag file
  (`B:/FLAG.bin`) exists, standby instead immediately posts the auto-mount
  event 0x1058 and descends into book mode — no auto-off countdown.
- **In book mode (a product mounted) there is no firmware auto-off at all.**
  Long-idle behavior in a book is product-driven (GME scripts play reminder
  voices); the pen then stays on until the power button or the low-battery
  cascade. **[Observed absence in the book-mode handlers; overall Inferred]**

So an emulator run that boots to standby and receives no tap **will** see the
firmware power itself off after ~300 heartbeats. Either deliver activity within
that budget, or detect the shutdown cleanly (§5).

---

## 5. The power-off mechanism

The pen has no suspend-to-RAM and no software reset on the happy path: "off" is
losing power. The supply is held by the firmware driving **GPIO 15 = 1**
(done first thing in `app_init_main`); every shutdown path ends with
**GPIO 15 = 0 followed by an infinite spin** waiting for the supply to
collapse. (The spin loops poll the dummy GPIO 0xFF, which reads constant 0 —
they are intentional "wait until dead" loops.) **[Observed]**

Shutdown paths, all terminating in GPIO 15 = 0 **[Observed]**:

| Trigger | Sequence |
|---|---|
| Power button (key event 0x105F, code 8, held) | stop audio → power-off jingle voice 0x14 → event 0x1062 → GPIO 15 = 0 (on VBUS the off routine instead parks in a retry loop = the "off but charging" state) |
| Battery final-low (§3) | voice 0x1A → voice 0x14 → event 0x1062 → same path |
| Idle auto-off (§4) | inline: GPIO 15 = 0, no sound |
| Boot battery-calibration fatal (§2.3) | dummy-pin "blink" → GPIO 15 = 0 |
| Anti-clone auth failure, filesystem mount failure | GPIO 15 = 0 immediately |

**Emulator consequence:** model GPIO 15 as a tracked output and treat the
**1 → 0 transition as "pen powered off"** — terminate the run cleanly there.
Without that, every shutdown looks like a mystery hang (an eternal spin loop),
because nothing else distinguishes it. During normal operation GPIO 15 should
read back 1 (it is an output the firmware drove high). **[Observed —
emulator-verified]**

---

## 6. Charger / USB power (as far as it affects power management)

Full USB behavior is in usb-musb-device.md; the power-relevant slice
**[Observed]**:

- **GPIO 8** (input) is the VBUS/cable detect. 8 == 1 at boot **skips** the
  battery calibration (§2.3); 8 == 1 at runtime routes the pen into the
  classification state, where MUSB bus activity distinguishes a PC host from a
  dumb charger.
- Low-battery voices are suppressed while a cable is present.
- In the **charging** state a ~1 s tick counts up; after ~1000 ticks (~17 min)
  the firmware switches the power path to USB: GPIO 6 = 1, then **GPIO 15 = 0**
  (the pen now runs from VBUS, battery latch released). The **USB-PC** (mass
  storage) session similarly releases the battery latch after a long session
  while VBUS keeps the pen alive. So GPIO 15 = 0 means "clean shutdown" *only
  when no VBUS is modeled* — with GPIO 8 = 0 it is unambiguous.
- Cable removal (GPIO 8 → 0, debounced) exits back to standby, which re-arms
  the auto-off countdown (§4).

For a battery-only emulation, present **GPIO 8 = 0** permanently and this
entire section stays dormant.

---

## 7. Emulator model

Minimum faithful model for a healthy, battery-powered pen — everything here is
**[Observed]** working in an emulator against the unmodified firmware:

1. **Battery ADC — one constant.** Serve reads of `0x04000070` with a constant
   whose bits [19:10] are ≥ 0x180 (scaled ≥ 0x300). The proven value:

   ```
   0x04000070 = 0x000C0000    # bits[19:10] = 0x300 raw → scaled 0x600, ≫ all thresholds
   0x04000064 = 0x00000200    # harmless preset; firmware RMWs bit 9 itself anyway
   ```

   This passes the boot calibration (identical averages, above the 0x155
   floor), keeps the runtime monitor permanently in its OK mode, and satisfies
   the firmware-update gate. A more physically plausible constant is raw
   ≈ 0x1D5 (`0x00075400`, scaled ≈ 0x3AA ≈ 3.0 V — what a real healthy pen
   reads), but the firmware cannot tell the difference. No timer, no noise, no
   discharge curve needed.

2. **`0x04000064`, `0x04000004`** — plain read/write scratch is sufficient; the
   firmware never gates on reading back the bits it set on the battery path.

3. **GPIO** (see gpio-buttons-led.md for the full pin map): GPIO 8 = 0 (no
   USB), GPIO 11 = 0 and GPIO 1 = 0 (power / vol-down buttons not pressed —
   also keeps the prod-test chord and the boot "power-held" auto-mount latch
   off), GPIO 15 modeled as a real output latch.

4. **Auto-off — no emulator counter needed.** The firmware's own counter runs
   off the 0x1046 heartbeat (interrupts-and-timers.md); just be aware of the
   budget: **~300 heartbeats (~30 s emulated) of idle standby → the firmware
   powers off by itself.** Deliver the first OID tap within that budget for an
   interactive run, or use the timeout deliberately as a deterministic
   end-of-run. In book mode there is no timeout.

5. **Power-off detection**: hook writes that drive **GPIO 15 to 0** and stop
   emulation with a "pen powered off" result. This catches auto-off,
   battery-final, power-button, calibration-fatal, auth-fail, and mount-fail
   terminations uniformly.

6. **No PMU block.** Do not model any power peripheral at 0x04070000; that
   address must behave as the MUSB USB controller (usb-musb-device.md) or, in a
   USB-less run with GPIO 8 = 0, is never meaningfully touched.

### Open points

- The exact heartbeat period (100 ms vs a multiple) rests on the 20 ms timer-IRQ
  assumption; the auto-off wall-clock time scales with it. The tick *count*
  (300) and all ADC thresholds are exact. **[Inferred vs Observed as tagged]**
- The physical meaning of `0x04000004` bit 15 (measurement/load enable?) is
  unknown; scratch behavior suffices. **[Open]**
