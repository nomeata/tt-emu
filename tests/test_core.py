"""Unit tests for the tt-emu core, framework, and peripherals.

These exercise the pieces the (NAND-blocked) headless boot cannot reach yet:
IRQ delivery, the ZC90B wire model, register constants, and the loader. The
full-boot integration test is skipped unless ``update3202MT.upd`` is present.
"""

from __future__ import annotations

import logging
import struct

import pytest
from _data import firmware_path

from tt_emu.loader import CODEPAGE_SIZE, load_upd
from tt_emu.machine import IRQ_VECTOR, Machine, MachineConfig
from tt_emu.peripheral import WordRegisterPeripheral
from tt_emu.peripherals.battery import ADC_DATA, ADC_DATA_HEALTHY, BatteryAdc
from tt_emu.peripherals.gpio import GPIO_DIR0, GPIO_IN0, GPIO_OUT0, GpioBlock, PIN_POWER_HOLD
from tt_emu.peripherals.intc import IntcTimer, NOMINAL_RELOAD, TIMER1_CTRL
from tt_emu.peripherals.syscon import CHIP_ID, SysCon
from tt_emu.peripherals.zc90b import PIN_CLOCK, PIN_DATA, Zc90bAuth

UPD_PATH = firmware_path()


# --- WordRegisterPeripheral -------------------------------------------------------


def test_word_register_subword_access() -> None:
    p = WordRegisterPeripheral()
    p.write(0, 4, 0xAABBCCDD)
    assert p.read(0, 4) == 0xAABBCCDD
    assert p.read(0, 1) == 0xDD  # little-endian byte 0
    assert p.read(3, 1) == 0xAA
    assert p.read(2, 2) == 0xAABB
    p.write(1, 1, 0x11)  # RMW on byte 1
    assert p.read(0, 4) == 0xAABB11DD


# --- SysCon: chip-ID gate + self-clearing latch bits ------------------------------


def test_syscon_chip_id_constant_and_write_ignored() -> None:
    s = SysCon()
    assert s.read_reg(0x00) == CHIP_ID
    s.write_reg(0x00, 0xDEADBEEF)
    assert s.read_reg(0x00) == CHIP_ID


@pytest.mark.parametrize("bit", [12, 13, 14, 21])
def test_syscon_clk_latch_bits_read_zero(bit: int) -> None:
    s = SysCon()
    s.write_reg(0x04, (1 << bit) | 0x43)
    assert (s.read_reg(0x04) >> bit) & 1 == 0
    assert s.read_reg(0x04) & 0x43 == 0x43  # persistent bits survive


def test_syscon_analog_pd_strobe_reads_zero() -> None:
    s = SysCon()
    s.write_reg(0x10, 1 << 8)
    assert (s.read_reg(0x10) >> 8) & 1 == 0


# --- Battery ADC ------------------------------------------------------------------


def test_battery_adc_constant() -> None:
    b = BatteryAdc()
    assert b.read_reg(ADC_DATA) == ADC_DATA_HEALTHY
    b.write_reg(ADC_DATA, 0)  # read-only from firmware's view
    assert b.read_reg(ADC_DATA) == ADC_DATA_HEALTHY


# --- GPIO: readback, IN composition, power-off ------------------------------------


def test_gpio_out_reads_back_and_in_composition() -> None:
    g = GpioBlock()
    g.write_reg(GPIO_OUT0, 0x8000)  # power-hold high (GPIO15)
    assert g.read_reg(GPIO_OUT0) == 0x8000
    g.read_reg(GPIO_IN0)  # app-init sample releases the boot-held power button (§7.3.1a)
    assert g.input_word() == 0x00003201  # idle base (gpio-buttons-led.md §2)
    g.set_input(9, 0)  # OID data pulled low
    assert (g.input_word() >> 9) & 1 == 0
    g.write_reg(GPIO_OUT0, 0x8000 | (1 << 16))  # amp on -> mirrored into GPIO_IN
    assert (g.input_word() >> 16) & 1 == 1


def test_gpio_power_hold_release_stops_machine() -> None:
    m = _bare_machine()
    g = GpioBlock()
    m.add_peripheral(g)
    g.write(GPIO_OUT0, 4, 1 << PIN_POWER_HOLD)  # latch power-hold on (0->1)
    assert m.stop_reason is None
    g.write(GPIO_OUT0, 4, 0)  # release (1->0)
    assert m.stop_reason is not None and "power" in m.stop_reason


# --- IntcTimer: timer latch/ack, delivery gating ----------------------------------


