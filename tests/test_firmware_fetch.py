"""Tests for the firmware auto-download/cache/verify machinery.

The network is never touched: ``urlopen`` is monkeypatched with a fake
response. The SHA-256 gate, the cache behaviour and the explicit-path bypass
are exercised; the CLI wiring (optional positional, ``--firmware-cache``) is
checked at the parser level.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

import tt_emu.firmware_fetch as ff
from tt_emu.cli import build_parser

PAYLOAD = b"ANYKA106" + bytes(range(256)) * 8
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()
WRONG_SHA = "0" * 64


class FakeResponse(io.BytesIO):
    """Minimal stand-in for the object ``urlopen`` returns."""

    def __init__(self, data: bytes, content_length: bool = True) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))} if content_length else {}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def fake_urlopen(data: bytes, calls: list[str] | None = None):
    def opener(req, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if calls is not None:
            calls.append(req.full_url)
        return FakeResponse(data)

    return opener


def test_download_verifies_and_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(ff, "urlopen", fake_urlopen(PAYLOAD, calls))
    got = ff.ensure_firmware(
        None, cache_dir=tmp_path, url="https://example.test/fw.upd",
        sha256=PAYLOAD_SHA, quiet=True,
    )
    assert got == tmp_path / ff.FIRMWARE_FILENAME
    assert got.read_bytes() == PAYLOAD
    assert calls == ["https://example.test/fw.upd"]
    # No leftover temp files from the atomic download.
    assert sorted(p.name for p in tmp_path.iterdir()) == [ff.FIRMWARE_FILENAME]


def test_sha_mismatch_raises_and_keeps_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ff, "urlopen", fake_urlopen(PAYLOAD))
    with pytest.raises(ff.FirmwareIntegrityError) as exc_info:
        ff.ensure_firmware(
            None, cache_dir=tmp_path, url="https://example.test/fw.upd",
            sha256=WRONG_SHA, quiet=True,
        )
    msg = str(exc_info.value)
    assert WRONG_SHA in msg  # expected
    assert PAYLOAD_SHA in msg  # got
    assert "https://example.test/fw.upd" in msg
    # The bad download must not be left behind as a (poisoned) cache entry.
    assert list(tmp_path.iterdir()) == []


def test_cache_hit_skips_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ff.FIRMWARE_FILENAME).write_bytes(PAYLOAD)

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("network must not be touched on a cache hit")

    monkeypatch.setattr(ff, "urlopen", explode)
    got = ff.ensure_firmware(None, cache_dir=tmp_path, sha256=PAYLOAD_SHA, quiet=True)
    assert got == tmp_path / ff.FIRMWARE_FILENAME


def test_corrupt_cache_is_redownloaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ff.FIRMWARE_FILENAME).write_bytes(b"truncated garbage")
    calls: list[str] = []
    monkeypatch.setattr(ff, "urlopen", fake_urlopen(PAYLOAD, calls))
    got = ff.ensure_firmware(None, cache_dir=tmp_path, sha256=PAYLOAD_SHA, quiet=True)
    assert got.read_bytes() == PAYLOAD
    assert len(calls) == 1


def test_explicit_path_bypasses_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("explicit path must not trigger a download")

    monkeypatch.setattr(ff, "urlopen", explode)
    fw = tmp_path / "my-other-firmware.upd"
    fw.write_bytes(b"whatever the user says goes")  # no SHA gate on explicit paths
    assert ff.ensure_firmware(str(fw)) == fw


def test_download_error_is_wrapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from urllib.error import URLError

    def fail(*args: object, **kwargs: object) -> None:
        raise URLError("host unreachable")

    monkeypatch.setattr(ff, "urlopen", fail)
    with pytest.raises(ff.FirmwareDownloadError, match="host unreachable"):
        ff.ensure_firmware(None, cache_dir=tmp_path, quiet=True)
    assert list(tmp_path.iterdir()) == []


def test_url_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(ff, "urlopen", fake_urlopen(PAYLOAD, calls))
    monkeypatch.setenv("TT_EMU_FIRMWARE_URL", "https://mirror.test/fw.upd")
    ff.ensure_firmware(None, cache_dir=tmp_path, sha256=PAYLOAD_SHA, quiet=True)
    assert calls == ["https://mirror.test/fw.upd"]


def test_default_cache_dir_respects_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    if not ff.sys.platform.startswith(("linux", "freebsd")):
        pytest.skip("XDG semantics are the non-macOS/Windows branch")
    monkeypatch.setenv("XDG_CACHE_HOME", "/xdg/cache")
    assert ff.default_cache_dir() == Path("/xdg/cache/tt-emu")
    monkeypatch.delenv("XDG_CACHE_HOME")
    assert ff.default_cache_dir() == Path.home() / ".cache" / "tt-emu"


def test_cli_firmware_argument_is_optional() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert args.firmware is None
    assert args.firmware_cache is None
    args = parser.parse_args(["fw.upd", "--firmware-cache", "/tmp/cache"])
    assert args.firmware == "fw.upd"
    assert args.firmware_cache == "/tmp/cache"
