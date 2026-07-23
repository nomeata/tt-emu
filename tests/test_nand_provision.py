"""Provision-via-producer seam (:mod:`tt_emu.nand_provision`).

The reusable NAND-provisioning path runs the firmware's own ``producer.bin`` to
format an authentic image. Validated here against the MT producer, whose output
is pinned by ``firmware-re``'s reference ``producer_nand.img`` — so a faithful
port reproduces it byte-for-byte. ZC3201 has no reference output yet (its
``ProducerProfile`` is filled as the 1st-gen bring-up lands); this guards the
seam MT and ZC3201 share.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _data import firmware_path

from tt_emu.firmware_profile import MT
from tt_emu.loader import load_upd
from tt_emu.nand_provision import WritableNand, run_producer

# Legacy reference image from the (retired) RE workspace, now archived. Skipif-guarded
# on existence, so a fresh checkout without it just skips this comparison test.
_REF_IMG = Path(
    "/home/jojo/tiptoi/firmware-re-ARCHIVE/fw/2N-update3202MT/data/producer_nand.img"
)


def test_writable_nand_cache_roundtrip() -> None:
    n = WritableNand()
    n.program(6, 0, b"ABCD", b"\x12\x12\x12\x12")
    n.program(5, 63, b"\xaa" * 2048, b"\xe2\x05\x00\x00")
    n.erase(3)
    m = WritableNand.deserialize(n.serialize())
    assert m.data.keys() == n.data.keys()
    assert m.spare == n.spare
    # au = page // 2 keying, page-0 metadata magic reachable as the block's tag.
    img = m.to_nand_image()
    assert img.get_tag(6 << 8 | 0)[:4] == b"\x12\x12\x12\x12"


@pytest.mark.skipif(firmware_path() is None, reason="MT firmware .upd not available")
def test_mt_producer_formats_metadata() -> None:
    """Running the MT producer formats the ASA + block-0 metadata (cmds 5/7/9/0x22)."""
    upd = firmware_path()
    assert upd is not None
    fw = load_upd(str(upd))
    assert MT.producer is not None
    nand = run_producer(fw.producer.data, upd.read_bytes(), MT.producer, b_mb=64)

    # The producer touches the ASA blocks (1-5, +6 spare) and block 0 metadata.
    assert set(nand.data).issuperset({0, 1, 2, 3, 4, 5, 6})
    # Block-0 metadata magics land in its page spare tags (map/bin-info/zone table).
    magics = {t[:4].hex() for (blk, _pg), t in nand.spare.items() if blk == 0}
    for magic in ("12121212", "34343434", "56565656", "5a5a5a5a"):
        assert magic in magics, f"missing metadata magic {magic} (got {sorted(magics)})"


@pytest.mark.skipif(
    firmware_path() is None or not _REF_IMG.exists(),
    reason="MT firmware / firmware-re reference producer_nand.img not available",
)
def test_mt_producer_matches_reference_image() -> None:
    """The port reproduces firmware-re's reference producer image byte-for-byte."""
    upd = firmware_path()
    assert upd is not None
    fw = load_upd(str(upd))
    assert MT.producer is not None
    nand = run_producer(fw.producer.data, upd.read_bytes(), MT.producer, b_mb=64)
    raw = nand.raw_pages()
    ref = _REF_IMG.read_bytes()
    assert len(raw) == len(ref)
    assert raw == ref
