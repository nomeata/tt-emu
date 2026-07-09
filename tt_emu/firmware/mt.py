"""tiptoi 2N "MT" firmware (N0038MT / 20131009): recognition + live debug readers.

Implements ``docs/firmware-2n-mt.md``: positively recognize the one firmware
build these addresses belong to (§1), then expose **hook-free live readers** of
the QHsm statechart (§2), a transition detector (§3), and the GME interpreter
state (§4) — registers, mounted product, OID→script routing, playlist/media
state, the booklist. Everything is read-only polling of emulator RAM; the
firmware runs unmodified. Optionally, read-only PC *watchpoints* (§3/§4.4/§4.5
— observation of the program counter, no behavior change) add an exact
executed-action trace and a "now playing" line.

On any other firmware, :func:`recognize` fails and none of this is used — the
TUI then falls back to its generic panels.
"""

from __future__ import annotations

import struct
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterable

from unicorn.arm_const import (
    UC_ARM_REG_R0,
    UC_ARM_REG_R1,
    UC_ARM_REG_R2,
    UC_ARM_REG_R3,
)

from .symbols import GmeLine, GmeScripts, TttoolSymbols

if TYPE_CHECKING:
    from ..machine import Machine

__all__ = [
    "FINGERPRINT",
    "MtDebugSnapshot",
    "MtDebugger",
    "OidRouting",
    "STATE_NAMES",
    "event_name",
    "recognize",
    "state_name",
]

# --- §1 Firmware recognition ---------------------------------------------------------------

#: The 26 bytes at PROG offset 0 (load address 0x08009000): version id,
#: build date, product name. Byte-exact — any other build fails the gate.
FINGERPRINT = b"N0038MT\x00\x00\x00" + b"20131009" + b"\x00\x00" + b"Tiptoi"

FIRMWARE_LABEL = "N0038MT / 20131009 (2N 'MT', ZC3202N)"


def recognize(prog: bytes) -> bool:
    """True iff ``prog`` is the N0038MT/20131009 image this module describes."""
    return prog[: len(FINGERPRINT)] == FINGERPRINT


# --- §2.2 Runtime addresses (all specific to this build) ------------------------------------

AO_BASE = 0x08008874
AO_DEPTH = AO_BASE + 0x04  # u16 stack depth (0 = only the bottom frame)
AO_SP = AO_BASE + 0x14  # u32 -> base of the top frame; *(u8*)SP = leaf id
AO_DESC_PTR = AO_BASE + 0x18  # u32 == DESC_TABLE once app init has run (§1 gate)
AO_LAST_EVENT = AO_BASE + 0x24  # u32 last event id dispatched
DESC_TABLE = 0x08121D44

FRAME_BASE = 0x08007E80
FRAME_STRIDE = 0xC
MAX_DEPTH = 16  # sanity bound; the app never nests deeper

GAME_CTX = 0x080089A4
AKOID_PTR = GAME_CTX + 0x20  # u32 -> heap akoid_buf (0 until initialised)

# §4.2 mounted-GME state (fixed addresses)
PRODUCT_ID_ADDR = 0x081DA08C  # u32 current product id (0 = none)
GME_HANDLE_ADDR = 0x08121ED0  # i32 file handle of the mounted .gme (-1 = none)
GME_STATUS_ADDR = 0x081DA086  # u8 status bits (bit7 = GME mounted)
GME_TIMER_ADDR = 0x08121ECC  # u8 GME timer handle (0xFF = none armed)
XOR_ACTIVE_ADDR = 0x08008C00  # u8 1 = GME media being read (vs system voice)

# §4.3 the $-registers
REG_FILE_ADDR = 0x081DA350  # u16[]
REG_COUNT_ADDR = 0x081DA0A0  # u32 (from the GME register-init block)
MAX_REGISTERS = 256  # display sanity clamp

# §4.4 the most recently parsed script line (resident in RAM)
LINE_COUNT_ADDR = 0x081DA09C  # u32 line count of the routed script
ACTION_COUNT_ADDR = 0x081DA0A8  # u8 (clamped to 8 by the firmware)
ACTION_REG_ADDR = 0x081DA0EA  # u16[8]
ACTION_OP_ADDR = 0x081DA0FA  # u16[8]
ACTION_CONST_ADDR = 0x081DA10A  # u8[8]
ACTION_OPERAND_ADDR = 0x081DA112  # u16[8]
PLAYLIST_LEN_ADDR = 0x081DA122  # u16
PLAYLIST_ADDR = 0x081DA126  # u16[]
MAX_PLAYLIST = 32

