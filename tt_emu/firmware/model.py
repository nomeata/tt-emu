"""Firmware-neutral debug model: the shared snapshot/transition types, the GME
interpreter's symbolic renderers, and the :class:`FirmwareDebugger` contract.

The GME interpreter is a *shared twin* across pen generations (the 2N "MT" and the
1st-gen ZC3201 run the same bytecode over the same RAM layout, shifted by a uniform
offset), so the debugger *view* is generation-neutral even though each generation's
live readers live in its own module (:mod:`tt_emu.firmware.mt` /
:mod:`tt_emu.firmware.zc3201`). Both produce a :class:`DebugSnapshot` and a
:class:`Transition` and satisfy :class:`FirmwareDebugger`; the TUI/scripting consumers
render them without knowing which firmware produced them. This module owns the pieces
neither generation should own — so ``zc3201`` need not import from ``mt``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .symbols import TttoolSymbols

__all__ = [
    "ACTION_NAMES",
    "COND_NAMES",
    "DebugSnapshot",
    "FirmwareDebugger",
    "OidRouting",
    "PRODUCT_OID_MAX",
    "Transition",
    "render_action",
    "render_condition",
]

#: Highest OID in the reserved product band (codes <= this are product-select taps,
#: not content). The GME interpreter routes them to the booklist, not a script.
PRODUCT_OID_MAX = 0x3E7

#: Action opcodes (tttool names where they exist). The scripted-GME interpreter is
#: identical across generations, so its opcode vocabulary lives here, not per-firmware.
ACTION_NAMES: dict[int, str] = {
    0xFFF9: "Set",
    0xFFF0: "Inc",
    0xFFF1: "Dec",
    0xFFF2: "Mult",
    0xFFF3: "Div",
    0xFFF4: "Mod",
    0xFFF5: "And",
    0xFFF6: "Or",
    0xFFF7: "XOr",
    0xFFF8: "Neg",
    0xFFE8: "Play",
    0xFFE0: "RandomVariant",
    0xFFE1: "PlayAllVariant",
    0xFB00: "PlayAll",
    0xFC00: "Random",
    0xFF00: "RandomToReg",
    0xFE00: "Timer",
    0xFEFF: "CancelTimer",
    0xF8FF: "Jump",
    0xFD00: "Game",
    0xFAFF: "Cancel",
    0xFFA1: "CoinFlipPlay",
}

#: Condition opcodes (pure u16 compares).
COND_NAMES: dict[int, str] = {
    0xFFF9: "==",
    0xFFFA: ">",
    0xFFFB: "<",
    0xFFFC: "==",
    0xFFFD: ">=",
    0xFFFE: "<=",
    0xFFFF: "!=",
}


# --- Symbolic rendering ------------------------------------------------------------------------


def _reg(index: int, symbols: TttoolSymbols | None) -> str:
    return symbols.register_name(index) if symbols else f"${index}"


def render_action(
    reg: int,
    opcode: int,
    is_const: int,
    operand: int,
    symbols: TttoolSymbols | None = None,
    playlist: tuple[int, ...] = (),
) -> str:
    """One parsed/executed action as debugger text, symbolic where possible."""
    name = ACTION_NAMES.get(opcode)
    operand_text = f"{operand}" if is_const else _reg(operand, symbols)
    if name in ("Set", "Inc", "Dec", "Mult", "Div", "Mod", "And", "Or", "XOr"):
        return f"{name}({_reg(reg, symbols)},{operand_text})"
    if name == "Neg":
        return f"Neg({_reg(reg, symbols)})"
    if name == "RandomToReg":
        return f"RandomToReg({_reg(reg, symbols)},{operand_text})"
    if name in ("Play", "RandomVariant"):
        if name == "Play" and 0 <= operand < len(playlist):
            media = playlist[operand]
            label = symbols.media_name(media) if symbols else f"media {media}"
            return f"Play({operand} → {label})"
        return f"{name}({operand_text})"
    if name == "Jump":
        target = symbols.oid_label(operand) if symbols else None
        return f"Jump({target or operand})"
    if name is not None:
        return f"{name}({operand_text})"
    if 0xFEE0 <= opcode <= 0xFEE7:
        return f"SoundProfile({opcode - 0xFEE0})"
    return f"op{opcode:04x}({_reg(reg, symbols)},{operand_text})"


def render_condition(
    cond: tuple[int, int, int, int, int],
    symbols: TttoolSymbols | None = None,
    registers: tuple[int, ...] = (),
) -> str:
    """One GME-binary condition, with live register values substituted."""
    lhs_const, lhs, op, rhs_const, rhs = cond

    def side(is_const: int, value: int) -> str:
        if is_const:
            return str(value)
        live = f"={registers[value]}" if 0 <= value < len(registers) else ""
        return f"{_reg(value, symbols)}{live}"

    return f"{side(lhs_const, lhs)} {COND_NAMES.get(op, f'op{op:04x}')} {side(rhs_const, rhs)}"


# --- Snapshot dataclasses -------------------------------------------------------------------


@dataclass(frozen=True)
class OidRouting:
    """Where the last decoded tap routed (§4.4), rendered for display."""

    oid: int
    label: str = ""  #: tttool script name, "" if unknown
    kind: str = "content"  #: content / product-band / out-of-range / no-script
    line_count: int = 0  #: lines in the routed script
    matched_line: int = -1  #: index of the resident parsed line, -1 unknown
    conditions: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    playlist: tuple[str, ...] = ()
    source: str = ""  #: YAML source text of the matched line


@dataclass(frozen=True)
class DebugSnapshot:
    """Immutable per-refresh view of the firmware's debug state (RAM reads only).

    Generation-neutral: both the MT and ZC3201 live readers fill this same shape, so
    the panels render without knowing which firmware produced it. ``chain``/
    ``chain_names`` are the statechart parent chain (MT's nested QHsm hierarchy, or the
    ZC3201's single flat handler-dispatch leaf); the producing debugger fills the names
    because each generation names its states differently.
    """

    ready: bool = False  #: §1 init-done gate passed; fields below are valid
    chain: tuple[int, ...] = ()  #: statechart parent chain, bottom -> leaf
    last_event: int = 0
    registers: tuple[int, ...] = ()
    register_names: tuple[str, ...] = ()
    product: int = 0
    product_label: str = ""
    gme_handle: int = -1
    gme_mounted: bool = False
    gme_path: str = ""
    book_count: int = 0
    oid_first: int = 0
    oid_last: int = 0
    last_oid: int = 0
    first_tap_oid: int = 0
    routing: OidRouting | None = None
    play_busy: bool = False
    playall_mode: int = 0
    playall_cursor: int = 0
    xor_active: bool = False
    deferred_jump: int | None = None  #: pending Jump target (the OID code, observed)
    deferred_jump_label: str = ""  #: symbolic name of the jump target, if known
    timer_slot: int | None = None  #: armed GME timer handle
    tick: int = 0
    heartbeat: int = 0
    last_play: str = ""  #: from the optional PC watches ("now playing")
    recent_actions: tuple[str, ...] = ()  #: executed-action trace (PC watch)
    #: Human names of ``chain``, filled by the producing debugger (each firmware has
    #: its own state names), so a snapshot renders without the renderer knowing the
    #: firmware. Empty when ``chain`` is empty.
    chain_names: tuple[str, ...] = ()

    @property
    def leaf(self) -> int:
        return self.chain[-1] if self.chain else 0

    @property
    def leaf_name(self) -> str:
        return self.chain_names[-1] if self.chain_names else ""


@dataclass(frozen=True)
class Transition:
    """One observed statechart move (poll-detected, §3 pure-RAM method).

    ``last_oid`` is the classifier's last decoded tap at the time of the move — the most
    useful cause annotation available from RAM. The producing debugger fills ``text``
    with the firmware-specific rendering (MT names its QHsm chain; ZC3201 names its flat
    leaf), so :meth:`format` needs no per-firmware state-name table; the numeric fallback
    is only used if a producer leaves ``text`` empty.
    """

    kind: str  #: push / pop / sib / pop-all
    old_chain: tuple[int, ...]
    new_chain: tuple[int, ...]
    last_oid: int = 0
    text: str = ""  #: pre-rendered line supplied by the producing debugger

    def format(self) -> str:
        if self.text:
            return self.text
        old = self.old_chain[-1] if self.old_chain else 0
        new = self.new_chain[-1] if self.new_chain else 0
        depth = f"depth {max(len(self.old_chain) - 1, 0)}→{len(self.new_chain) - 1}"
        oid = f"  (last tap: OID {self.last_oid})" if self.last_oid else ""
        return f"{self.kind:7} {old} → {new}  [{depth}]{oid}"


# --- The debugger contract ---------------------------------------------------------------------


@runtime_checkable
class FirmwareDebugger(Protocol):
    """The hook-free live-reader surface every recognized firmware exposes.

    Both :class:`tt_emu.firmware.mt.MtDebugger` and
    :class:`tt_emu.firmware.zc3201.Zc3201Debugger` satisfy this; the TUI and scripting
    consumers depend on the Protocol, not the concrete classes. Everything is read-only
    polling of emulator RAM (plus documented read-only PC watchpoints).
    """

    def attach_watches(self) -> None:
        """Install the read-only PC watchpoints (executed-action trace, now-playing)."""
        ...

    def snapshot(self) -> DebugSnapshot:
        """One immutable, all-RAM debug view; never raises (empty on a torn read)."""
        ...

    def poll_transition(self) -> Transition | None:
        """A statechart move since the last poll, or ``None`` if unchanged."""
        ...