def test_intc_timer_latch_and_ack() -> None:
    m = _bare_machine()
    g = GpioBlock()
    intc = IntcTimer(g)
    m.add_peripheral(g)
    m.add_peripheral(intc)
    m.intc = intc
    # arm timer1 (reload = nominal 20 ms, enable) exactly as the firmware does.
    intc.write(TIMER1_CTRL, 4, NOMINAL_RELOAD | (1 << 26) | (1 << 27))
    # advance model time past one period.
    per_tick = m.config.instructions_per_tick
    intc.tick(per_tick + 1)
    assert intc.timer_irqs == 1
    assert intc.pending() & (1 << 10)  # line 10 latched
    assert intc.irq_asserted()  # enable default 0xFFFFFFFF
    # firmware ACK: write bit28.
    intc.write(TIMER1_CTRL, 4, 1 << 28)
    assert not (intc.pending() & (1 << 10))


def test_machine_delivers_irq_to_vector() -> None:
    """A pending+enabled line, IRQs unmasked, vectors the CPU to 0x08000018."""
    m = _bare_machine()

    class AlwaysPending:
        def irq_asserted(self) -> bool:
            return True

    m.intc = AlwaysPending()  # type: ignore[assignment]
    # nandboot IRQ vector holds code; plant an infinite self-loop there so the
    # chunk after delivery lands on it deterministically.
    m.write_bytes(IRQ_VECTOR, struct.pack("<I", 0xEAFFFFFE))  # b .
    m.write_bytes(0x08039100, struct.pack("<I", 0xEAFFFFFE))  # main: b .
    m.set_entry_state(0x08039100, 0x08420000, 0x13)  # SVC, IRQs on
    m.run(m.config.chunk_instructions * 2)
    assert m.irqs_delivered >= 1
    assert m.pc == IRQ_VECTOR
    assert (m.cpsr & 0x1F) == 0x12  # IRQ mode
    assert m.cpsr & 0x80  # I set on entry


def test_machine_irq_gated_by_cpsr_i() -> None:
    m = _bare_machine()

    class AlwaysPending:
        def irq_asserted(self) -> bool:
            return True

    m.intc = AlwaysPending()  # type: ignore[assignment]
    m.write_bytes(0x08039100, struct.pack("<I", 0xEAFFFFFE))
    m.set_entry_state(0x08039100, 0x08420000, 0x13 | 0x80)  # IRQs MASKED
    m.run(m.config.chunk_instructions)
    assert m.irqs_delivered == 0


# --- Machine: realtime pacing ------------------------------------------------------


def test_config_rejects_unknown_pacing() -> None:
    with pytest.raises(ValueError):
        MachineConfig(pacing="fast")


def test_realtime_pacing_tracks_wall_time() -> None:
    """Realtime chunks advance the clock by wall time at the modelled rate.

    ipt=20_000 models 1M insn/s; a 100k-instruction budget is therefore
    ~100 ms of wall time (generous upper bound for CI jitter), and the pacer
    thread must be gone when run() returns.
    """
    import threading
    import time

    m = Machine(MachineConfig(instructions_per_tick=20_000, pacing="realtime"))
    m.write_bytes(0x08000000, struct.pack("<I", 0xEAFFFFFE))  # b .
    m.set_entry_state(0x08000000, 0x08020000, 0x13)
    t0 = time.monotonic()
    result = m.run(100_000)
    elapsed = time.monotonic() - t0
    assert result.reason == "instruction budget exhausted"
    assert m.clock >= 100_000
    assert 0.05 <= elapsed <= 10.0  # ~0.1 s nominal; wide bounds for slow CI
    assert not any(t.name == "tt-emu-pacer" for t in threading.enumerate())
    # The machine resumes: a second bounded run continues from the same state.
    result = m.run(20_000)
    assert m.clock >= 120_000


def test_realtime_pacing_delivers_irqs_between_chunks() -> None:
    """Wall-paced chunks still tick peripherals and deliver IRQs at boundaries."""
    m = Machine(MachineConfig(instructions_per_tick=1_000, pacing="realtime"))

    class AlwaysPending:
        def irq_asserted(self) -> bool:
            return True

    m.intc = AlwaysPending()  # type: ignore[assignment]
    m.write_bytes(IRQ_VECTOR, struct.pack("<I", 0xEAFFFFFE))  # b .
    m.write_bytes(0x08039100, struct.pack("<I", 0xEAFFFFFE))  # main: b .
    m.set_entry_state(0x08039100, 0x08420000, 0x13)  # SVC, IRQs on
    m.run(2_000)  # two ticks' worth of emulated time (~40 ms wall)
    assert m.irqs_delivered >= 1
    assert m.pc == IRQ_VECTOR
    assert (m.cpsr & 0x1F) == 0x12  # IRQ mode


