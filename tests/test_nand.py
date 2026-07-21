"""Tests for the storage subsystem: AU decode, FAT16 builder, image builder,
and the register-level NFC protocol (driven exactly as the firmware drives it,
per ``nand-and-nfc-controller.md`` §8)."""

from __future__ import annotations

import struct

import pytest
from _data import firmware_path

from tt_emu.fat16 import build_fat16
from tt_emu.machine import Machine, MachineConfig
from tt_emu.nand_image import (
    A_BLOCKS,
    AU_SIZE,
    BLOCK_SIZE,
    FS_START,
    NandImage,
    SYS_START,
    make_head_tag,
)
from tt_emu.peripherals.nand import (
    AU_SECS,
    EccEngine,
    L2NandBuffer,
    NAND_READ_ID,
    NfcController,
    SRAM_WINDOW,
    decode_byte_offset,
    tag_key,
)

UPD_PATH = firmware_path()

#: eccsize for ECC mode 2: 512 + (7*2+7) = 0x215 (§4.1 worked example).
ECCSIZE = 0x215
#: eccsize for the driver's mode-4 data records: 512 + (14*4-14) = 554.
ECCSIZE_M4 = 512 + 42


# --- §4.2 address decode (the critical part) ---------------------------------------


@pytest.mark.parametrize(
    "row,col,expected",
    [
        # §4.3 worked examples (Observed trace values)
        (0x8600, 0, 134 * 0x20000),                     # block 134, AU 0
        (0x8601, 0, 134 * 0x20000 + 0x1000),            # block 134, AU 1
        (0x8603, 0, 134 * 0x20000 + 0x3000),            # block 134, AU 3
        (0x8700, 0, 135 * 0x20000),                     # block 135 starts a new window
        (0x2400, 1 * ECCSIZE, 36 * 0x20000 + 0x200),    # 2nd sector of an AU
    ],
)
def test_decode_worked_examples(row: int, col: int, expected: int) -> None:
    assert decode_byte_offset(row, col, ECCSIZE) == expected


def test_decode_not_flat_sector_index() -> None:
    """§4.4: row must NOT be decoded as a flat 512-B sector index."""
    # AU k serves sectors [8k, 8k+8), not sector k.
    assert decode_byte_offset(0x8601, 0, ECCSIZE) == (256 * 134 + 8) * 512


def test_decode_sub_sector_all_modes() -> None:
    """col = k*eccsize selects sector k for every plausible ECC strength."""
    for eccsize in (ECCSIZE, ECCSIZE_M4):
        for k in range(8):
            assert decode_byte_offset(0x100, k * eccsize, eccsize) == 0x20000 + k * 512


def test_tag_key_is_raw_row() -> None:
    """Tags are keyed by the raw row (§4.2): a unit ≥ 32 of a block's window
    aliases the next blocks' *data* linearly but owns its own spare tag."""
    assert tag_key(0x8600) == 0x8600
    assert tag_key(0x8613) == 0x8613
    assert tag_key(255) == 255  # metadata row (sparse block-0 row space)
    assert tag_key(0x8620) != tag_key(0x8700)  # unit 32 != next block's page 0
    assert AU_SECS == 8


# --- NandImage layering -------------------------------------------------------------


def test_image_erased_reads_ff_and_program_overlays() -> None:
    img = NandImage()
    assert img.read(0x123456, 8) == b"\xff" * 8
    img.program(0x20000 - 4, b"ABCDEFGH")  # spans a block boundary
    assert img.read(0x20000 - 4, 8) == b"ABCDEFGH"
    img.program(0x20000, b"XY")  # last write wins
    assert img.read(0x20000 - 4, 8) == b"ABCDXYGH"


def test_image_erase_shadows_data_and_tags() -> None:
    img = NandImage()
    img.program(5 * BLOCK_SIZE, b"data")
    img.set_tag(5 * 256, make_head_tag(0))
    img.erase_block(5)
    assert img.read(5 * BLOCK_SIZE, 4) == b"\xff" * 4
    assert img.get_tag(5 * 256) == b"\xff" * 8  # tags dropped (§8.3)