# §4.6 the booklist iterator
BOOKLIST_PTR_ADDR = 0x081DA080  # u32 -> heap iterator (0 before standby entry)
BOOKLIST_PATH_OFF = 0x30  # wchar16[0x104] current record path

# §4.4 related counters
TICK_ADDR = 0x08008D24  # u32, ++ per 20 ms timer IRQ
HEARTBEAT_ADDR = 0x081DA014  # u32, ++ per OID-poll tick

#: Content OIDs are > 0x3E7; at or below is the product band (§4).
PRODUCT_OID_MAX = 0x3E7

#: Plausible heap-pointer window (the pen's RAM + headroom).
_PTR_MIN, _PTR_MAX = 0x08000000, 0x08440000

# akoid_buf field offsets (§4.1)
AKOID_LAST_OID = 0x04  # u16 last decoded OID
AKOID_FIRST_OID = 0x18  # u16 first content OID of the mounted GME
AKOID_LAST_CONTENT = 0x1A  # u16 last content OID
AKOID_FIRST_TAP = 0x74  # u16 OID latched at the book-opening tap
AKOID_PLAY_BUSY = 0x12A  # u8 playlist busy
AKOID_PLAYALL_MODE = 0x12C  # u8 play-all mode
AKOID_PLAYALL_CURSOR = 0x12D  # u8 play-all cursor
AKOID_JUMP_PENDING = 0xDD0  # u8 deferred Jump pending
AKOID_JUMP_TARGET = 0xDD2  # u16 deferred Jump target script line

# --- §3 / §4.4 / §4.5 read-only PC watchpoints ----------------------------------------------

PC_GME_EXEC_COMMAND = 0x08034DA0  # r0..r3 = register, opcode, is-const, operand
PC_PLAY_MEDIA = 0x080AB7B4  # GME media playback starts
PC_PLAY_VOICE = 0x080AB9AC  # r0 = system voice id (A:/VOIMG/Chomp_Voice.bin)

# --- §2.3 state id -> name -------------------------------------------------------------------

STATE_NAMES: dict[int, str] = {
    0: "root",
    1: "splash",
    2: "fw_update",
    3: "standby",
    4: "system_off",
    5: "usb_msc",
    6: "charging",
    7: "poweroff_prep",
    8: "usb_detect",
    9: "global_pre",
    10: "global_post",
    11: "orphan_overlay",
    12: "book_mount",
    13: "book",
    14: "st14_gme_script_game",
    15: "st15_study_step",
    16: "st16_study_list_a",
    17: "st17_study_list_b",
    18: "st18_study_return",
    19: "st19_study_mode_x",
    20: "st20_game_step_confirm",
    21: "st21_minigame2",
    22: "st22_minigame3_linecut",
    23: "st23_game4_quiz",
    24: "st24_game5",
    25: "st25_game7_findtarget",
    26: "st26_game_ask_question",
    27: "st27_minigame6",
    28: "st28_special_game1",
    29: "st29_special_game2",
    30: "game_hub",
    31: "st31_book_aux_mode",
    32: "discovery_mode_hub",
    **{32 + i: f"study_reading_game{i}" for i in range(1, 10)},  # 33..41
    **{42 + i: f"prod0b_game_mode{i}" for i in range(4)},  # 42..45
    **{46 + i: f"prod09_game_mode{i}" for i in range(4)},  # 46..49
    50: "selection_board_mode",
    **{51 + i: f"hub_subgame_{chr(ord('A') + i)}" for i in range(7)},  # 51..57
    58: "gme_gametype17",
    59: "gme_gametype18",
    60: "gme_gametype16_p2",
    61: "gme_gametype19",
    62: "gme_gametype20",
    63: "gme_gametype21",
    64: "game_reading_prod0e",
    65: "game_prod0f",
    66: "gme_gametype22",
    67: "gme_binary_subgame",
    68: "gme_gametype23",
    69: "gme_separate_binary",
}


def state_name(state: int) -> str:
    return STATE_NAMES.get(state, f"state{state}")


# --- §2.4 event vocabulary -------------------------------------------------------------------

