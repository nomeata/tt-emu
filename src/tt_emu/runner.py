"""Headless runner: load firmware, boot, emulate forward, report progress.

Boots per ``memory-map-and-boot.md`` §5 and runs to a checkpoint or a stop
condition (power-off, fault, budget). Boot health checkpoints (§5.8) are wired
as code hooks so progress is observable: ``app_init_main`` entry/return, the
event-pump idle loop, and the fatal-hang addresses of the self-tests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .boot import BootedMachine, build_machine
from .fat16 import files_from_dir
from .loader import Firmware, load_upd
from .machine import MachineConfig, RunResult

log = logging.getLogger(__name__)

# --- Boot checkpoints (memory-map-and-boot.md §4/§5.8) -----------------------------

#: Named PC checkpoints; reaching one is logged and recorded.
CHECKPOINTS: dict[int, str] = {
    0x08039100: "PROG entry (§5.2)",
    0x08038F5C: "app_init_main entry (§4)",
    0x08038DA8: "power_battery_check (§5.4)",
    0x0804C47C: "anticlone_zc90b_verify (§5.4 item 3)",
    0x0800B4A4: "event-pump main loop (§4 / §5.8 'main loop idle')",
}

#: Fatal-hang addresses the self-tests branch to on failure (§5.4).
FATAL_ADDRS: dict[int, str] = {
    0x0804E50C: "ZC90B auth failure fatal hang (§5.4 item 3)",
    0x08038D24: "fatal_low_battery_blink (battery-and-power.md §2.3)",
}

# --- Mount health globals (memory-map-and-boot.md §5.8, index.md RAM table) ---------

#: Mem-driver vtable "keystone": *0x081db984 == 0x08038cf8 after a good mount.
KEYSTONE_ADDR = 0x081D_B984
KEYSTONE_EXPECTED = 0x0803_8CF8
#: Codepage-active flag byte (nonzero once codepage_load succeeded).
CODEPAGE_FLAG_ADDR = 0x081D_B730
#: Booklist head (populated by the discovery scan).
BOOKLIST_HEAD_ADDR = 0x081D_A080


@dataclass
class BootReport:
    """Structured result of a headless boot run."""

    result: RunResult
    checkpoints_hit: list[tuple[int, int, str]] = field(default_factory=list)
    timer_irqs: int = 0
    irqs_delivered: int = 0
    keystone: int = 0
    codepage_flag: int = 0
    booklist_head: int = 0
    nand_reads: int = 0
    nand_programs: int = 0
    nand_erases: int = 0

    @property
    def mount_ok(self) -> bool:
        """§5.8 "Keystone self-built" — the NFTL init ran through the real mount."""
        return self.keystone == KEYSTONE_EXPECTED

    def format_log(self) -> str:
        lines = ["boot run log:"]
        for clock, pc, name in self.checkpoints_hit:
            lines.append(f"  [{clock:>10} insn] pc={pc:#010x}  {name}")
        lines.append(
            f"  stop: {self.result.reason} at pc={self.result.pc:#010x} "
            f"after ~{self.result.instructions} insn"
        )
        lines.append(f"  timer IRQs latched: {self.timer_irqs}, delivered: {self.irqs_delivered}")
        lines.append(
            f"  mount: keystone={self.keystone:#010x} "
            f"({'OK' if self.mount_ok else 'not built'}), "
            f"codepage flag={self.codepage_flag:#x}, booklist head={self.booklist_head:#010x}"
        )
        lines.append(
            f"  nand: {self.nand_reads} reads, {self.nand_programs} programs, "
            f"{self.nand_erases} erases"
        )
        return "\n".join(lines)


def boot_firmware(
    path: str,
    *,
    max_instructions: int = 40_000_000,
    config: MachineConfig | None = None,
    a_dir: str | None = None,
    b_dir: str | None = None,
) -> BootReport:
    """Load ``path``, boot, and emulate up to ``max_instructions``; return a report.

    ``a_dir``/``b_dir`` are optional host directories mirrored onto the A:
    (system) / B: (user ``.gme``) NAND partitions (``nand-image-layout.md`` §5).
    """
    firmware: Firmware = load_upd(path)
    booted: BootedMachine = build_machine(
        firmware,
        config,
        a_files=files_from_dir(a_dir) if a_dir else None,
        b_files=files_from_dir(b_dir) if b_dir else None,
    )
    machine = booted.machine

    hits: list[tuple[int, int, str]] = []
    seen: set[int] = set()

    def make_checkpoint_hook(addr: int, name: str, fatal: bool):
        def hook(m) -> None:
            if addr not in seen:
                seen.add(addr)
                hits.append((m.clock, addr, name))
                log.info("checkpoint pc=%#010x %s", addr, name)
            if fatal:
                m.request_stop(f"fatal: {name}")

        return hook

    for addr, name in CHECKPOINTS.items():
        machine.on_code(addr, make_checkpoint_hook(addr, name, fatal=False))
    for addr, name in FATAL_ADDRS.items():
        machine.on_code(addr, make_checkpoint_hook(addr, name, fatal=True))

    result = machine.run(max_instructions)
    report = BootReport(
        result=result,
        checkpoints_hit=hits,
        timer_irqs=booted.machine.intc.timer_irqs if booted.machine.intc else 0,  # type: ignore[attr-defined]
        irqs_delivered=machine.irqs_delivered,
        keystone=machine.read_u32(KEYSTONE_ADDR),
        codepage_flag=machine.read_u8(CODEPAGE_FLAG_ADDR),
        booklist_head=machine.read_u32(BOOKLIST_HEAD_ADDR),
        nand_reads=booted.nand.reads,
        nand_programs=booted.nand.programs,
        nand_erases=booted.nand.erases,
    )
    return report