def test_head_tag_format() -> None:
    tag = make_head_tag(60)  # B:'s first NFTL block is logical 60 (VBLOCK doc gap)
    assert tag[0] == 1 and tag[0] & 0x80 == 0     # seq, not obsolete
    assert tag[2:4] == b"\xfd\xff"                # 0xfffd chain end
    assert struct.unpack("<H", tag[4:6])[0] == 60 | 0x8000


def test_unit_tag_format() -> None:
    from tt_emu.nand_image import make_unit_tag

    tag = make_unit_tag(60)  # the firmware's append-form unit tag (Observed)
    assert tag[0] == 0 and tag[1] == 0
    assert tag[2:4] == b"\xfd\xff"                       # chain end
    assert struct.unpack("<H", tag[4:6])[0] == 60        # bit 15 clear
    assert struct.unpack("<H", tag[6:8])[0] == 1         # own = 0x0001


# --- FAT16 builder -------------------------------------------------------------------


def test_fat16_mount_checks() -> None:
    """The §5 sector-0 checks the firmware's mount performs."""
    vol = build_fat16(60 * 1024 * 1024, label="SYSTEM")
    d = vol.data
    assert struct.unpack_from("<H", d, 0x0B)[0] == 512      # bytes/sector
    assert d[0x36:0x3E] == b"FAT16   "                       # FS type label
    assert struct.unpack_from("<I", d, 0x1C6)[0] == 0        # dispatch heuristic
    assert d[0x1FE:0x200] == b"\x55\xaa"
    assert vol.total_clusters >= 4085                        # not FAT12-sized


def test_fat16_rejects_fat12_sized_volume() -> None:
    with pytest.raises(ValueError):
        build_fat16(4 * 1024 * 1024, sectors_per_cluster=4)


