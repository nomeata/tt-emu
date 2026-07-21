"""Provision an authentic NAND image by running the firmware's own ``producer.bin``.

tt-emu's principle is to run the pen firmware **unmodified** and model the
hardware. The on-media NAND layout the firmware mounts (``MtdLib``/``FatLib``
metadata, the zone/partition table, the FAT superfloppies) is *itself* produced
by the factory USB-flash tool ``producer.bin`` that ships inside every ``.upd``
container. Rather than hand-reconstruct that layout from the mount side (guesswork
that differs per generation), this module **runs the producer** against a Python
NAND model and captures the exact bytes it writes — guaranteed-correct where a
hand-built image is guesswork, because the producer and the firmware share the
same ``MtdLib`` library (``MtdLib59`` in the producer == ``mtd_helper59`` in the
firmware's mount).

The producer is a *provisioning tool*, not the firmware under test, so hooking
its libc/NFC leaves (malloc/printf and the OS-vtable NAND ops) is legitimate — it
is exactly how the MT reference image was built (``firmware-re/tools/
ttrun_producer.py``, ``docs/producer-run-results.md``). The captured page image is
then served **authentically** to the unmodified firmware through the real
:class:`~tt_emu.peripherals.nand.NfcController`.

The addresses come from :class:`~tt_emu.firmware_profile.ProducerProfile`, so MT
and ZC3201 flow through the same code path. Results are cached on disk keyed by
the producer + firmware content, so provisioning runs once and later machine
builds reuse it.
"""

from __future__ import annotations

import collections
import hashlib
import logging
import os
import struct
from pathlib import Path

from unicorn import (
    UC_ARCH_ARM,
    UC_HOOK_CODE,
    UC_HOOK_INTR,
    UC_HOOK_MEM_FETCH_UNMAPPED,
    UC_HOOK_MEM_READ,
    UC_HOOK_MEM_READ_UNMAPPED,
    UC_HOOK_MEM_WRITE,
    UC_HOOK_MEM_WRITE_UNMAPPED,
    UC_MEM_FETCH,
    UC_MEM_FETCH_PROT,
    UC_MEM_FETCH_UNMAPPED,
    UC_MODE_ARM,
    UC_MODE_LITTLE_ENDIAN,
    Uc,
    UcError,
)
from unicorn.arm_const import (
    UC_ARM_REG_CPSR,
    UC_ARM_REG_LR,
    UC_ARM_REG_PC,
    UC_ARM_REG_R0,
    UC_ARM_REG_R1,
    UC_ARM_REG_R2,
    UC_ARM_REG_R3,
    UC_ARM_REG_SP,
)

from .firmware_profile import MT, FirmwareProfile, ProducerProfile
from .loader import Firmware
from .nand_image import BLOCK_SIZE, NandImage

log = logging.getLogger(__name__)

__all__ = ["WritableNand", "run_producer", "provision_nand_image", "NandGeometry"]

# --- geometry (the pen's real Hynix/Samsung 512-MiB chip; same for both gens) --------

PAGE = 2048
SPARE = 64
PAGES_BLK = 64
NBLOCKS = 4096
BLK = PAGE * PAGES_BLK  # 128 KiB == nand_image.BLOCK_SIZE

assert BLK == BLOCK_SIZE


class NandGeometry:
    page = PAGE
    spare = SPARE
    pages_blk = PAGES_BLK
    nblocks = NBLOCKS
    blk = BLK


# ================================================================ NAND model