# --- Machine: checkpoint hooks + semihosting ---------------------------------------


def test_on_code_checkpoint_and_request_stop() -> None:
    """`on_code` fires at the watched PC and `request_stop` ends the run cleanly."""
    m = _bare_machine()
    m.write_bytes(0x08000000, struct.pack("<I", 0xEAFFFFFE))  # b .
    hits: list[int] = []

    def checkpoint(mm: Machine) -> None:
        hits.append(mm.pc)
        mm.request_stop("checkpoint reached")

    m.on_code(0x08000000, checkpoint)
    m.set_entry_state(0x08000000, 0x08020000, 0x13)
    result = m.run(10_000)
    assert result.reason == "checkpoint reached"
    assert hits and hits[0] == 0x08000000


def test_semihosting_write0_logged(caplog: pytest.LogCaptureFixture) -> None:
    """`svc 0xab` SYS_WRITE0 logs the NUL-terminated string (§1.1) and returns."""
    m = _bare_machine()
    code = struct.pack(
        "<IIII",
        0xE3A00004,  # mov r0, #4          (SYS_WRITE0)
        0xE59F1004,  # ldr r1, [pc, #4]    (-> literal below)
        0xEF0000AB,  # svc 0xab
        0xEAFFFFFE,  # b .
    ) + struct.pack("<I", 0x08000100)  # literal: message address
    m.write_bytes(0x08000000, code)
    m.write_bytes(0x08000100, b"hello from the blob\x00")
    m.set_entry_state(0x08000000, 0x08020000, 0x13)
    with caplog.at_level(logging.INFO, logger="tt_emu.machine"):
        m.run(1_000)
    assert "hello from the blob" in caplog.text
    from unicorn.arm_const import UC_ARM_REG_R0

    assert m.uc.reg_read(UC_ARM_REG_R0) == 0  # success return


# --- ZC90B wire model -------------------------------------------------------------


def _fake_sboxes() -> dict[int, bytes]:
    # Identity-ish permutations so we can predict the response.
    table_a = bytes((i ^ 0xA5) & 0xFF for i in range(256))
    table_b = bytes((i ^ 0x3C) & 0xFF for i in range(256))
    table_c = bytes((i ^ 0x5A) & 0xFF for i in range(256))
    return {0x080B0078: table_a, 0x080B0178: table_b, 0x080B0278: table_c}


def test_zc90b_bitbang_exchange() -> None:
    g = GpioBlock()
    z = Zc90bAuth(g)
    boxes = _fake_sboxes()
    z.load_tables(lambda addr, size: boxes[addr][:size])

    c1, c2, c3 = 0x9D, 0x42, 0x17
    b = boxes[0x080B0178][c2 & 0xBE]
    c = boxes[0x080B0278][(c1 ^ b) & 0xFF]
    a = boxes[0x080B0078][c3 & 0xD7]
    expected = bytes((c, b, a))

    # Firmware side: drive GPIO5 as output, clock out 24 challenge bits MSB-first.
    g.write(GPIO_DIR0, 4, g.read(GPIO_DIR0, 4) & ~(1 << PIN_DATA))  # DATA = output
    for byte in (c1, c2, c3):
        for i in range(8):
            bit = (byte >> (7 - i)) & 1
            _set_out(g, PIN_CLOCK, 1)
            _set_out(g, PIN_DATA, bit)
            _set_out(g, PIN_CLOCK, 0)  # falling edge latches

    # Switch DATA to input (ready handshake); poll should read high.
    g.write(GPIO_DIR0, 4, g.read(GPIO_DIR0, 4) | (1 << PIN_DATA))
    assert (g.input_word() >> PIN_DATA) & 1 == 1

    # Clock in 24 response bits; sample GPIO5 while clock low, MSB-first.
    got_bits = []
    for _ in range(24):
        _set_out(g, PIN_CLOCK, 1)  # rising edge presents the bit
        _set_out(g, PIN_CLOCK, 0)
        got_bits.append((g.input_word() >> PIN_DATA) & 1)
    got = bytes(
        sum(got_bits[byte * 8 + i] << (7 - i) for i in range(8)) for byte in range(3)
    )
    assert got == expected