def test_fat16_files_round_trip() -> None:
    payload = bytes(range(256)) * 33  # multi-cluster (8448 B > 2048-B cluster)
    vol = build_fat16(
        60 * 1024 * 1024,
        label="tiptoi",
        files={"game.gme": payload, "Language/UpdateGERMAN.wav": b"RIFFxxxx"},
    )
    d = vol.data
    spc, reserved = d[13], struct.unpack_from("<H", d, 14)[0]
    nfats, root_entries = d[16], struct.unpack_from("<H", d, 17)[0]
    fat_sectors = struct.unpack_from("<H", d, 22)[0]
    root_off = (reserved + nfats * fat_sectors) * 512
    first_data = root_off + root_entries * 32

    def find_entry(dir_off: int, dir_len: int, want: str) -> tuple[int, int]:
        """Resolve ``want`` (long name, case-insensitive) to (cluster, size).

        Matches the way the firmware opens files: a run of VFAT long-name
        entries (attr 0x0F) reconstructs the long name for the 8.3 entry that
        follows; the 8.3 short name matches too (case-insensitively).
        """
        lfn = ""
        for i in range(dir_len // 32):
            e = d[dir_off + i * 32 : dir_off + (i + 1) * 32]
            if e[0] == 0:
                break
            if e[11] == 0x0F:  # long-name entry: 13 UTF-16 units at 1/14/28
                chunk = e[1:11] + e[14:26] + e[28:32]
                part = chunk.decode("utf-16-le").split("\x00")[0].rstrip("￿")
                lfn = part + lfn  # entries are stored highest-sequence first
                continue
            short = e[:11].decode("ascii").rstrip()
            short = short[:8].rstrip() + ("." + short[8:].rstrip() if short[8:].strip() else "")
            if want.upper() in (lfn.upper(), short.upper()) and not e[11] & 0x08:
                return struct.unpack_from("<H", e, 26)[0], struct.unpack_from("<I", e, 28)[0]
            lfn = ""
        raise AssertionError(f"{want!r} not found")

    def read_file(dir_off: int, dir_len: int, want: str) -> bytes:
        cluster, size = find_entry(dir_off, dir_len, want)
        out = b""
        while cluster < 0xFFF8 and cluster >= 2:
            off = first_data + (cluster - 2) * spc * 512
            out += d[off : off + spc * 512]
            cluster = struct.unpack_from("<H", d, reserved * 512 + cluster * 2)[0]
        return out[:size]

    # The firmware opens by long path — the file must be findable by its full
    # name (via the LFN entries), not only by an 8.3 short alias.
    assert read_file(root_off, root_entries * 32, "game.gme") == payload
    sub_cluster, _ = find_entry(root_off, root_entries * 32, "Language")
    sub_off = first_data + (sub_cluster - 2) * spc * 512
    assert read_file(sub_off, spc * 512, "UpdateGERMAN.wav") == b"RIFFxxxx"


# --- Register-level NFC protocol (driven as the firmware drives it, §8) --------------


class _Rig:
    """A machine with the storage trio and a synthetic image."""

    def __init__(self, img: NandImage) -> None:
        self.m = Machine(MachineConfig())
        self.ecc = EccEngine()
        self.nfc = NfcController(img, self.ecc)
        self.l2 = L2NandBuffer(self.nfc)
        self.m.add_peripheral(self.nfc)
        self.m.add_peripheral(self.ecc)
        self.m.add_peripheral(self.l2)
        self.img = img

    # -- firmware-shaped primitives (nand-and-nfc-controller.md §8) --

    def _stage(self, words: list[int]) -> None:
        for i, w in enumerate(words):
            self.nfc.write(0x100 + 4 * i, 4, w)

    def _go(self) -> None:
        self.nfc.write(0x158, 4, 0x40000200 | (1 << 10))
        assert self.nfc.read(0x158, 4) & 0x80000000  # ready poll

    def _addr_cycles(self, col: int, row: int, *, final_prog: bool = False) -> list[int]:
        words = [((col & 0xFF) << 11) | 0x62, (((col >> 8) & 0xFF) << 11) | 0x62]
        row_bytes = [(row >> (8 * i)) & 0xFF for i in range(3)]
        for j, b in enumerate(row_bytes):
            last_of_prog = final_prog and j == len(row_bytes) - 1
            words.append((b << 11) | (0x63 if last_of_prog else 0x62))
        return words

    def read_au(self, row: int, col: int, nbytes: int, *, mode: int = 2,
                want_tag: bool = True) -> tuple[bytes, bytes]:
        """§8.1 READ: setup list, then per-record ECC config + data op + GO."""
        parity = 7 * mode + 7 if mode < 4 else 14 * mode - 14
        self._stage([0x64, *self._addr_cycles(col, row), 0x18464, 0x201])
        self._go()
        data = b""
        for _ in range(nbytes // 512):
            self.ecc.write(0, 4, (512 << 7) | (mode << 21) | 0xC100012)
            self.ecc.write(0, 4, (512 << 7) | (mode << 21) | 0xC100012 | 8)
            self._stage([((512 + parity - 1) << 11) | 0x119])
            self.l2.write(0, 4, (4 << 8) | 0x800)  # BUF_CTRL(4, attach)
            self._go()
            assert self.l2.read(0x10, 4) >> 16     # fill level != 0
            data += bytes(self.m.read_bytes(SRAM_WINDOW, 512))
            assert self.ecc.read(0, 4) == 0x7000040
        tag = b""
        if want_tag:
            self.ecc.write(0, 4, (8 << 7) | (mode << 21) | 0xC100012 | 8)
            self._stage([((8 + parity - 1) << 11) | 0x119])
            self._go()
            tag = bytes(self.m.read_bytes(SRAM_WINDOW, 8))
        return data, tag

    def program_au(self, row: int, data: bytes, tag: bytes, *, mode: int = 2,
                   record_size: int = 512) -> None:
        """§8.2 PROGRAM: setup, per-record stage-to-window + flush strobe, commit.

        ``record_size`` is the data-record payload (512, or the runtime FAT
        driver's 1024). Records longer than the 512-B window are staged in
        512-B slabs at window offset 0 with one drain poll each (Observed
        protocol) — the parity scales with the sectors per record.
        """
        parity = 7 * mode + 7 if mode < 4 else 14 * mode - 14
        self._stage([0x40064, *self._addr_cycles(0, row, final_prog=True)])
        self._go()
        records = [data[i : i + record_size] for i in range(0, len(data), record_size)]
        records.append(tag)
        for rec in records:
            payload = len(rec)
            rec_parity = parity * max(payload // 512, 1)
            self.ecc.write(0, 4, (payload << 7) | (mode << 21) | 0x100015 | 8)
            self._stage([((payload + rec_parity - 1) << 11) | 0x129])
            self._go()
            for s in range(0, payload, 512):  # <=512-B slabs at window offset 0
                self.m.write_bytes(SRAM_WINDOW, rec[s : s + 512])
                assert self.l2.read(0x10, 4) >> 16 == 0  # drain poll per slab
            self.l2.write(0, 4, (1 << 12) | (4 << 8) | 0x800)  # flush strobe
            self.l2.write(0, 4, (4 << 8) | 0x800)              # re-attach
        self._stage([0x8065])  # cmd 0x10 + LAST
        self._go()

    def erase(self, block: int) -> None:
        row = block << 8
        words = [0x30064]
        words += [(((row >> (8 * i)) & 0xFF) << 11) | 0x62 for i in range(3)]
        words += [0x68064, 0x201]
        self._stage(words)
        self._go()


def test_nfc_sram_window_is_per_instance() -> None:
    # The L2 buffer-4 staging address is fixed hardware SRAM that differs per pen
    # generation; ZC3201 stages into 0x08005800 (the MT 0x08006800 collides with
    # its nandboot-alias code). A read must deposit at the configured window.
    from tt_emu.peripherals.nand import SRAM_WINDOW

    assert NfcController(NandImage(), EccEngine()).sram_window == SRAM_WINDOW
    img = NandImage()
    au = bytes((i * 3 + 1) & 0xFF for i in range(AU_SIZE))
    img.program(36 * BLOCK_SIZE, au)
    rig = _Rig(img)
    rig.nfc.sram_window = 0x0800_5800  # ZC3201 window
    rig.ecc.write(0, 4, (512 << 7) | (2 << 21) | 0xC100012 | 8)
    rig._stage([0x64, *rig._addr_cycles(0, 36 << 8), 0x18464, 0x201])
    rig._go()
    rig.ecc.write(0, 4, (512 << 7) | (2 << 21) | 0xC100012 | 8)
    rig._stage([((512 + 21 - 1) << 11) | 0x119])
    rig._go()
    assert bytes(rig.m.read_bytes(0x0800_5800, 512)) == au[:512]


def test_nfc_read_id_and_status() -> None:
    rig = _Rig(NandImage())
    rig._stage([0x48064, 0x62, 0x3859 | 1])  # cmd 0x90, addr 0x00, read 8 -> DATA_RD
    rig._go()
    assert rig.nfc.read(0x150, 4) == NAND_READ_ID
    assert rig.nfc.read(0x154, 4) == 0
    rig._stage([0x38064, 0x27400, 0x59])  # cmd 0x70, delay, read 1 byte + LAST (§8.4)
    rig._go()
    assert rig.nfc.read(0x150, 4) & 0xFF == 0xC0


def test_nfc_full_au_read_with_tag() -> None:
    img = NandImage()
    payload = bytes((i * 7 + 3) & 0xFF for i in range(AU_SIZE))
    img.program(134 * BLOCK_SIZE + 3 * AU_SIZE, payload)  # block 134, AU 3
    img.set_tag(tag_key(0x8603), make_head_tag(0))
    rig = _Rig(img)
    data, tag = rig.read_au(0x8603, 0, AU_SIZE)
    assert data == payload
    assert tag == make_head_tag(0)


def test_nfc_sub_sector_read() -> None:
    """The codepage path: col = k*eccsize addresses the k-th sector of the AU."""
    img = NandImage()
    au = bytes((i >> 9) + 0x40 for i in range(AU_SIZE))  # sector index in every byte
    img.program(36 * BLOCK_SIZE, au)
    rig = _Rig(img)
    for k in (0, 1, 5, 7):
        data, _ = rig.read_au(36 << 8, k * ECCSIZE, 512, want_tag=False)
        assert data == bytes([0x40 + k]) * 512, f"sector {k}"


def test_nfc_tag_only_block_scan() -> None:
    """Mount-time scan: col = 8*eccsize, single 8-byte record -> just the tag."""
    img = NandImage()
    img.set_tag(tag_key(0x8600), make_head_tag(0))
    rig = _Rig(img)
    _, tag = rig.read_au(0x8600, 8 * ECCSIZE, 0, want_tag=True)
    assert tag == make_head_tag(0)
    # a free block answers the blank/erased tag (§4.2 mount scan -> free pool)
    _, tag = rig.read_au(0x8700, 8 * ECCSIZE, 0, want_tag=True)
    assert tag == b"\xff" * 8


def test_nfc_program_read_round_trip() -> None:
    img = NandImage()
    rig = _Rig(img)
    payload = bytes((i ^ (i >> 8)) & 0xFF for i in range(AU_SIZE))
    tag = make_head_tag(500)
    rig.program_au((614 << 8) | 4, payload, tag)
    data, rtag = rig.read_au((614 << 8) | 4, 0, AU_SIZE)
    assert data == payload
    assert rtag == tag


def test_nfc_program_round_trip_1024_byte_records() -> None:
    """The runtime FAT driver's write path: 1024-B records staged through the
    512-B circular window in two slabs with a drain poll each (Observed).
    A capture-at-strobe model sees only the window's final state and commits
    one slab duplicated — the write round-trip corruption behind 'discovery
    persists 0 games'. The record must be assembled slab by slab."""
    img = NandImage()
    rig = _Rig(img)
    payload = bytes((i * 31 + (i >> 7)) & 0xFF for i in range(AU_SIZE))
    assert payload[:512] != payload[512:1024]  # halves must be distinguishable
    tag = make_head_tag(60)
    rig.program_au((618 << 8) | 5, payload, tag, record_size=1024)
    data, rtag = rig.read_au((618 << 8) | 5, 0, AU_SIZE)
    assert data == payload
    assert rtag == tag


def test_nfc_unit_tags_do_not_alias_block_page0() -> None:
    """§4.2 raw-row tag keys: programming unit 32 of a block's row window
    (whose *data* lands in the next physical block) must not disturb that
    block's own page-0 tag — the mount scan reads it to classify the block."""
    img = NandImage()
    rig = _Rig(img)
    rig.program_au((614 << 8) | 32, b"\xa5" * AU_SIZE, make_head_tag(60))
    # data lands linearly in the next block...
    assert img.read(615 * BLOCK_SIZE, 4) == b"\xa5" * 4
    # ...but block 615's own page-0 tag row stays erased (free-pool view).
    _, tag = rig.read_au(615 << 8, 8 * ECCSIZE, 0, want_tag=True)
    assert tag == b"\xff" * 8


def test_nfc_erase_shadows_placed_content() -> None:
    img = NandImage()
    img.program(140 * BLOCK_SIZE, b"static content")
    img.set_tag(tag_key(140 << 8), make_head_tag(6))
    rig = _Rig(img)
    rig.erase(140)
    data, tag = rig.read_au(140 << 8, 0, AU_SIZE)
    assert data == b"\xff" * AU_SIZE
    assert tag == b"\xff" * 8


def test_nfc_copy_back_moves_data_and_tag() -> None:
    img = NandImage()
    payload = bytes(range(256)) * 16
    img.program(200 * BLOCK_SIZE + 2 * AU_SIZE, payload)
    img.set_tag(tag_key((200 << 8) | 2), make_head_tag(66))
    rig = _Rig(img)
    # §8.6: one GO: cmd 0x00 col+row(src) cmd 0x35 wait cmd 0x85 col+row(dst) cmd 0x10 wait+LAST
    rig._stage(
        [0x64, *rig._addr_cycles(0, (200 << 8) | 2), 0x1A864, 0x200,
         0x42864, *rig._addr_cycles(0, (201 << 8) | 2), 0x8064, 0x201]
    )
    rig._go()
    assert img.read(201 * BLOCK_SIZE + 2 * AU_SIZE, AU_SIZE) == payload
    assert img.get_tag(tag_key((201 << 8) | 2)) == make_head_tag(66)


# --- Image builder ------------------------------------------------------------------


@pytest.mark.skipif(UPD_PATH is None, reason="firmware .upd not available")
def test_image_builder_layout() -> None:
    from tt_emu.loader import load_upd
    from tt_emu.nand_image import build_nand_image

    fw = load_upd(UPD_PATH)
    img = build_nand_image(fw, b_files={"test.gme": b"GME!" * 100})

    # bins: PROG at block 8, codepage at 36 (§6 step 3) — the codepage bin is a
    # hard mount precondition (memory-map-and-boot.md §5.7).
    assert img.read(SYS_START * BLOCK_SIZE, 64) == fw.prog.data[:64]
    assert img.read(36 * BLOCK_SIZE, 64) == fw.codepage.data[:64]
    assert img.get_tag(36 * 256)[4:6] == struct.pack("<H", 28 | 0x8000)

    # zone table at row 255 (flat offset row*0x1000): fs_start + AddrCnt in AU
    zt = img.read(255 * AU_SIZE, 0x30)
    assert struct.unpack_from("<I", zt, 4)[0] == FS_START
    assert struct.unpack_from("<I", zt, 0x14)[0] == 0x3C00
    assert zt[0x1B] == 0 and zt[0x2B] == 1
    assert struct.unpack_from("<I", zt, 0x24)[0] == 0x8000

    # bin entry table at row 253: names + map rows
    entries = img.read(253 * AU_SIZE, 0x48)
    assert entries[0x14:0x18] == b"PROG"
    assert entries[0x24 + 0x14 : 0x24 + 0x1C] == b"codepage"
    assert struct.unpack_from("<I", entries, 0x08)[0] == 200
    assert struct.unpack_from("<I", entries, 0x24 + 0x08)[0] == 201

    # maplists: both u16 identical, never 0
    m = img.read(200 * AU_SIZE, 4)
    assert struct.unpack("<HH", m) == (8, 8)

    # partition A: FAT16 VBR at block 134; B at 614 with the .gme content placed
    vbr_a = img.read(FS_START * BLOCK_SIZE, 512)
    assert vbr_a[0x36:0x3E] == b"FAT16   " and vbr_a[0x1FE:] == b"\x55\xaa"
    vbr_b = img.read((FS_START + A_BLOCKS) * BLOCK_SIZE, 512)
    assert vbr_b[0x36:0x3E] == b"FAT16   "
    assert vbr_b[43:54].rstrip() == b"TIPTOI"
    # Head tag per 1-MiB NFTL span: A's first span is logical 0 at block 134,
    # B's is logical 60 at block 614; the span's interior AUs carry append-form
    # unit tags in the base row window, and only the base is a chain head.
    assert struct.unpack("<H", img.get_tag(134 << 8)[4:6])[0] == 0x8000
    assert struct.unpack("<H", img.get_tag(614 << 8)[4:6])[0] == 60 | 0x8000
    assert struct.unpack("<H", img.get_tag(614 << 8 | 1)[4:6])[0] == 60  # unit tag
    assert img.get_tag(615 << 8) == b"\xff" * 8  # follower page-0 row stays blank

    # A: is written every boot, so its placed spans' follower blocks are reserved
    # in the row-2 bad-block bitmap (1 bit/block, MSB-first) — kept out of the COW
    # free pool so the firmware's own writes cannot recycle the factory content.
    # A:'s first span (base 134) holds the FAT/VOIMG content, so 135..141 are set;
    # an unplaced block stays available (0).
    bitmap = img.read(2 * AU_SIZE, 0x1000)
    for follower in range(135, 142):
        assert bitmap[follower >> 3] & (0x80 >> (follower & 7)), follower
    unplaced = 300
    assert not bitmap[unplaced >> 3] & (0x80 >> (unplaced & 7))


# --- Boot smoke test: the mount checkpoint ------------------------------------------


@pytest.mark.skipif(UPD_PATH is None, reason="firmware .upd not available")
def test_boot_mounts_storage() -> None:
    """The storage-mount done-criteria (memory-map-and-boot.md §5.7/§5.8):

    keystone self-built (the real NFTL init ran), the codepage read succeeded
    (its active flag is set), the NAND medium is exercised (reads + the
    firmware's own COW writes into A:), and the boot runs *past* the mount into
    the event-pump / statechart. The prior NAND-read spin is gone.
    """
    from tt_emu.runner import boot_firmware

    report = boot_firmware(str(UPD_PATH), max_instructions=60_000_000)
    assert report.mount_ok, report.format_log()             # §5.8 keystone
    assert report.codepage_flag != 0, report.format_log()   # §5.7 codepage read
    assert report.nand_reads > 100, report.format_log()
    assert report.nand_programs > 0, report.format_log()    # A: COW writes
    assert report.nand_erases == 0, report.format_log()     # no bins recycled
    names = {name for _, _, name in report.checkpoints_hit}
    assert any("event-pump" in n for n in names), report.format_log()
