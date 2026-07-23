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
from collections import deque
from typing import TYPE_CHECKING, Callable

from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3

from ..firmware_profile import ZC3201 as _PROFILE
from .mt import (
    MtDebugSnapshot,
    OidRouting,
    PRODUCT_OID_MAX,
    Transition,
    render_action,
    render_condition,
)
from .symbols import GmeScripts, TttoolSymbols

#: The reverse-engineered symbol table for this build is the single source of truth
#: for firmware addresses (``firmware_profile.ZC3201.symbols``); the watchpoint and
#: handle constants below are *derived* from it rather than re-literalled, so a
#: lift/rebase correction in one place can't drift out of sync (that is exactly how
#: ``play_chomp_voice`` once ended up 0x8000 low). Addresses with no matching symbol,
#: or whose symbol name is a known-stale label (see ``PC_VOICE_POLL``), stay literal.
_SYM = _PROFILE.symbols

if TYPE_CHECKING:
    from ..machine import Machine

__all__ = [
    "CURRENT_GME_HANDLE",
    "FINGERPRINT",
    "PC_VOICE_PLAY_SAMPLE",
    "STATE_HANDLERS",
    "Zc3201DacPageMap",
    "Zc3201Debugger",
    "event_name",
    "recognize",
    "state_name",
]

# --- Firmware recognition (matches firmware_profile.ZC3201.build_prefix) ------------------
FINGERPRINT = b"v0136\x00\x00\x00\x00\x00" + b"120117" + b"\x00\x00\x00\x00" + b"Tiptoi"
#: Short label for the recognized image (the debugger-panel banner).
FIRMWARE_LABEL = "v0136 / 120117 (1st-gen ZC3201)"


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

#: The GME play observable (``docs/zc3201-boot-feasibility.md`` "Leg 22"):
#: ``voice_play_sample`` is entered with ``r1 = media file-offset`` and
#: ``r2 = media byte-size`` — exactly the ``(offset, size)`` key the game's own
#: media table stores, so a play resolves to a media index by that key (the same
#: observable the ``firmware-re`` lab ``zc3201_emu.py`` validates against, and the
#: twin of MT's ``PC_PLAY_MEDIA``).
PC_VOICE_PLAY_SAMPLE = _SYM["voice_play_sample"]  # 0x0809F068

#: ``p_filehandle_current_gme``: the file handle of the currently-mounted ``.gme``,
#: written by ``gme_mount_check_product`` (``0x080297dc``) when a product-OID tap
#: matches a discovered game. It changes from its book-idle value once a game is
#: mounted, so a change is the hook-free "a game got mounted" signal (Leg 22).
CURRENT_GME_HANDLE = _SYM["gme_file_handle_ptr"]  # 0x080D20A0

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

#: The power-on voice AO leaf reached once the standby SM descends past the GPIO
#: pin0 wait (docs Leg 18): the **stable book-idle leaf** — entered ~22 M insn into
#: boot and held from then on, so "the pump has reached this leaf" is the hook-free
#: "book descent complete / ready for the first tap" signal.
BOOK_IDLE_LEAF_PC = 0x0809_EDA4

