"""1st-generation ZC3201 firmware (v0136 / 120117): recognition + live debug readers.

The ZC3201 twin of :mod:`tt_emu.firmware.mt`. Where MT exposes a QHsm *frame-byte
stack*, ZC3201's QF-style framework is observed through:

* the **event ring** the nandboot pump drains (`FUN 0x08003a84`): a 32-entry ring
  of 12-byte records at ``ring_base + 0``; ``head`` (u16) at ``ring_base + 0x180``,
  ``tail`` at ``+0x182``, both masked ``& 0x1f`` (``docs/zc3201-boot-feasibility.md``
  Legs 2/17). ``ring_base`` is the fixed HAL scheduler object ``0x080075cc``.
* the **statechart leaves** as *code addresses* — the state handlers are dispatched
  by ``sm_dispatch_hierarchy`` (``0x080096a8``) through the active object's
  ``obj+0xc`` slot, but the reliable, hook-free observable is the handler PC itself
  (INIT ``state_init_power_on`` ``0x08038e48``, the standby state-machine
  ``FUN_0803ef7c`` — the twin of MT's ``standby_handler`` per
  ``tt-firmware-reveng/correspondences.tsv``). :meth:`attach_watches` records the
  current leaf the same read-only way MT's ``attach_watches`` records "now playing".

All reads are pure RAM / read-only PC observation — the firmware runs unmodified.
On any other firmware :func:`recognize` fails and none of this is used.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ..machine import Machine

__all__ = [
    "FINGERPRINT",
    "STATE_HANDLERS",
    "Zc3201DebugSnapshot",
    "Zc3201Debugger",
    "event_name",
    "recognize",
    "state_name",
]

# --- Firmware recognition (matches firmware_profile.ZC3201.build_prefix) ------------------
FINGERPRINT = b"v0136\x00\x00\x00\x00\x00" + b"120117" + b"\x00\x00\x00\x00" + b"Tiptoi"


def recognize(prog: bytes) -> bool:
    """True iff ``prog`` is the v0136/120117 ZC3201 image this module describes."""
    return prog[: len(FINGERPRINT)] == FINGERPRINT


# --- Runtime addresses (all specific to this build; runtime base 0x08008000) --------------

#: The HAL scheduler object whose event ring the nandboot pump (0x08003a84) drains.
RING_BASE = 0x0800_75CC
RING_HEAD = RING_BASE + 0x180  # u16, & 0x1f
RING_TAIL = RING_BASE + 0x182  # u16, & 0x1f
RING_ENTRIES = 32
RING_STRIDE = 12  # {u16 event; u32 a; u32 b; ...}

#: The QF active-object pointer (RAM global) and its current-state handler slot.
AO_PTR_GLOBAL = 0x0800_9708
AO_STATE_HANDLER_OFF = 0xC

#: Statechart / pump landmark PCs.
PC_RING_DRAIN = 0x0800_3A84          # nandboot event-ring drain loop (the pump)
#: The QF event dispatch: the pump (``PC_RING_DRAIN``) drains one event from the
#: ring (base ``0x080075CC``, head ``0x0800774C`` / tail ``0x0800774E``, & 0x1f)
#: and calls this with ``r0`` = event id — it forwards to the QHsm handler chain
#: (``0x080036E0``) of the app's current state (scheduler ``0x080075A8``: current
#: state index ``[[+0x14]]`` into the handler table ``[+0x18]``). This — not
#: ``PC_VOICE_POLL`` — is where a tapped OID's event ``0x1063`` is delivered
#: (``docs/zc3201-boot-feasibility.md`` "Leg 20").
PC_EVENT_DISPATCH = 0x0800_37D8
#: The voice/media poll dispatcher the pump *also* calls every iteration
#: (``FUN_080096A8``): it polls the voice object ``0x08009708`` (``+8`` poll
#: method) and, on a nonzero result, calls its completion callback
#: (``+0xc`` = ``Fwl_pfVoice_fn`` ``0x0809EDA4``). It is the idle-loop voice
#: poll, **not** the statechart event dispatch (a Leg-17 misattribution
#: corrected in Leg 20); still a useful "pump alive" landmark.
PC_VOICE_POLL = 0x0800_96A8
PC_SM_DISPATCH = PC_VOICE_POLL        # back-compat alias (was mislabelled)
PC_TIMER_TICK = 0x0800_6D38          # HAL software-timer tick (per 20 ms IRQ)

#: OID sensor nandboot HAL (Leg 20). The two-wire bit-bang: clock GPIO7, data /
#: attention GPIO16, capture-state struct ``0x08007BF8`` (``+1`` = ``bit_count``,
#: ``0x17`` = 23 on the gameplay decode). ``hal_oid_timer_start`` arms the 40 ms
#: repeating poll (callback ``PC_OID_POLL_CB``), armed by the INIT leaf
#: (``0x08038EAC``) and the book-descent SM (``0x0803F13C``); the callback shifts
#: an armed frame in (``PC_OID_SHIFT_IN``), stores ``0x400000|oid`` to
#: ``[gb_app_context+0x40]+8``, and posts event ``0x1063``.
PC_OID_TIMER_START = 0x0800_5CF0
PC_OID_POLL_CB = 0x0800_5F48
PC_OID_DECODE = 0x0800_5EEC
PC_OID_SHIFT_IN = 0x0800_5D80
OID_CAPTURE_STATE = 0x0800_7BF8
OID_BIT_COUNT = 0x0800_7BF9
EVENT_OID_TAP = 0x1063

#: The statechart state handlers, by their entry PC (the leaf observable).
STATE_HANDLERS: dict[int, str] = {
    0x0803_8E48: "init_power_on",     # state_init_power_on (INIT leaf)
    0x0803_EF7C: "standby",           # standby state machine (MT standby_handler twin)
    0x0803_E454: "standby_setrefresh",  # SetRefresh helper in the standby file
    0x0809_EDA4: "voice_player",      # Fwl_pfVoice_fn — the power-on voice AO leaf
                                      # reached once standby descends past the GPIO
                                      # pin0 wait (docs Leg 18)
}

#: The QF event vocabulary observed on this build (partial; the shared 0x10xx band
#: is the same family as MT — see :mod:`tt_emu.firmware.mt`).
EVENT_NAMES: dict[int, str] = {
    0x0000: "null",
    0x0030: "sw-timer tick",
    0x1001: "init-after-transition",
    0x1015: "mode/launch",
    0x1063: "OID tap",
    0x1065: "resume-timer",
}


def state_name(handler_pc: int) -> str:
    return STATE_HANDLERS.get(handler_pc, f"handler_{handler_pc:#x}")


def event_name(event: int) -> str:
    if event in EVENT_NAMES:
        return f"{event:#x} {EVENT_NAMES[event]}"
    if 0x1015 <= event <= 0x1045:
        return f"{event:#x} game/mode launch"
    return f"{event:#x}"


@dataclass
class Zc3201DebugSnapshot:
    """One immutable-ish view of the ZC3201 framework state (RAM + PC observation)."""

    ready: bool = False               #: the AO pointer global is populated
    leaf: str = ""                    #: current statechart leaf (last handler entered)
    leaf_pc: int = 0
    ring_head: int = 0
    ring_tail: int = 0
    ring_pending: int = 0             #: events queued but not yet drained
    timer_ticks: int = 0             #: HAL software-timer ticks observed
    dispatches: int = 0              #: sm_dispatch_hierarchy calls observed
    recent_events: tuple[int, ...] = ()  #: events seen flowing through the ring


class Zc3201Debugger:
    """Hook-free live readers of the recognized ZC3201 firmware (one machine).

    Construct after :func:`recognize` passes. :meth:`attach_watches` adds the
    read-only PC observation points (the statechart-leaf handlers, the ring drain,
    the software-timer tick); :meth:`snapshot` reads the event ring straight from
    RAM. Nothing writes firmware state.
    """

    def __init__(
        self,
        machine: "Machine",
        *,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.machine = machine
        self._log = log or (lambda _msg: None)
        self.leaf_pc = 0
        self.timer_ticks = 0
        self.dispatches = 0
        self.leaves: list[tuple[int, int]] = []   # (clock, handler_pc)
        self._recent_events: list[int] = []

    # --- low-level safe reads -------------------------------------------------------------
    def _u16(self, addr: int) -> int:
        return struct.unpack("<H", self.machine.read_bytes(addr, 2))[0]

    def _u32(self, addr: int) -> int:
        return struct.unpack("<I", self.machine.read_bytes(addr, 4))[0]

    @property
    def ready(self) -> bool:
        try:
            ao = self._u32(AO_PTR_GLOBAL)
            return 0x0800_0000 <= ao < 0x0844_0000
        except Exception:  # noqa: BLE001
            return False

    # --- read-only PC watchpoints ---------------------------------------------------------
    def attach_watches(self) -> None:
        """Add the leaf-handler / pump / timer observation points (firmware unmodified)."""
        for pc in STATE_HANDLERS:
            self.machine.on_code(pc, self._make_leaf(pc))
        self.machine.on_code(PC_SM_DISPATCH, self._on_dispatch)
        self.machine.on_code(PC_TIMER_TICK, self._on_timer_tick)

    def _make_leaf(self, pc: int) -> Callable[["Machine"], None]:
        def cb(machine: "Machine") -> None:
            if pc != self.leaf_pc:
                self.leaf_pc = pc
                self.leaves.append((machine.clock, pc))
                self._log(f"zc3201: statechart leaf -> {state_name(pc)}")
        return cb

    def _on_dispatch(self, _machine: "Machine") -> None:
        self.dispatches += 1

    def _on_timer_tick(self, _machine: "Machine") -> None:
        self.timer_ticks += 1

    # --- event ring (pure RAM) ------------------------------------------------------------
    def ring_state(self) -> tuple[int, int]:
        """(head, tail) of the pump's event ring."""
        return self._u16(RING_HEAD) & 0x1F, self._u16(RING_TAIL) & 0x1F

    def ring_events(self) -> tuple[int, ...]:
        """The event ids currently queued in the ring (head..tail)."""
        head, tail = self.ring_state()
        out: list[int] = []
        i = head
        while i != tail and len(out) < RING_ENTRIES:
            out.append(self._u16(RING_BASE + i * RING_STRIDE))
            i = (i + 1) & 0x1F
        return tuple(out)

    # --- snapshot -------------------------------------------------------------------------
    def snapshot(self) -> Zc3201DebugSnapshot:
        try:
            head, tail = self.ring_state()
            pending = (tail - head) & 0x1F
        except Exception:  # noqa: BLE001
            head = tail = pending = 0
        return Zc3201DebugSnapshot(
            ready=self.ready,
            leaf=state_name(self.leaf_pc) if self.leaf_pc else "",
            leaf_pc=self.leaf_pc,
            ring_head=head,
            ring_tail=tail,
            ring_pending=pending,
            timer_ticks=self.timer_ticks,
            dispatches=self.dispatches,
            recent_events=tuple(self._recent_events[-16:]),
        )