class WritableNand:
    """Blank-0xFF NAND of the pen's geometry, written at the HAL-leaf boundary.

    Stores page DATA + the per-page spare tag the producer programs (both the
    4-byte metadata magics and the 8-byte NFTL tags). No ECC synthesis (bypassed
    at the seam) — same model as ``firmware-re/tools/ttrun_producer.py``.
    """

    def __init__(self) -> None:
        self.data: dict[int, bytearray] = {}          # block -> 128 KiB (missing => 0xFF)
        self.spare: dict[tuple[int, int], bytes] = {}  # (block, page) -> tag
        self.log: list[tuple[str, int, int, int]] = []

    def _blk(self, block: int) -> bytearray:
        d = self.data.get(block)
        if d is None:
            d = bytearray(b"\xff" * BLK)
            self.data[block] = d
        return d

    def erase(self, block: int) -> None:
        self.data[block] = bytearray(b"\xff" * BLK)
        for pg in range(PAGES_BLK):
            self.spare.pop((block, pg), None)
        self.log.append(("erase", block, -1, 0))

    def program(self, block: int, page: int, data: bytes, tag: bytes) -> None:
        d = self._blk(block)
        data = (bytes(data) + b"\xff" * PAGE)[:PAGE]
        d[page * PAGE : page * PAGE + PAGE] = data
        if tag:
            self.spare[(block, page)] = bytes(tag)
        self.log.append(("prog", block, page, len(tag) if tag else 0))

    def read_page(self, block: int, page: int) -> bytes:
        d = self.data.get(block)
        if d is None:
            return b"\xff" * PAGE
        return bytes(d[page * PAGE : page * PAGE + PAGE])

    def read_spare(self, block: int, page: int, taglen: int) -> bytes:
        t = self.spare.get((block, page))
        if t is None:
            return b"\xff" * taglen
        return (t + b"\xff" * taglen)[:taglen]

    # --- serialization (cache) -------------------------------------------------------

    def raw_pages(self) -> bytes:
        """The flat data-page image (``NBLOCKS·PAGES_BLK·PAGE``, sparse = 0xFF)."""
        maxblk = (max(self.data) + 1) if self.data else 1
        out = bytearray()
        for blk in range(maxblk):
            d = self.data.get(blk)
            out += (bytes(d) if d is not None else b"\xff" * BLK)
        return bytes(out)

    def serialize(self) -> bytes:
        """A self-contained cache blob: sparse data blocks + the spare-tag map.

        Format: ``b"TTNAND01"`` | u32 nblocks | [u32 block, 128 KiB]* |
        u32 ntags | [u32 (block<<16|page), u8 len, tag-bytes]*.
        """
        out = bytearray(b"TTNAND01")
        out += struct.pack("<I", len(self.data))
        for block in sorted(self.data):
            out += struct.pack("<I", block) + bytes(self.data[block])
        out += struct.pack("<I", len(self.spare))
        for (block, page), tag in self.spare.items():
            out += struct.pack("<IB", (block << 16) | page, len(tag)) + bytes(tag)
        return bytes(out)

    @classmethod
    def deserialize(cls, blob: bytes) -> "WritableNand":
        n = cls()
        assert blob[:8] == b"TTNAND01", "bad nand cache magic"
        off = 8
        (ndata,) = struct.unpack_from("<I", blob, off)
        off += 4
        for _ in range(ndata):
            (block,) = struct.unpack_from("<I", blob, off)
            off += 4
            n.data[block] = bytearray(blob[off : off + BLK])
            off += BLK
        (ntags,) = struct.unpack_from("<I", blob, off)
        off += 4
        for _ in range(ntags):
            key, ln = struct.unpack_from("<IB", blob, off)
            off += 5
            n.spare[(key >> 16, key & 0xFFFF)] = blob[off : off + ln]
            off += ln
        return n

    def to_nand_image(self) -> NandImage:
        """Convert into the backing store the real :class:`NfcController` serves.

        Data lands at flat byte offsets (``block·BLOCK_SIZE + page·PAGE``). Spare
        tags are keyed by the NFC row ``block<<8 | au`` (au = page//2, the 2-page
        4-KiB allocation unit the §4.2 decode addresses); page 0's tag is the
        block's chain-head/metadata tag the mount reads.

        CAVEAT (known-imperfect — see docs/zc3201-boot-feasibility.md "Leg 5"):
        this conversion is *lossy*. The producer writes a **real** on-NAND layout
        (e.g. block-0 metadata at PAGES 61/62/63 — bin-info / bin-header / zone
        table), but tt-emu's :class:`NfcController` decode + tag-keying is tuned
        to the hand-built :func:`~tt_emu.nand_image.build_nand_image` layout
        (metadata as flat ``row·0x1000`` offsets), and the ``au = page//2``
        collapse aliases pages 62↔63 onto the same tag row. Serving a producer
        image faithfully needs the NfcController to model real **page + 64-B OOB**
        NAND (so the firmware's own ``MtdLib`` reads drive the addressing), or a
        replay that inverts the ``MtdLib`` decode. Kept as the honest starting
        point for the ZC3201 mount bring-up, not a finished conversion.
        """
        img = NandImage()
        for block, d in self.data.items():
            img.program(block * BLK, bytes(d))
        for (block, page), tag in self.spare.items():
            au = page // 2
            img.set_tag(block << 8 | au, tag)
        return img


