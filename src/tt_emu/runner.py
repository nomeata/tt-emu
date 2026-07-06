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


@dataclass
class BootReport:
    """Structured result of a headless boot run."""

    result: RunResult
    checkpoints_hit: list[tuple[int, int, str]] = field(default_factory=list)
    timer_irqs: int = 0
    irqs_delivered: int = 0

    def format_log(self) -> str:
        lines = ["boot run log:"]
        for clock, pc, name in self.checkpoints_hit:
            lines.append(f"  [{clock:>10} insn] pc={pc:#010x}  {name}")
        lines.append(
            f"  stop: {self.result.reason} at pc={self.result.pc:#010x} "
            f"after ~{self.result.instructions} insn"
        )
        lines.append(f"  timer IRQs latched: {self.timer_irqs}, delivered: {self.irqs_delivered}")
        return "\n".join(lines)


def boot_firmware(
    path: str,
    *,
    max_instructions: int = 40_000_000,
    config: MachineConfig | None = None,
) -> BootReport:
    """Load ``path``, boot, and emulate up to ``max_instructions``; return a report."""
    firmware: Firmware = load_upd(path)
    booted: BootedMachine = build_machine(firmware, config)
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
    )
    return report
