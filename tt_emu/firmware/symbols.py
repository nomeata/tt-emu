"""Symbol sources for the firmware debugger: the GME container and a tttool YAML.

Two independent, join-able symbol tables (``docs/firmware-2n-mt.md`` §5):

* :class:`GmeScripts` — the **GME container itself** (game-data format, not
  firmware RE): the play-script table gives the content-OID range and, per
  OID, the script's lines (conditions, actions, playlist) exactly as the
  firmware's interpreter parses them. Used to recover *which line* the
  interpreter's RAM-resident parsed-line state corresponds to, and to render
  a routed script even without a YAML.
* :class:`TttoolSymbols` — the **tttool source ``.yaml``** (plus its sibling
  ``*.codes.yaml``): the product id, script names ↔ OID codes, ``$``-register
  names, per-line source text, and (derived) media names.

Joins to live firmware state:

* ``product-id`` ↔ the mounted product id — this is how the debugger knows the
  YAML applies to the mounted GME;
* script name ↔ OID code via the codes file (numeric script keys are codes
  directly);
* YAML line order == the firmware's line-offset array order, so a matched
  binary line index names the YAML source line;
* named registers: tttool numbers named registers deterministically — the
  names **sorted ascending** map to register-file indices 0…n−1 (§5 marks the
  rule tttool-internal; it is verified against the real GME in the test
  suite, and every register display degrades to ``$N`` when unnamed);
* media names: recovered by aligning each YAML line's ``P(...)`` arguments
  with the same line's binary playlist entries (both are in source order).

The YAML reader is a minimal, targeted parser for the tttool subset (top-level
scalars, one nested block, block lists, comments) — no external YAML
dependency.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "GmeLine",
    "GmeScripts",
    "TttoolSymbols",
    "derive_media_names",
    "load_tttool_yaml",
    "parse_min_yaml",
]

#: GME header: product/book OID code at file offset 0x14 (game-data format).
GME_PRODUCT_CODE_OFFSET = 0x14
#: Upper bound of the product-OID band (content OIDs are greater).
PRODUCT_OID_MAX = 0x3E7

_REGISTER_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
_PLAY_RE = re.compile(r"\bP[A*]?\(([^)]*)\)")


# --- The GME play-script table (game-data format) ---------------------------------------


@dataclass(frozen=True)
class GmeLine:
    """One script line as stored in the GME container.

    Encodings (verified against a tttool-assembled GME):

    * condition: ``u8 lhs_is_const, u16 lhs, u16 op, u8 rhs_is_const, u16 rhs``
      (8 bytes; a non-const side is a register index);
    * action: ``u16 register, u16 opcode, u8 is_const, u16 operand`` (7 bytes);
    * playlist: ``u16 count`` + ``u16`` media indices.
    """

    conds: tuple[tuple[int, int, int, int, int], ...]
    actions: tuple[tuple[int, int, int, int], ...]
    playlist: tuple[int, ...]


class GmeScripts:
    """Read-only view of a ``.gme`` file's play-script table.

    ``u32@0`` points at the table = ``u32 last_oid, u32 first_oid`` followed by
    one ``u32`` script offset per code (``0xFFFFFFFF`` = no script). Raises
    :class:`ValueError` if the table is implausible.
    """

    def __init__(self, data: bytes) -> None:
        self.data = data
        (self.product,) = struct.unpack_from("<I", data, GME_PRODUCT_CODE_OFFSET)
        (table,) = struct.unpack_from("<I", data, 0)
        last, first = struct.unpack_from("<II", data, table)
        count = last - first + 1
        if not (PRODUCT_OID_MAX < first <= last <= 0x3FFFF and count <= 20_000):
            raise ValueError(f"implausible GME script table ({first}..{last})")
        self.first_oid = first
        self.last_oid = last
        self._offsets: tuple[int, ...] = struct.unpack_from(f"<{count}I", data, table + 8)
        self._cache: dict[int, tuple[GmeLine, ...] | None] = {}
        self._media_table: tuple[tuple[int, int], ...] | None = None

    @property
    def media_table(self) -> tuple[tuple[int, int], ...]:
        """The media-file table: one ``(file_offset, byte_size)`` per media index.

        ``u32@4`` points at the table, a packed list of ``u32 offset, u32 size``
        pairs; the entry count is implied — the table ends where the first
        media file begins (the same rule tttool uses). Empty on any
        implausibility (the join is best-effort, like the YAML one).
        """
        if self._media_table is None:
            self._media_table = self._parse_media_table()
        return self._media_table

    def _parse_media_table(self) -> tuple[tuple[int, int], ...]:
        data = self.data
        try:
            (table,) = struct.unpack_from("<I", data, 4)
            (first_file,) = struct.unpack_from("<I", data, table)
            count = (first_file - table) // 8
            if not (0 < count <= 20_000 and first_file <= len(data)):
                return ()
            entries = struct.unpack_from(f"<{2 * count}I", data, table)
        except struct.error:
            return ()
        pairs = tuple(zip(entries[0::2], entries[1::2]))
        if any(off + size > len(data) for off, size in pairs):
            return ()
        return pairs

    def script(self, oid: int) -> tuple[GmeLine, ...] | None:
        """The script lines for a content OID; None if absent/out of range."""
        if not self.first_oid <= oid <= self.last_oid:
            return None
        if oid not in self._cache:
            try:
                self._cache[oid] = self._parse(self._offsets[oid - self.first_oid])
            except (struct.error, IndexError, ValueError):
                self._cache[oid] = None
        return self._cache[oid]

    def _parse(self, offset: int) -> tuple[GmeLine, ...] | None:
        if offset == 0xFFFFFFFF:
            return None
        data = self.data
        (nlines,) = struct.unpack_from("<H", data, offset)
        if nlines > 1000:
            raise ValueError("implausible line count")
        line_offsets = struct.unpack_from(f"<{nlines}I", data, offset + 2)
        lines: list[GmeLine] = []
        for lo in line_offsets:
            pos = lo
            (nconds,) = struct.unpack_from("<H", data, pos)
            pos += 2
            conds = tuple(
                struct.unpack_from("<BHHBH", data, pos + 8 * i) for i in range(nconds)
            )
            pos += 8 * nconds
            (nacts,) = struct.unpack_from("<H", data, pos)
            pos += 2
            actions = tuple(
                struct.unpack_from("<HHBH", data, pos + 7 * i) for i in range(nacts)
            )
            pos += 7 * nacts
            (nplay,) = struct.unpack_from("<H", data, pos)
            pos += 2
            playlist = struct.unpack_from(f"<{nplay}H", data, pos)
            if nconds > 64 or nacts > 64 or nplay > 256:
                raise ValueError("implausible line")
            lines.append(GmeLine(conds=conds, actions=actions, playlist=tuple(playlist)))
        return tuple(lines)


# --- Minimal tttool-YAML reader -----------------------------------------------------------


def _tokenize(text: str) -> list[tuple[int, str]]:
    """(indent, content) per significant line; comments and blanks dropped."""
    out: list[tuple[int, str]] = []
    for raw in text.splitlines():
        line = raw.expandtabs()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        cut = line.find(" #")  # trailing comment (tttool values never quote '#')
        if cut != -1:
            line = line[:cut]
            if not line.strip():
                continue
        indent = len(line) - len(line.lstrip())
        out.append((indent, line.strip()))
    return out


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def parse_min_yaml(text: str) -> dict[str, object]:
    """Parse the tttool YAML subset: nested mappings of scalars and block lists.

    Handles exactly what tttool books use — ``key: value`` scalars, ``key:``
    followed by a deeper mapping or by ``- item`` lists (which YAML permits at
    the *same* indent as the key), and ``#`` comments. Anything fancier is out
    of scope; parse errors surface as missing keys, never exceptions.
    """
    tokens = _tokenize(text)
    pos = 0

    def parse_map(indent: int) -> dict[str, object]:
        nonlocal pos
        mapping: dict[str, object] = {}
        while pos < len(tokens):
            ind, content = tokens[pos]
            if ind != indent or content.startswith("- "):
                break
            key, sep, rest = content.partition(":")
            if not sep:
                pos += 1  # not a mapping entry; skip
                continue
            key = _unquote(key.strip())
            rest = rest.strip()
            pos += 1
            if rest:
                mapping[key] = _unquote(rest)
                continue
            # Empty value: a list (at indent >= key) or a deeper mapping.
            if (
                pos < len(tokens)
                and tokens[pos][1].startswith("- ")
                and tokens[pos][0] >= indent
            ):
                item_indent = tokens[pos][0]
                items: list[str] = []
                while (
                    pos < len(tokens)
                    and tokens[pos][0] == item_indent
                    and tokens[pos][1].startswith("- ")
                ):
                    items.append(_unquote(tokens[pos][1][2:].strip()))
                    pos += 1
                mapping[key] = items
            elif pos < len(tokens) and tokens[pos][0] > indent:
                mapping[key] = parse_map(tokens[pos][0])
            else:
                mapping[key] = ""
        return mapping

    return parse_map(tokens[0][0]) if tokens else {}


# --- tttool symbols ------------------------------------------------------------------------


@dataclass
class TttoolSymbols:
    """Symbolic names from a tttool ``.yaml``, joinable to live firmware state."""

    path: str = ""
    product_id: int | None = None
    comment: str = ""
    welcome: str = ""
    init: str = ""
    #: script name -> source lines (YAML order == firmware line order, §5).
    scripts: dict[str, list[str]] = field(default_factory=dict)
    #: script name -> OID code (codes file / embedded scriptcodes / numeric keys).
    codes: dict[str, int] = field(default_factory=dict)
    #: register-file index -> register name (names sorted ascending, §5).
    register_names: tuple[str, ...] = ()
    #: media-table index -> media name (derived; may be empty).
    media_names: dict[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.labels: dict[int, str] = {code: name for name, code in self.codes.items()}

    def register_name(self, index: int) -> str:
        """``$name`` when known, ``$index`` otherwise (graceful degrade)."""
        if 0 <= index < len(self.register_names):
            return f"${self.register_names[index]}"
        return f"${index}"

    def oid_label(self, oid: int) -> str | None:
        """The script name assembled at this OID code, if known."""
        return self.labels.get(oid)

    def script_source(self, oid: int, line: int) -> str | None:
        """The YAML source text of script line ``line`` of the script at ``oid``."""
        name = self.labels.get(oid)
        if name is None:
            return None
        lines = self.scripts.get(name)
        if lines is None or not 0 <= line < len(lines):
            return None
        return lines[line]

    def media_name(self, index: int) -> str:
        return self.media_names.get(index, f"media {index}")


def load_tttool_yaml(path: str | Path) -> TttoolSymbols:
    """Load a tttool book YAML (+ its sibling ``*.codes.yaml`` if present)."""
    path = Path(path)
    doc = parse_min_yaml(path.read_text(encoding="utf-8"))

    scripts: dict[str, list[str]] = {}
    raw_scripts = doc.get("scripts")
    if isinstance(raw_scripts, dict):
        for name, value in raw_scripts.items():
            if isinstance(value, list):
                scripts[name] = [str(v) for v in value]
            else:
                scripts[name] = [str(value)] if str(value) else []

    # Script name -> OID code: numeric keys are codes directly; otherwise the
    # embedded ``scriptcodes:`` block or the sibling codes file supplies them.
    codes: dict[str, int] = {}
    for source in (doc.get("scriptcodes"), _sibling_codes(path)):
        if isinstance(source, dict):
            for name, value in source.items():
                try:
                    codes[str(name)] = int(str(value), 0)
                except ValueError:
                    pass
    for name in scripts:
        if name not in codes and name.isdigit():
            codes[name] = int(name)

    # Named registers: every $name in init + script lines, sorted ascending.
    names: set[str] = set(_REGISTER_RE.findall(str(doc.get("init", ""))))
    for lines in scripts.values():
        for line in lines:
            names.update(_REGISTER_RE.findall(line))

    product_id: int | None = None
    try:
        product_id = int(str(doc.get("product-id", "")), 0)
    except ValueError:
        pass

    return TttoolSymbols(
        path=str(path),
        product_id=product_id,
        comment=str(doc.get("comment", "")),
        welcome=str(doc.get("welcome", "")),
        init=str(doc.get("init", "")),
        scripts=scripts,
        codes=codes,
        register_names=tuple(sorted(names)),
    )


def _sibling_codes(path: Path) -> dict[str, object] | None:
    """The ``scriptcodes:`` mapping of the sibling ``<book>.codes.yaml``, if any."""
    codes_path = path.with_suffix(".codes.yaml")
    if not codes_path.exists():
        return None
    try:
        doc = parse_min_yaml(codes_path.read_text(encoding="utf-8"))
    except OSError:
        return None
    block = doc.get("scriptcodes")
    return block if isinstance(block, dict) else None


def derive_media_names(symbols: TttoolSymbols, gme: GmeScripts) -> dict[int, str]:
    """Media-table index -> name, by aligning YAML ``P(...)`` args with binary playlists.

    For every script whose YAML line count matches the binary line count, the
    ``P(...)``/``P*(...)`` arguments of a YAML line (in source order) name that
    line's binary playlist entries (same order). Conflicting witnesses drop
    the entry rather than guess.
    """
    names: dict[int, str] = {}
    conflicts: set[int] = set()
    for name, lines in symbols.scripts.items():
        code = symbols.codes.get(name)
        if code is None:
            continue
        blines = gme.script(code)
        if blines is None or len(blines) != len(lines):
            continue
        for text, bline in zip(lines, blines):
            args = [
                arg.strip()
                for group in _PLAY_RE.findall(text)
                for arg in group.split(",")
                if arg.strip()
            ]
            if len(args) != len(bline.playlist):
                continue
            for media_name, index in zip(args, bline.playlist):
                if names.setdefault(index, media_name) != media_name:
                    conflicts.add(index)
    for index in conflicts:
        names.pop(index, None)
    return names