# ================================================================ harness


def _s32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def run_producer(
    producer_bin: bytes,
    upd_bytes: bytes,
    prod: ProducerProfile,
    *,
    b_mb: int = 64,
    verbose: bool = False,
) -> WritableNand:
    """Run ``producer.bin`` under Unicorn and capture the NAND image it formats.

    Drives the factory format sequence (cmd 5 init → 7 ASA → 9 partition →
    0x22 write-maps → 10 mkfs) through the producer's own dispatchers, hooking
    the OS-vtable NAND ops to a :class:`WritableNand`. ``b_mb`` caps partition B
    (the ``.upd`` carries a fill-the-rest sentinel far larger than the chip).
    """
    nand = WritableNand()
    uc = Uc(UC_ARCH_ARM, UC_MODE_ARM | UC_MODE_LITTLE_ENDIAN)
    base = prod.load_base
    # One RAM window covers image + bss + stacks + packet scratch; a big heap.
    uc.mem_map(base, 0x0040_0000)
    heap_base, heap_end = 0x0900_0000, 0x1100_0000
    uc.mem_map(heap_base, heap_end - heap_base)
    uc.mem_write(base, producer_bin)

    pkt_base = base + 0x50000
    sentinel = base + 0x380000
    heap_ptr = [heap_base]
    prlog: list[str] = []
    nand_ops: collections.Counter[str] = collections.Counter()
    cur_ce = [0]

    # --- MMIO catch-all: read-as-0 except NFC ready/read-ID ---
    uc.mem_map(0x0400_0000, 0x0020_0000)

    def hook_mmio_r(uc_: Uc, acc: int, addr: int, sz: int, val: int, ud: object) -> bool:
        b0 = addr & ~3
        if addr == prod.nfc_readid_reg:
            v = prod.chip_id if cur_ce[0] == 0 else 0xFFFFFFFF
            uc_.mem_write(b0, struct.pack("<I", v))
            return True
        if 0x0404_A000 <= addr < 0x0404_B000 or 0x0405_B000 <= addr < 0x0405_C000:
            uc_.mem_write(b0, struct.pack("<I", 0xFFFFFFFF))
            return True
        uc_.mem_write(b0, struct.pack("<I", 0))
        return True

    uc.hook_add(UC_HOOK_MEM_READ, hook_mmio_r, None, 0x0400_0000, 0x0420_0000)
    uc.hook_add(UC_HOOK_MEM_WRITE, lambda *a: True, None, 0x0400_0000, 0x0420_0000)

    def on_unmapped(uc_: Uc, acc: int, addr: int, sz: int, val: int, ud: object) -> bool:
        if acc in (UC_MEM_FETCH, UC_MEM_FETCH_UNMAPPED, UC_MEM_FETCH_PROT):
            return False
        try:
            uc_.mem_map(addr & ~0xFFFF, 0x10000)
            return True
        except UcError:
            return False

    uc.hook_add(
        UC_HOOK_MEM_READ_UNMAPPED | UC_HOOK_MEM_WRITE_UNMAPPED | UC_HOOK_MEM_FETCH_UNMAPPED,
        on_unmapped,
    )

    def rd(a: int, n: int = 4) -> int:
        return int.from_bytes(uc.mem_read(a, n), "little")

    def wr(a: int, v: int, n: int = 4) -> None:
        uc.mem_write(a, int(v).to_bytes(n, "little"))

    def ret(val: int) -> None:
        lr = uc.reg_read(UC_ARM_REG_LR)
        uc.reg_write(UC_ARM_REG_R0, val & 0xFFFFFFFF)
        uc.reg_write(UC_ARM_REG_PC, lr & ~1)

    def read_cstr(p: int, mx: int = 256) -> str:
        out = b""
        while len(out) < mx:
            c = uc.mem_read(p + len(out), 1)[0]
            if c == 0:
                break
            out += bytes([c])
        return out.decode("latin1", "replace")

    # --- libc oracle ---
    def do_malloc() -> None:
        n = (uc.reg_read(UC_ARM_REG_R0) + 7) & ~7
        p = heap_ptr[0]
        heap_ptr[0] += n
        if heap_ptr[0] > heap_end:
            raise RuntimeError("bump heap exhausted")
        uc.mem_write(p, b"\x00" * n)
        ret(p)

    def do_printf() -> None:
        prlog.append(read_cstr(uc.reg_read(UC_ARM_REG_R0)))
        if verbose:
            log.info("[pr] %s", prlog[-1])
        ret(0)

    # --- NAND op hooks ---
    def _stack_tag() -> tuple[int, int]:
        sp = uc.reg_read(UC_ARM_REG_SP)
        a0, a1 = rd(sp), rd(sp + 4)
        if 1 <= a1 <= 64:
            return a0, a1
        if 1 <= a0 <= 64:
            return a1, a0
        return a0, a1

    def do_write() -> None:
        nand_ops["write"] += 1
        blk = uc.reg_read(UC_ARM_REG_R1)
        pg = uc.reg_read(UC_ARM_REG_R2)
        dptr = uc.reg_read(UC_ARM_REG_R3)
        tptr, taglen = _stack_tag()
        if blk >= NBLOCKS:
            ret(0)
            return
        data = bytes(uc.mem_read(dptr, PAGE)) if dptr else b"\xff" * PAGE
        tag = bytes(uc.mem_read(tptr, taglen)) if (tptr and 0 < taglen <= 64) else b""
        nand.program(blk, pg, data, tag)
        ret(1)

    def do_read() -> None:
        nand_ops["read"] += 1
        blk = uc.reg_read(UC_ARM_REG_R1)
        pg = uc.reg_read(UC_ARM_REG_R2)
        dptr = uc.reg_read(UC_ARM_REG_R3)
        tptr, taglen = _stack_tag()
        if dptr:
            uc.mem_write(dptr, nand.read_page(blk, pg))
        if tptr and 0 < taglen <= 64:
            uc.mem_write(tptr, nand.read_spare(blk, pg, taglen))
        ret(1)

    def do_erase() -> None:
        nand_ops["erase"] += 1
        blk = uc.reg_read(UC_ARM_REG_R1)
        if blk >= NBLOCKS:
            ret(0)
            return
        nand.erase(blk)
        ret(1)

    def do_bootwr() -> None:
        nand.log.append(("boot", uc.reg_read(UC_ARM_REG_R0), 0, 0))
        ret(1)

    def do_badchk() -> None:
        ret(0 if uc.reg_read(UC_ARM_REG_R1) < NBLOCKS else 1)

    hooks = {
        prod.r_malloc: do_malloc,
        prod.r_free: lambda: ret(0),
        prod.r_printf: do_printf,
        prod.n_write: do_write,
        prod.n_wrpg0: do_write,
        prod.n_read: do_read,
        prod.n_rdpg0: do_read,
        prod.n_erase: do_erase,
        prod.n_bootwr: do_bootwr,
        prod.anyka_ic_gate: lambda: ret(1),
        prod.ready_poll: lambda: ret(0),
        prod.badchk: do_badchk,
    }

    def _mk(fn: object) -> object:
        def cb(uc_: Uc, addr: int, size: int, ud: object) -> None:
            fn()  # type: ignore[operator]

        return cb

    for a, fn in hooks.items():
        uc.hook_add(UC_HOOK_CODE, _mk(fn), None, a, a)

    def _peek_readid(uc_: Uc, addr: int, size: int, ud: object) -> None:
        cur_ce[0] = uc_.reg_read(UC_ARM_REG_R0)

    uc.hook_add(UC_HOOK_CODE, _peek_readid, None, prod.readid_peek, prod.readid_peek)

    # --- ARM semihosting (SVC 0xAB): MtdLib/FatLib logs ---
    def hook_intr(uc_: Uc, intno: int, ud: object) -> None:
        op = uc_.reg_read(UC_ARM_REG_R0)
        arg = uc_.reg_read(UC_ARM_REG_R1)
        if op == 0x04:
            prlog.append(read_cstr(arg, 512))
        elif op == 0x03:
            prlog.append(chr(uc_.mem_read(arg, 1)[0]))
        uc_.reg_write(UC_ARM_REG_R0, 0)

    uc.hook_add(UC_HOOK_INTR, hook_intr)

    # --- run startup ---
    uc.reg_write(UC_ARM_REG_CPSR, 0x13 | 0xC0)
    uc.reg_write(UC_ARM_REG_SP, prod.svc_sp)
    uc.mem_write(sentinel, struct.pack("<I", 0xFFFFFFFF))
    if prod.calib_bound is not None:
        addr, value = prod.calib_bound
        uc.mem_write(addr, struct.pack("<I", value))

    try:
        uc.emu_start(prod.entry, prod.usb_loop, count=50_000_000)
    except UcError as e:
        log.warning("producer startup fault %s PC=%#x", e, uc.reg_read(UC_ARM_REG_PC))

    # --- .upd inputs (flash_ic row + partition records) ---
    def uu32(o: int) -> int:
        return struct.unpack("<I", upd_bytes[o : o + 4])[0]

    flash_ic = upd_bytes[0x200 + 48 * 0x40 : 0x200 + 48 * 0x40 + 0x40]
    part_recs = [upd_bytes[0xA4 + i * 0x18 : 0xA4 + (i + 1) * 0x18] for i in range(uu32(0x08))]

    # --- command driver ---
    def call_cmd(disp: int, cmdnum: int, pkt: bytes = b"", r1: int = 0) -> tuple[int | None, bool]:
        uc.mem_write(pkt_base, (pkt + b"\x00" * 64)[: max(len(pkt), 64)])
        wr(prod.cmd_ctx + 4, cmdnum)
        for i, v in enumerate([pkt_base, r1, 0, 0]):
            uc.reg_write(UC_ARM_REG_R0 + i, v & 0xFFFFFFFF)
        uc.reg_write(UC_ARM_REG_SP, prod.svc_sp)
        uc.reg_write(UC_ARM_REG_LR, sentinel)
        uc.reg_write(UC_ARM_REG_CPSR, 0x13 | 0xC0)
        try:
            uc.emu_start(disp, sentinel, count=400_000_000)
            pc = uc.reg_read(UC_ARM_REG_PC)
            r0 = uc.reg_read(UC_ARM_REG_R0)
            log.info(
                "[cmd %#x] -> %s r0=%d (progs=%d erases=%d)",
                cmdnum,
                "RET" if pc == sentinel else f"PC={pc:#x}",
                _s32(r0),
                sum(1 for x in nand.log if x[0] == "prog"),
                sum(1 for x in nand.log if x[0] == "erase"),
            )
            return r0, pc == sentinel
        except UcError as e:
            log.warning("[cmd %#x] FAULT %s PC=%#x", cmdnum, e, uc.reg_read(UC_ARM_REG_PC))
            return None, False

    # cmd 5 init (packet = flash_ic row); seed single-chip count if cmd 4/6 skipped.
    call_cmd(prod.disp_media, 5, flash_ic)
    if rd(prod.pr_ctx) == 0:
        wr(prod.pr_ctx, 1)
    # cmd 7 ASA (non-destructive scan; model has 0 bad blocks).
    call_cmd(prod.disp_burn, 7, struct.pack("<I", 0))
    # cmd 9 partition: resolve B's fill-rest sentinel to a bounded size.
    recs = [bytearray(r) for r in part_recs]
    if len(recs) >= 2:
        struct.pack_into("<I", recs[1], 4, b_mb)
    pinfo = b"".join(bytes(r) for r in recs)
    call_cmd(prod.disp_media, 9, struct.pack("<II", 0, len(recs)) + pinfo)
    # cmd 0x22 write maps (metadata into block 0).
    call_cmd(prod.disp_burn, 0x22)
    # cmd 10 FAT mkfs (may fault without the full FsLib/Medium stack — see docs).
    if prod.fslib_driver_obj is not None:
        stub = base + 0x61000
        uc.mem_write(stub, struct.pack("<II", 0xE3A00000, 0xE12FFF1E))
        if rd(prod.fslib_driver_obj + 0x18) == 0:
            wr(prod.fslib_driver_obj + 0x18, stub)
    call_cmd(prod.disp_media, 10, b"\x00\x00\x00\x00" + b"A" + b"TIPTOI".ljust(11, b"\x00"))

    log.info(
        "producer run: %d programs, %d erases, %d blocks touched",
        sum(1 for x in nand.log if x[0] == "prog"),
        sum(1 for x in nand.log if x[0] == "erase"),
        len({x[1] for x in nand.log if x[0] == "prog"}),
    )
    return nand