def test_zc90b_spurious_leading_edge_before_challenge() -> None:
    """Regression: the firmware emits a spurious clock fall *before* it drives
    GPIO5 to output (Observed on the real boot exchange). The challenge must be
    delimited by the GPIO5 direction, not a raw bit count — otherwise that stray
    edge is miscounted as challenge bit 0, the whole challenge shifts by one, the
    reply is wrong, and the auth gate powers the pen off (0x804e50c)."""
    g = GpioBlock()
    z = Zc90bAuth(g)
    boxes = _fake_sboxes()
    z.load_tables(lambda addr, size: boxes[addr][:size])

    c1, c2, c3 = 0x71, 0x06, 0x3E
    b = boxes[0x080B0178][c2 & 0xBE]
    c = boxes[0x080B0278][(c1 ^ b) & 0xFF]
    a = boxes[0x080B0078][c3 & 0xD7]
    expected = bytes((c, b, a))

    # GPIO5 idle as an input before the exchange (as on the real boot).
    g.write(GPIO_DIR0, 4, g.read(GPIO_DIR0, 4) | (1 << PIN_DATA))
    # A stray high→low clock edge while GPIO5 is still an input (pre-challenge).
    _set_out(g, PIN_CLOCK, 1)
    _set_out(g, PIN_CLOCK, 0)  # spurious fall — must NOT count as a challenge bit

    # Now the real challenge: GPIO5 → output (the delimiting transition), 24 bits.
    g.write(GPIO_DIR0, 4, g.read(GPIO_DIR0, 4) & ~(1 << PIN_DATA))
    for byte in (c1, c2, c3):
        for i in range(8):
            _set_out(g, PIN_CLOCK, 1)
            _set_out(g, PIN_DATA, (byte >> (7 - i)) & 1)
            _set_out(g, PIN_CLOCK, 0)
    # A trailing extra clock fall before the direction change (Observed too).
    _set_out(g, PIN_CLOCK, 1)
    _set_out(g, PIN_CLOCK, 0)

    # GPIO5 → input ends the challenge and triggers the response.
    g.write(GPIO_DIR0, 4, g.read(GPIO_DIR0, 4) | (1 << PIN_DATA))
    assert (g.input_word() >> PIN_DATA) & 1 == 1

    got_bits = []
    for _ in range(24):
        _set_out(g, PIN_CLOCK, 1)
        _set_out(g, PIN_CLOCK, 0)
        got_bits.append((g.input_word() >> PIN_DATA) & 1)
    got = bytes(sum(got_bits[byte * 8 + i] << (7 - i) for i in range(8)) for byte in range(3))
    assert got == expected


def _set_out(g: GpioBlock, pin: int, level: int) -> None:
    word = g.read(GPIO_OUT0, 4)
    if level:
        word |= 1 << pin
    else:
        word &= ~(1 << pin)
    g.write(GPIO_OUT0, 4, word)


# --- Loader -----------------------------------------------------------------------


@pytest.mark.skipif(UPD_PATH is None, reason="firmware .upd not available")
def test_loader_artifacts() -> None:
    fw = load_upd(UPD_PATH)
    assert fw.build_id == "N0038MT"
    assert fw.boot_generation == "ANYKANB1"
    assert fw.nandboot.offset == 0x20000 and fw.nandboot.size == 0x7E80
    assert fw.prog.offset == 0x28000 and fw.prog.size == 0x380000
    assert fw.codepage.size == CODEPAGE_SIZE
    # ZC90B S-boxes at their documented addresses (image base 0x08009000).
    base = 0x08009000
    table_c = fw.prog.data[0x080B0278 - base : 0x080B0278 - base + 256]
    table_b = fw.prog.data[0x080B0178 - base : 0x080B0178 - base + 256]
    table_a = fw.prog.data[0x080B0078 - base : 0x080B0078 - base + 256]
    # Worked example (zc90b-auth.md §3.3): 0,0,0 -> R1=0x16, R2=0xa3, R3=0xd3.
    bb = table_b[0]
    assert table_c[bb] == 0x16
    assert bb == 0xA3
    assert table_a[0] == 0xD3


@pytest.mark.skipif(UPD_PATH is None, reason="firmware .upd not available")
def test_boot_reaches_app_init_main() -> None:
    from tt_emu.runner import boot_firmware

    report = boot_firmware(str(UPD_PATH), max_instructions=5_000_000)
    names = {name for _, _, name in report.checkpoints_hit}
    assert any("PROG entry" in n for n in names)
    assert any("app_init_main" in n for n in names)
    # Timer1 gets armed and starts latching ticks during init.
    assert report.timer_irqs > 0


# --- helpers ----------------------------------------------------------------------


def _bare_machine() -> Machine:
    return Machine(MachineConfig(chunk_instructions=256, instructions_per_tick=1000))