#: The statechart state handlers, by their entry PC (the leaf observable).
STATE_HANDLERS: dict[int, str] = {
    0x0803_8E48: "init_power_on",     # state_init_power_on (INIT leaf)
    0x0803_EF7C: "standby",           # standby state machine (MT standby_handler twin)
    0x0803_E454: "standby_setrefresh",  # SetRefresh helper in the standby file
    BOOK_IDLE_LEAF_PC: "voice_player",  # Fwl_pfVoice_fn — the power-on voice AO leaf
                                      # reached once standby descends past the GPIO
                                      # pin0 wait (docs Leg 18); the book-idle leaf
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


#: The virtual→physical page table the 1st-gen nandboot MMU installs. Base
#: ``0x08004400`` is a literal in ``virt_to_phys`` (``0x0800203c``): 512 PTEs cover
#: the 2 MiB virtual window ``[0x08000000, 0x08200000)``; a PTE is ``phys_frame |
#: flags``, valid iff ``PTE & 0xff0``, and ``virt_to_phys(v) = (PTE[v]&~0xfff) |
#: (v&0xfff)`` (proven at runtime — it reproduces every observed DAC mapping).
DAC_PT_BASE = 0x0800_4400
DAC_VBASE = 0x0800_0000
DAC_VSPAN = 0x0020_0000
DAC_PT_ENTRIES = DAC_VSPAN >> 12


class Zc3201DacPageMap:
    """Physical→flat resolver for the DAC bus master on the flat-loaded ZC3201.

    tt-emu runs the ZC3201 firmware **flat** (virtual == flat address; no MMU
    peripheral, unlike MT's :class:`~tt_emu.mmu_boot.MmuBoot`). That is faithful
    for code and almost all data, because the firmware's own page table is
    identity for them — but **not** for the audio DMA buffers, which use a
    *non-identity* mapping: the software codec decodes S16LE PCM into a **virtual**
    buffer (e.g. ``0x080fe800``) that the page table maps to **physical** low RAM
    (e.g. ``0x08008800``). The DAC engine is a *physical* bus master, so its source
    register carries the physical address; but in this flat emulator the decoded
    bytes live at the *virtual* (== flat) address, while the physical address
    aliases unrelated PROG memory. So to read what the DAC truly streams, invert
    the firmware's page table (phys frame → virtual page) and read flat memory
    there. Purely read-only: the firmware runs unmodified; this only consults its
    installed page table and RAM. A small ``phys_frame → virt_page`` cache keeps it
    to one page-table probe per submit once a buffer is known.
    """

    def __init__(self, machine: "Machine", *, pt_base: int = DAC_PT_BASE) -> None:
        self.machine = machine
        self._pt_base = pt_base
        self._cache: dict[int, int] = {}

    def resolve(self, phys: int, length: int) -> bytes | None:
        """The ``length`` bytes the DAC streams from physical ``phys`` (or ``None``
        if no valid page maps there). Reads at the *virtual* alias — contiguous in
        flat memory, so a chunk that runs to a page edge is still read correctly."""
        frame = phys & 0xFFFF_F000
        virt_page = self._cache.get(frame)
        if virt_page is None or not self._maps_to(virt_page, frame):
            virt_page = self._scan(frame)
            if virt_page is None:
                return None
            self._cache[frame] = virt_page
        return self.machine.read_bytes(virt_page | (phys & 0xFFF), length)

    def _maps_to(self, virt_page: int, frame: int) -> bool:
        idx = (virt_page - DAC_VBASE) >> 12
        pte = self._u32(self._pt_base + idx * 4)
        return bool(pte & 0xFF0) and (pte & 0xFFFF_F000) == frame

    def _scan(self, frame: int) -> int | None:
        for idx in range(DAC_PT_ENTRIES):
            pte = self._u32(self._pt_base + idx * 4)
            if pte & 0xFF0 and (pte & 0xFFFF_F000) == frame:
                return DAC_VBASE + (idx << 12)
        return None

    def _u32(self, addr: int) -> int:
        return struct.unpack("<I", self.machine.read_bytes(addr, 4))[0]


# --- GME-interpreter RAM addresses (verified live against a content tap; the whole
# 2N GME state block is a uniform -0x2028 shift of the MT block — see firmware-2n-mt §4
# and the MT addresses in :mod:`tt_emu.firmware.mt`). The register file, the register
# count, the GME context, and the akoid low fields are all solid; the resident parsed-
# script-line arrays proved unreliable (cleared between parse and exec), so the OID→script
# panel renders from the .gme container keyed on the reliable akoid ``last_oid`` instead.
REG_FILE_ADDR = 0x081D_8328    # u16[] $-register file
REG_COUNT_ADDR = 0x081D_8078   # u32 (low 16 = count)
PRODUCT_ID_ADDR = 0x081D_8064  # u32 mounted product id
GME_STATUS_ADDR = 0x081D_805F  # u8, bit7 = GME mounted, bit6 = scanning
TICK_ADDR = 0x0800_7DEC        # u32 monotonic tick (20 ms timer)
XOR_ACTIVE_ADDR = 0x0800_7BE8  # u8, 1 = GME media (vs system voice)

#: akoid_buf (OID-routing struct): base = ``*(gb_app_context + 0x40)``. The low fields
#: match MT; the play/jump fields sit at ZC-specific offsets (mid-struct layout differs).
AKOID_PTR_ADDR = _SYM["gb_app_context"] + 0x40  # 0x080077DC
AKOID_LAST_OID = 0x04               # u32 (OID fits 16 bits)
AKOID_LAST_CONTENT = 0x1A           # u16
AKOID_OID_MIN = 0x1C                # u16 mounted GME's first content OID
AKOID_OID_MAX = 0x1E                # u16 last content OID
AKOID_PLAY_BUSY = 0xD8              # u8
AKOID_PLAYALL_MODE = 0xDC           # u8
AKOID_PLAYALL_CURSOR = 0xDD         # u8
AKOID_JUMP_PENDING = 0xD80          # u8
AKOID_JUMP_TARGET = 0xD82           # u16

#: Interpreter watchpoints (twins of MT's): the executed-action trace and "now playing".
PC_GME_EXEC_COMMAND = _SYM["gme_exec_command"]   # 0x0804C6E4; r0..r3 = reg, opcode,
                                                 # is-const, operand
PC_PLAY_CHOMP_VOICE = _SYM["play_chomp_voice"]   # 0x0809F374; system-voice play
                                                 # (Chomp_Voice.bin)
_OPCODE_PLAY = 0xFFE8               # GME "Play n" action opcode
MAX_REGISTERS = 256                 # display clamp


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
        gme_files: list[bytes] | None = None,
        symbols: TttoolSymbols | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.machine = machine
        self.symbols = symbols
        self._log = log or (lambda _msg: None)
        self.leaf_pc = 0
        self.timer_ticks = 0
        self.dispatches = 0
        self.leaves: list[tuple[int, int]] = []   # (clock, handler_pc)
        self._reported_leaves = 0                 # poll_transition cursor into leaves
        self._recent_events: list[int] = []
        self._recent_actions: deque[str] = deque(maxlen=12)  # executed-action trace
        self.last_play = ""                       # "now playing" from the play watches
        #: The mounted game(s), for the container-driven OID→script routing.
        self._scripts: list[GmeScripts] = []
        for data in gme_files or ():
            try:
                self._scripts.append(GmeScripts(data))
            except (ValueError, struct.error, IndexError):
                continue

    # --- low-level safe reads (MMU-aware) -------------------------------------------------
    def _read(self, addr: int, size: int) -> bytes:
        """Read guest memory, translating VA→physical through the firmware's MMU.

        The interpreter globals (0x081Dxxxx) live in the demand-paged region where VA≠PA,
        so a raw physical ``read_bytes`` would read the wrong bytes; go through the MMU's
        ``read_va`` when it is enabled (identity for the resident low region, so the ring /
        AO reads are unaffected)."""
        mmu = self.machine.mmu
        return mmu.read_va(addr, size) if mmu is not None else self.machine.read_bytes(addr, size)

    def _u16(self, addr: int) -> int:
        return struct.unpack("<H", self._read(addr, 2))[0]

    def _u32(self, addr: int) -> int:
        return struct.unpack("<I", self._read(addr, 4))[0]

    @property
    def ready(self) -> bool:
        try:
            ao = self._u32(AO_PTR_GLOBAL)
            return 0x0800_0000 <= ao < 0x0844_0000
        except Exception:  # noqa: BLE001
            return False

    # --- read-only PC watchpoints ---------------------------------------------------------
    def attach_watches(self) -> None:
        """Add the leaf-handler / pump / timer / interpreter observation points.

        Read-only PC watchpoints (the firmware runs unmodified): the statechart leaves,
        the pump, the software-timer tick, plus the GME interpreter's executed-action
        command (the twin of MT's ``gme_exec_command``) and the two play paths (game
        media / system voice) for the "now playing" line.
        """
        for pc in STATE_HANDLERS:
            self.machine.on_code(pc, self._make_leaf(pc))
        self.machine.on_code(PC_SM_DISPATCH, self._on_dispatch)
        self.machine.on_code(PC_TIMER_TICK, self._on_timer_tick)
        self.machine.on_code(PC_GME_EXEC_COMMAND, self._on_exec_command)
        self.machine.on_code(PC_VOICE_PLAY_SAMPLE, self._on_play_media)
        self.machine.on_code(PC_PLAY_CHOMP_VOICE, self._on_play_voice)

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

    def _on_exec_command(self, machine: "Machine") -> None:
        """One executed GME script action (r0..r3 = register, opcode, is-const, operand).

        The exact action trace — the same observable MT's ``gme_exec_command`` watch gives,
        rendered generation-neutrally. The playlist for a ``Play n`` action comes from the
        routed line, resolved in :meth:`snapshot`; here the raw opcode/operand are enough.
        """
        uc = machine.uc
        reg = uc.reg_read(UC_ARM_REG_R0)
        opcode = uc.reg_read(UC_ARM_REG_R1)
        is_const = uc.reg_read(UC_ARM_REG_R2)
        operand = uc.reg_read(UC_ARM_REG_R3)
        text = render_action(reg, opcode, is_const, operand, self.symbols, ())
        self._recent_actions.append(text)
        self._log(f"zc3201 gme: exec {text}")

    def _on_play_media(self, _machine: "Machine") -> None:
        self.last_play = "GME media (voice_play_sample)"
        self._log(f"zc3201 gme: {self.last_play}")

    def _on_play_voice(self, _machine: "Machine") -> None:
        self.last_play = "system voice (Chomp_Voice)"
        self._log(f"zc3201 voice: {self.last_play}")

    # --- interpreter RAM reads ------------------------------------------------------------
    def _u8(self, addr: int) -> int:
        return self._read(addr, 1)[0]

    def _ptr(self, addr: int) -> int | None:
        p = self._u32(addr)
        return p if 0x0800_0000 <= p < 0x0844_0000 else None

    def registers(self) -> tuple[int, ...]:
        """The GME ``$``-register file, clamped to the live register count."""
        count = min(self._u16(REG_COUNT_ADDR), MAX_REGISTERS)
        if count <= 0:
            return ()
        return struct.unpack(f"<{count}H", self._read(REG_FILE_ADDR, 2 * count))

    def _scripts_for(self, product: int) -> GmeScripts | None:
        for scripts in self._scripts:
            if scripts.product == product:
                return scripts
        return None

    # --- transition log (leaf changes; the QF framework has no QHsm frame stack) ----------
    def poll_transition(self) -> Transition | None:
        """One statechart move since the last poll, or ``None``.

        ZC3201's QF framework is dispatched by handler PC (no QHsm frame-byte stack), so a
        "transition" is a change of the active leaf handler. Pre-rendered with the ZC3201
        state names (:data:`Transition.text`), since MT's ``state_name`` doesn't know them.
        """
        if self._reported_leaves >= len(self.leaves):
            return None
        _clock, pc = self.leaves[self._reported_leaves]
        prev = self.leaves[self._reported_leaves - 1][1] if self._reported_leaves else 0
        self._reported_leaves += 1
        old = state_name(prev) if prev else "(pre-init)"
        text = f"leaf    {old} → {state_name(pc)}"
        return Transition(kind="leaf", old_chain=(prev,) if prev else (),
                          new_chain=(pc,), text=text)

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

    # --- OID -> script routing (container-driven, keyed on akoid last_oid) -----------------
    def _routing(
        self, oid: int, product: int, registers: tuple[int, ...], oid_min: int, oid_max: int
    ) -> OidRouting | None:
        """Render "tap OID → script line" from the .gme container + symbols.

        Unlike MT (which reads the resident parsed line out of RAM), ZC3201's resident
        line arrays are cleared between parse and exec, so this routes from the container
        keyed on the reliable akoid ``last_oid`` and its content-OID band. The interpreter
        evaluates lines in order and runs the first whose conditions hold; the first line is
        the executed one for the single-line scripts typical of content OIDs.
        """
        if oid == 0:
            return None
        symbols = self.symbols
        label = (symbols.oid_label(oid) or "") if symbols else ""
        if oid <= PRODUCT_OID_MAX:
            return OidRouting(oid=oid, kind="product-band", label=label)
        if oid_min and not (oid_min <= oid <= oid_max):
            return OidRouting(oid=oid, kind="out-of-range", label=label)
        scripts = self._scripts_for(product)
        lines = scripts.script(oid) if scripts else None
        if lines is None:  # no container loaded — show the tap + band only
            return OidRouting(oid=oid, kind="content", label=label)
        if not lines:
            return OidRouting(oid=oid, kind="no-script", label=label)
        matched = 0
        line = lines[matched]
        conditions = tuple(render_condition(c, symbols, registers) for c in line.conds)
        actions = tuple(
            render_action(reg, op, const, operand, symbols, line.playlist)
            for reg, op, const, operand in line.actions
        )
        playlist = tuple(
            f"{index}:{symbols.media_name(index)}" if symbols else str(index)
            for index in line.playlist
        )
        source = symbols.script_source(oid, matched) if symbols else None
        return OidRouting(
            oid=oid, label=label, kind="content", line_count=len(lines),
            matched_line=matched, conditions=conditions, actions=actions,
            playlist=playlist, source=source or "",
        )

    # --- the full snapshot (the shared MtDebugSnapshot the TUI panels render) --------------
    def snapshot(self) -> MtDebugSnapshot:
        """One immutable debug view; all-RAM, never raises (empty on any torn read)."""
        try:
            return self._snapshot()
        except Exception:  # noqa: BLE001
            return MtDebugSnapshot()

    def _snapshot(self) -> MtDebugSnapshot:
        if not self.ready:
            return MtDebugSnapshot(ready=False)
        chain = (self.leaf_pc,) if self.leaf_pc else ()
        chain_names = (state_name(self.leaf_pc),) if self.leaf_pc else ()
        registers = self.registers()
        product = self._u32(PRODUCT_ID_ADDR)
        handle = self._u32(CURRENT_GME_HANDLE)
        handle = handle - 0x1_0000_0000 if handle >= 0x8000_0000 else handle
        status = self._u8(GME_STATUS_ADDR)
        symbols = self.symbols
        product_label = (
            symbols.comment
            if symbols and symbols.product_id == product and symbols.comment
            else ""
        )
        register_names = tuple(
            (symbols.register_name(i) if symbols else f"${i}") for i in range(len(registers))
        )

        last_oid = oid_first = oid_last = 0
        routing = None
        play_busy = False
        playall_mode = playall_cursor = 0
        deferred: int | None = None
        deferred_label = ""
        akoid = self._ptr(AKOID_PTR_ADDR)
        if akoid is not None:
            last_oid = self._u32(akoid + AKOID_LAST_OID) & 0xFFFF
            oid_first = self._u16(akoid + AKOID_OID_MIN)
            oid_last = self._u16(akoid + AKOID_OID_MAX)
            routing = self._routing(last_oid, product, registers, oid_first, oid_last)
            play_busy = bool(self._u8(akoid + AKOID_PLAY_BUSY))
            playall_mode = self._u8(akoid + AKOID_PLAYALL_MODE)
            playall_cursor = self._u8(akoid + AKOID_PLAYALL_CURSOR)
            if self._u8(akoid + AKOID_JUMP_PENDING):
                deferred = self._u16(akoid + AKOID_JUMP_TARGET)
                if symbols is not None:
                    deferred_label = symbols.oid_label(deferred) or ""

        return MtDebugSnapshot(
            ready=True,
            chain=chain,
            chain_names=chain_names,
            last_event=self._recent_events[-1] if self._recent_events else 0,
            registers=registers,
            register_names=register_names,
            product=product,
            product_label=product_label,
            gme_handle=handle,
            gme_mounted=bool(status & 0x80),
            gme_path="",
            book_count=0,
            oid_first=oid_first,
            oid_last=oid_last,
            last_oid=last_oid,
            first_tap_oid=0,
            routing=routing,
            play_busy=play_busy,
            playall_mode=playall_mode,
            playall_cursor=playall_cursor,
            xor_active=bool(self._u8(XOR_ACTIVE_ADDR)),
            deferred_jump=deferred,
            deferred_jump_label=deferred_label,
            timer_slot=None,
            tick=self._u32(TICK_ADDR),
            heartbeat=0,
            last_play=self.last_play,
            recent_actions=tuple(self._recent_actions),
        )