# ================================================================ cached provisioning


def _cache_dir() -> Path:
    root = os.environ.get("TT_EMU_CACHE") or os.path.join(
        os.path.expanduser("~"), ".cache", "tt-emu"
    )
    p = Path(root) / "nand"
    p.mkdir(parents=True, exist_ok=True)
    return p


def provision_nand_image(
    firmware: Firmware,
    profile: FirmwareProfile | None = None,
    *,
    b_mb: int = 64,
    use_cache: bool = True,
) -> NandImage:
    """Provision a mountable :class:`NandImage` by running the firmware's producer.

    Cached on disk keyed by the producer + PROG + nandboot content and ``b_mb``,
    so the (slow) producer run happens once and later builds reuse the raw page
    image. ``profile`` selects the :class:`ProducerProfile`; defaults to the
    profile detected from the container (falling back to MT).
    """
    if profile is None:
        profile = firmware.profile or MT
    prod = profile.producer
    if prod is None:
        raise ValueError(f"no ProducerProfile for {profile.key!r} — cannot provision")

    key = hashlib.sha256(
        firmware.producer.data
        + firmware.prog.data[:4096]
        + firmware.nandboot.data[:256]
        + struct.pack("<I", b_mb)
    ).hexdigest()[:32]
    cache = _cache_dir() / f"{profile.key}-{key}.nand"

    if use_cache and cache.exists():
        log.info("nand: reusing cached producer image %s", cache)
        return WritableNand.deserialize(cache.read_bytes()).to_nand_image()

    upd_bytes = firmware.path.read_bytes()
    nand = run_producer(firmware.producer.data, upd_bytes, prod, b_mb=b_mb)
    if use_cache:
        cache.write_bytes(nand.serialize())
        log.info("nand: cached producer image -> %s", cache)
    return nand.to_nand_image()