EVENT_NAMES: dict[int, str] = {
    0x30: "sw-timer tick",
    0x1000: "init-after-transition",
    0x1009: "charging",
    0x100C: "back/exit",
    0x1014: "splash done",
    0x1046: "OID heartbeat",
    0x1047: "poweroff prep",
    0x1049: "back/exit",
    0x104A: "abort to standby",
    0x1058: "open product",
    0x1059: "mount done",
    0x105C: "USB msc",
    0x105D: "USB attach",
    0x105E: "USB detach",
    0x105F: "OID partial",
    0x1060: "OID decoded (tap)",
    0x1062: "refresh-or-off",
}


def event_name(event: int) -> str:
    if event in EVENT_NAMES:
        return f"{event:#x} {EVENT_NAMES[event]}"
    if 0x1015 <= event <= 0x1045:
        return f"{event:#x} game launch"
    return f"{event:#x}"


# --- §4.4 opcodes ------------------------------------------------------------------------------

#: Action opcodes (tttool names where they exist).
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
    0xFFA1: "DeferredPlay",
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

#: §4.5 useful system-voice ids (A:/VOIMG/Chomp_Voice.bin).
VOICE_NAMES: dict[int, str] = {
    0x13: "welcome jingle",
    0x14: "power-off jingle",
    0x17: "battery warning",
    0x1A: "battery final",
    0x2B: "invalid tap / nothing mounted",
    0x2D: "product not found (use tiptoi Manager)",
    **{0x09 + d: f"digit {d}" for d in range(10)},
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
class MtDebugSnapshot:
    """Immutable per-refresh view of the firmware's debug state (RAM reads only)."""

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

    @property
    def leaf(self) -> int:
        return self.chain[-1] if self.chain else 0

    @property
    def chain_names(self) -> tuple[str, ...]:
        return tuple(state_name(s) for s in self.chain)


@dataclass(frozen=True)
class Transition:
    """One observed statechart move (poll-detected, §3 pure-RAM method).

    ``last_oid`` is the classifier's last decoded tap at the time of the move —
    the most useful cause annotation available from RAM. (The AO's "last event"
    word at AO+0x24 reads as a constant 0x10 in emulation, so it is not used.)
    """

    kind: str  #: push / pop / sib / pop-all
    old_chain: tuple[int, ...]
    new_chain: tuple[int, ...]
    last_oid: int = 0

    def format(self) -> str:
        old = state_name(self.old_chain[-1]) if self.old_chain else "(pre-init)"
        new = state_name(self.new_chain[-1]) if self.new_chain else "?"
        depth = f"depth {max(len(self.old_chain) - 1, 0)}→{len(self.new_chain) - 1}"
        oid = f"  (last tap: OID {self.last_oid})" if self.last_oid else ""
        return f"{self.kind:7} {old} → {new}  [{depth}]{oid}"


# --- The debugger -----------------------------------------------------------------------------


class MtDebugger:
    """Hook-free live readers of the recognized firmware's RAM (one machine).

    Construct only after :func:`recognize` passed. All methods are safe to
    call at any time: before app init they report "not ready" / empty rather
    than raising; heap pointers are validated before dereferencing
    (``firmware-2n-mt.md`` §6 caveat). :meth:`attach_watches` optionally adds
    the documented read-only PC watchpoints (observation only — the firmware
    is never modified).
    """

    def __init__(
        self,
        machine: "Machine",
        *,
        gme_files: Iterable[bytes] = (),
        symbols: TttoolSymbols | None = None,
        log: Callable[[str], None] | None = None,
        read_mem: Callable[[int, int], bytes] | None = None,
    ) -> None:
        self.machine = machine
        # Firmware globals are read through the MMU: PROG runs under its real page table,
        # so the allocator maps many heap/work VAs to non-identity frames. A plain physical
        # read of such a VA returns garbage. ``read_mem`` (``MmuBoot.read_va``) translates;
        # it defaults to a physical read for the flat/no-MMU case.
        self._read_mem: Callable[[int, int], bytes] = read_mem or machine.read_bytes
        self.symbols = symbols
        self._log = log or (lambda message: None)
        self._scripts: list[GmeScripts] = []
        for data in gme_files:
            try:
                self._scripts.append(GmeScripts(data))
            except (struct.error, ValueError, IndexError):
                pass
        self._last_chain: tuple[int, ...] | None = None
        self._match_cache: dict[tuple[int, int, bytes, tuple[int, ...]], int] = {}
        self._recent_actions: deque[str] = deque(maxlen=12)
        self.last_play = ""
        self.media_plays = 0
        self.voice_plays = 0

    # --- low-level safe reads -------------------------------------------------------------

    def _u8(self, addr: int) -> int:
        return self._read_mem(addr, 1)[0]

    def _u16(self, addr: int) -> int:
        return struct.unpack("<H", self._read_mem(addr, 2))[0]

    def _u32(self, addr: int) -> int:
        return struct.unpack("<I", self._read_mem(addr, 4))[0]

    def _ptr(self, addr: int) -> int | None:
        """A heap pointer, validated to the RAM window; None if unset/implausible."""
        value = self._u32(addr)
        return value if _PTR_MIN <= value < _PTR_MAX else None

    # --- §1 runtime gate --------------------------------------------------------------------

    @property
    def ready(self) -> bool:
        """App init has run: the AO's descriptor-table pointer is populated."""
        try:
            return self._u32(AO_DESC_PTR) == DESC_TABLE
        except Exception:  # noqa: BLE001 — a torn read means "not yet"
            return False

    # --- §2.2 statechart --------------------------------------------------------------------

    def state_chain(self) -> tuple[int, ...] | None:
        """The live parent chain, bottom -> leaf; None mid-transition/pre-init.

        depth u16 @AO+0x04; frame *i* state byte @FRAME_BASE + i*0xC; sanity
        check ``SP == FRAME_BASE + depth*0xC`` (§2.2 "skip this refresh").
        """
        if not self.ready:
            return None
        depth = self._u16(AO_DEPTH)
        if depth >= MAX_DEPTH:
            return None
        if self._u32(AO_SP) != FRAME_BASE + depth * FRAME_STRIDE:
            return None
        return tuple(
            self._u8(FRAME_BASE + i * FRAME_STRIDE) for i in range(depth + 1)
        )

    def last_event(self) -> int:
        """The AO+0x24 word. Documented as "last event id dispatched", but it
        reads as a constant 0x10 in emulation — kept readable for diagnosis,
        not used for display."""
        return self._u32(AO_LAST_EVENT)

    # --- §3 transition detection (pure-RAM poll) ----------------------------------------------

    def poll_transition(self) -> Transition | None:
        """Detect a statechart move since the last poll; None if unchanged.

        Poll-based (§3 pure-RAM method): call once per run-loop chunk. May
        miss sub-chunk transients (e.g. book_mount(12) is sub-millisecond).
        """
        try:
            chain = self.state_chain()
        except Exception:  # noqa: BLE001 — never disturb the run loop
            return None
        if chain is None:
            return None
        previous, self._last_chain = self._last_chain, chain
        if previous is None or chain == previous:
            return None
        if len(chain) > len(previous):
            kind = "push"
        elif len(chain) == len(previous):
            kind = "sib"
        elif len(chain) == 1 and len(previous) > 2:
            kind = "pop-all"
        else:
            kind = "pop"
        last_oid = 0
        try:
            akoid = self._ptr(AKOID_PTR)
            if akoid is not None:
                last_oid = self._u16(akoid + AKOID_LAST_OID)
        except Exception:  # noqa: BLE001
            pass
        return Transition(kind=kind, old_chain=previous, new_chain=chain, last_oid=last_oid)

    # --- §4.3 registers -------------------------------------------------------------------------

    def registers(self) -> tuple[int, ...]:
        """The GME ``$``-register file (u16 each); empty before a mount."""
        count = self._u32(REG_COUNT_ADDR)
        if not 0 < count <= MAX_REGISTERS:
            return ()
        raw = self._read_mem(REG_FILE_ADDR, 2 * count)
        return struct.unpack(f"<{count}H", raw)

    # --- §4.6 booklist ---------------------------------------------------------------------------

    def booklist(self) -> tuple[int, str]:
        """(number of .gme files found, current/mounted record path)."""
        it = self._ptr(BOOKLIST_PTR_ADDR)
        if it is None:
            return 0, ""
        count = self._u16(it)
        raw = self._read_mem(it + BOOKLIST_PATH_OFF, 2 * 0x104)
        path = raw.decode("utf-16-le", "replace").split("\x00", 1)[0]
        return count, path

    # --- §4.4 OID -> script routing -----------------------------------------------------------------

    def _resident_line(self) -> tuple[tuple[tuple[int, int, int, int], ...], tuple[int, ...]]:
        """The RAM-resident parsed line: (actions, playlist) as stored (§4.4)."""
        count = min(self._u8(ACTION_COUNT_ADDR), 8)
        regs = struct.unpack("<8H", self._read_mem(ACTION_REG_ADDR, 16))
        ops = struct.unpack("<8H", self._read_mem(ACTION_OP_ADDR, 16))
        consts = self._read_mem(ACTION_CONST_ADDR, 8)
        operands = struct.unpack("<8H", self._read_mem(ACTION_OPERAND_ADDR, 16))
        actions = tuple(
            (regs[i], ops[i], consts[i], operands[i]) for i in range(count)
        )
        length = min(self._u16(PLAYLIST_LEN_ADDR), MAX_PLAYLIST)
        playlist = struct.unpack(
            f"<{length}H", self._read_mem(PLAYLIST_ADDR, 2 * length)
        )
        return actions, playlist

    def _scripts_for(self, product: int) -> GmeScripts | None:
        for scripts in self._scripts:
            if scripts.product == product:
                return scripts
        return None

    def _routing(
        self, akoid: int, product: int, registers: tuple[int, ...]
    ) -> OidRouting | None:
        """Render "tap OID → script line" from RAM + the GME container + symbols."""
        oid = self._u16(akoid + AKOID_LAST_OID)
        if oid == 0:
            return None
        symbols = self.symbols
        label = (symbols.oid_label(oid) or "") if symbols else ""
        if oid <= PRODUCT_OID_MAX:
            return OidRouting(oid=oid, kind="product-band", label=label)
        first = self._u16(akoid + AKOID_FIRST_OID)
        last = self._u16(akoid + AKOID_LAST_CONTENT)
        if first and not first <= oid <= last:
            return OidRouting(oid=oid, kind="out-of-range", label=label)

        actions, playlist = self._resident_line()
        line_count = self._u32(LINE_COUNT_ADDR)
        scripts = self._scripts_for(product)
        lines = scripts.script(oid) if scripts else None
        matched = -1
        if lines is not None:
            if not lines:
                return OidRouting(oid=oid, kind="no-script", label=label)
            matched = self._match_line(product, oid, lines, actions, playlist)
            line_count = len(lines)

        matched_bin: GmeLine | None = (
            lines[matched] if lines is not None and matched >= 0 else None
        )
        conditions = tuple(
            render_condition(c, symbols, registers)
            for c in (matched_bin.conds if matched_bin else ())
        )
        action_texts = tuple(
            render_action(reg, op, const, operand, symbols, playlist)
            for reg, op, const, operand in actions
        )
        playlist_texts = tuple(
            f"{index}:{symbols.media_name(index)}" if symbols else str(index)
            for index in playlist
        )
        source = symbols.script_source(oid, matched) if symbols and matched >= 0 else None
        return OidRouting(
            oid=oid,
            label=label,
            kind="content",
            line_count=line_count,
            matched_line=matched,
            conditions=conditions,
            actions=action_texts,
            playlist=playlist_texts,
            source=source or "",
        )

    def _match_line(
        self,
        product: int,
        oid: int,
        lines: tuple[GmeLine, ...],
        actions: tuple[tuple[int, int, int, int], ...],
        playlist: tuple[int, ...],
    ) -> int:
        """Which script line the resident parsed (actions, playlist) is.

        The executing line index lives only in interpreter locals (§4.4); the
        resident decode of the *most recently parsed* line is matched against
        the container's lines instead. Lines are evaluated in order and
        parsing stops at the first line whose conditions hold, so the match
        is the executed line whenever one executed.
        """
        key = (product, oid, struct.pack(f"<{4 * len(actions)}H", *sum(actions, ())), playlist)
        if key not in self._match_cache:
            matched = -1
            for i, line in enumerate(lines):
                if line.actions == actions and line.playlist == playlist:
                    matched = i
                    break
            self._match_cache[key] = matched
        return self._match_cache[key]

    # --- the full snapshot --------------------------------------------------------------------------

    def snapshot(self) -> MtDebugSnapshot:
        """One immutable debug view; all-RAM, never raises (empty on any tear)."""
        try:
            return self._snapshot()
        except Exception:  # noqa: BLE001 — a torn read yields an empty frame
            return MtDebugSnapshot()

    def _snapshot(self) -> MtDebugSnapshot:
        chain = self.state_chain()
        if chain is None:
            chain = self._last_chain
        if chain is None:
            return MtDebugSnapshot(ready=self.ready)
        registers = self.registers()
        product = self._u32(PRODUCT_ID_ADDR)
        handle = self._u32(GME_HANDLE_ADDR)
        handle = handle - 0x100000000 if handle >= 0x80000000 else handle
        status = self._u8(GME_STATUS_ADDR)
        book_count, gme_path = self.booklist()
        timer = self._u8(GME_TIMER_ADDR)
        symbols = self.symbols
        product_label = (
            symbols.comment
            if symbols and symbols.product_id == product and symbols.comment
            else ""
        )
        register_names = tuple(
            (symbols.register_name(i) if symbols else f"${i}")
            for i in range(len(registers))
        )

        last_oid = first_tap = 0
        routing = None
        play_busy = False
        playall_mode = playall_cursor = 0
        deferred: int | None = None
        deferred_label = ""
        akoid = self._ptr(AKOID_PTR)
        if akoid is not None:
            last_oid = self._u16(akoid + AKOID_LAST_OID)
            first_tap = self._u16(akoid + AKOID_FIRST_TAP)
            routing = self._routing(akoid, product, registers)
            play_busy = bool(self._u8(akoid + AKOID_PLAY_BUSY))
            playall_mode = self._u8(akoid + AKOID_PLAYALL_MODE)
            playall_cursor = self._u8(akoid + AKOID_PLAYALL_CURSOR)
            if self._u8(akoid + AKOID_JUMP_PENDING):
                deferred = self._u16(akoid + AKOID_JUMP_TARGET)
                if symbols is not None:
                    deferred_label = symbols.oid_label(deferred) or ""
        oid_first = self._u16(akoid + AKOID_FIRST_OID) if akoid is not None else 0
        oid_last = self._u16(akoid + AKOID_LAST_CONTENT) if akoid is not None else 0

        return MtDebugSnapshot(
            ready=True,
            chain=chain,
            last_event=self.last_event(),
            registers=registers,
            register_names=register_names,
            product=product,
            product_label=product_label,
            gme_handle=handle,
            gme_mounted=bool(status & 0x80),
            gme_path=gme_path,
            book_count=book_count,
            oid_first=oid_first,
            oid_last=oid_last,
            last_oid=last_oid,
            first_tap_oid=first_tap,
            routing=routing,
            play_busy=play_busy,
            playall_mode=playall_mode,
            playall_cursor=playall_cursor,
            xor_active=bool(self._u8(XOR_ACTIVE_ADDR)),
            deferred_jump=deferred,
            deferred_jump_label=deferred_label,
            timer_slot=None if timer == 0xFF else timer,
            tick=self._u32(TICK_ADDR),
            heartbeat=self._u32(HEARTBEAT_ADDR),
            last_play=self.last_play,
            recent_actions=tuple(self._recent_actions),
        )

    # --- optional read-only PC watchpoints (§3/§4.4/§4.5) ----------------------------------------------

    def attach_watches(self) -> None:
        """Add the documented observation watchpoints (firmware unmodified).

        * ``gme_exec_command`` — one hit per executed script action (r0..r3 =
          register, opcode, is-const, operand): the exact action trace;
        * ``play_media`` / ``fwl_play_voice_by_id`` — the "now playing" line.
        """
        self.machine.on_code(PC_GME_EXEC_COMMAND, self._on_exec_command)
        self.machine.on_code(PC_PLAY_MEDIA, self._on_play_media)
        self.machine.on_code(PC_PLAY_VOICE, self._on_play_voice)

    def _on_exec_command(self, machine: "Machine") -> None:
        uc = machine.uc
        reg = uc.reg_read(UC_ARM_REG_R0)
        opcode = uc.reg_read(UC_ARM_REG_R1)
        is_const = uc.reg_read(UC_ARM_REG_R2)
        operand = uc.reg_read(UC_ARM_REG_R3)
        try:
            _, playlist = self._resident_line()
        except Exception:  # noqa: BLE001
            playlist = ()
        text = render_action(reg, opcode, is_const, operand, self.symbols, playlist)
        self._recent_actions.append(text)
        self._log(f"gme: exec {text}")

    def _on_play_media(self, machine: "Machine") -> None:
        self.media_plays += 1
        self.last_play = f"GME media (play_media #{self.media_plays})"
        self._log(f"gme: {self.last_play}")

    def _on_play_voice(self, machine: "Machine") -> None:
        self.voice_plays += 1
        voice = machine.uc.reg_read(UC_ARM_REG_R0)
        label = VOICE_NAMES.get(voice, f"id {voice:#x}")
        self.last_play = f"system voice {label}"
        self._log(f"voice: {label} ({voice:#x})")
