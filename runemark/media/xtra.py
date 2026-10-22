"""Read and write Explorer's "Tags" field in MP4 files without Windows.

Windows keeps the Tags you see in File Explorer (System.Keywords) for MP4 files as a
"WM/Category" entry inside a Microsoft "Xtra" box at moov/udta/Xtra. The layout below
follows ExifTool's Microsoft.pm, which has read and written these boxes since 2021:

  Xtra payload: a run of entries
    entry: size (u32 BE, whole entry) | name length (u32 BE) | name (ASCII) | values
    values: count (u32 BE), then per value:
            size (u32 BE, includes these 6 header bytes) | type (u16 BE) | data
    type 8 = text: UTF-16LE with a terminating NUL

Runemark normally lets Windows write this through its own property handler and uses this
module as the fallback and on other systems. Writes happen in place: the new box takes its
room from the padding ('free' box) mutagen keeps beside the tag list, so moov never changes
size and no media data moves.
"""
from __future__ import annotations

import os
import struct

from .atoms import Atom, MalformedMP4, iter_atoms, iter_atoms_bytes, render, top_level

CATEGORY = "WM/Category"
RATING = "WM/SharedUserRating"      # Explorer's star rating: 1, 25, 50, 75 or 99
TYPE_TEXT = 8
TYPE_UINT64 = 19                    # little-endian, like every value inside Xtra


class XtraFormatError(ValueError):
    pass


class NeedsPadding(RuntimeError):
    def __init__(self, needed: int):
        self.needed = max(0, needed)
        super().__init__(f"needs {self.needed} more bytes of padding in the MP4 tag area")


def encode_text(value: str) -> bytes:
    return value.encode("utf-16-le") + b"\x00\x00"


def decode_text(data: bytes) -> str:
    text = data.decode("utf-16-le", "replace")
    return text.rstrip("\x00")


def encode_entry(name: str, values: list) -> bytes:
    """One Xtra entry. Values are text (str) or unsigned 64-bit numbers (int)."""
    body = struct.pack(">I", len(values))
    for value in values:
        if isinstance(value, int):
            data, kind = struct.pack("<Q", value), TYPE_UINT64
        else:
            data, kind = encode_text(value), TYPE_TEXT
        body += struct.pack(">IH", len(data) + 6, kind) + data
    raw_name = name.encode("ascii")
    return struct.pack(">II", 8 + len(raw_name) + len(body), len(raw_name)) + raw_name + body


def parse_entries(payload: bytes) -> list[tuple[str, bytes, bytes]]:
    """Split an Xtra payload into (name, raw entry bytes, value bytes)."""
    entries = []
    pos = 0
    while pos + 8 <= len(payload):
        size, name_len = struct.unpack_from(">II", payload, pos)
        if size < 8 or pos + size > len(payload) or name_len + 8 > size:
            raise XtraFormatError(f"bad Xtra entry at offset {pos}")
        name = payload[pos + 8: pos + 8 + name_len].decode("latin-1")
        entries.append((name, payload[pos: pos + size], payload[pos + 8 + name_len: pos + size]))
        pos += size
    if pos != len(payload) and payload[pos:].strip(b"\x00"):
        raise XtraFormatError("trailing bytes after the last Xtra entry")
    return entries


def decode_values(block: bytes) -> list:
    if len(block) < 4:
        return []
    count = struct.unpack_from(">I", block, 0)[0]
    values = []
    pos = 4
    for _ in range(count):
        if pos + 6 > len(block):
            break
        size, kind = struct.unpack_from(">IH", block, pos)
        if size < 6 or pos + size > len(block):
            break
        data = block[pos + 6: pos + size]
        if kind == TYPE_TEXT:
            values.append(decode_text(data))
        elif kind == TYPE_UINT64 and len(data) == 8:
            values.append(struct.unpack("<Q", data)[0])
        else:
            values.append(data)
        pos += size
    return values


def category_from_payload(payload: bytes) -> list[str]:
    for name, _raw, block in parse_entries(payload):
        if name == CATEGORY:
            return [v for v in decode_values(block) if isinstance(v, str) and v.strip()]
    return []


def rating_from_payload(payload: bytes) -> int:
    for name, _raw, block in parse_entries(payload):
        if name == RATING:
            numbers = [v for v in decode_values(block) if isinstance(v, int)]
            return numbers[0] if numbers else 0
    return 0


def replace_entries(payload: bytes, updates: dict[str, list]) -> bytes:
    """New Xtra payload with the named entries replaced (an empty list removes the entry).
    Entries not named in `updates` are kept exactly as they are."""
    out = b""
    done: set[str] = set()
    for name, raw, _block in parse_entries(payload) if payload else []:
        if name in updates:
            if updates[name] and name not in done:
                out += encode_entry(name, updates[name])
            done.add(name)
        else:
            out += raw
    for name, values in updates.items():
        if values and name not in done:
            out += encode_entry(name, values)
    return out


def replace_category(payload: bytes, values: list[str]) -> bytes:
    """New Xtra payload with WM/Category set to `values`; other entries kept as they are."""
    return replace_entries(payload, {CATEGORY: list(values)})


# --------------------------------------------------------------------------
# File access
# --------------------------------------------------------------------------

def _locate(f) -> tuple[Atom, Atom | None]:
    tops = top_level(f)
    moovs = [a for a in tops if a.type == b"moov"]
    if len(moovs) != 1:
        raise MalformedMP4("expected exactly one moov box")
    moov = moovs[0]
    udta = None
    for atom in iter_atoms(f, moov.data_offset, moov.end):
        if atom.type == b"udta":
            udta = atom
            break
    return moov, udta


def read_payload(path: str | os.PathLike) -> bytes:
    """The raw Xtra payload (empty when the file has none)."""
    with open(path, "rb") as f:
        _moov, udta = _locate(f)
        if udta is None:
            return b""
        for atom in iter_atoms(f, udta.data_offset, udta.end):
            if atom.type == b"Xtra":
                f.seek(atom.data_offset)
                return f.read(atom.size - atom.header)
    return b""


def read_category(path: str | os.PathLike) -> list[str]:
    """Explorer's Tags as stored in the file (empty list if there are none)."""
    return category_from_payload(read_payload(path))


def read_rating(path: str | os.PathLike) -> int:
    """Explorer's rating as stored in the file: 0 (none) or 1-99."""
    return rating_from_payload(read_payload(path))


def write_category(path: str | os.PathLike, values: list[str]) -> None:
    """Set Explorer's Tags in place. Raises NeedsPadding if the tag area has no room."""
    write_entries(path, {CATEGORY: list(values)})


def write_entries(path: str | os.PathLike, updates: dict[str, list]) -> None:
    """Replace Xtra entries in place. Raises NeedsPadding if the tag area has no room."""
    with open(path, "r+b") as f:
        _moov, udta = _locate(f)
        if udta is None:
            raise NeedsPadding(4096)
        f.seek(udta.offset)
        ubuf = f.read(udta.size)
        if udta.header != 8:
            raise MalformedMP4("64-bit udta boxes aren't supported")
        kids = list(iter_atoms_bytes(ubuf, udta.header, len(ubuf)))
        tail_start = kids[-1].end if kids else udta.header
        tail = ubuf[tail_start:]  # QuickTime sometimes ends udta with a 4-byte zero terminator

        meta = ilst = free = None
        for kid in kids:
            if kid.type != b"meta":
                continue
            inner = list(iter_atoms_bytes(ubuf, kid.offset + kid.header + 4, kid.end))
            for i, child in enumerate(inner):
                if child.type == b"ilst":
                    meta, ilst = kid, child
                    for j in (i + 1, i - 1):
                        if 0 <= j < len(inner) and inner[j].type == b"free":
                            free = inner[j]
                            break
                    break
            if meta:
                break
        if meta is None or ilst is None:
            raise NeedsPadding(4096)

        old_xtras = [k for k in kids if k.type == b"Xtra"]
        old_payload = b""
        if old_xtras:
            first = old_xtras[0]
            old_payload = ubuf[first.data_offset: first.end]
        new_payload = replace_entries(old_payload, updates)
        new_xtra = render(b"Xtra", new_payload) if new_payload else b""
        delta = len(new_xtra) - sum(k.size for k in old_xtras)

        free_size = free.size if free is not None else 0
        if free is not None and free.header != 8:
            raise MalformedMP4("64-bit free boxes aren't supported")
        if meta.header != 8:
            raise MalformedMP4("64-bit meta boxes aren't supported")
        new_free_size = free_size - delta
        if delta == 0 and new_xtra == b"".join(ubuf[k.offset:k.end] for k in old_xtras):
            return  # nothing to change
        if free is None or new_free_size < 8:
            raise NeedsPadding(delta - free_size + 8 + 1024)

        meta_body = ubuf[meta.offset + meta.header: free.offset] \
            + render(b"free", b"\x00" * (new_free_size - 8)) \
            + ubuf[free.end: meta.end]
        new_meta = struct.pack(">I4s", 8 + len(meta_body), b"meta") + meta_body

        parts = []
        for kid in kids:
            if kid.type == b"Xtra":
                continue
            if kid is meta:
                parts.append(new_meta)
                parts.append(new_xtra)
            else:
                parts.append(ubuf[kid.offset: kid.end])
        new_ubuf = ubuf[: udta.header] + b"".join(parts) + tail
        if len(new_ubuf) != len(ubuf):
            raise AssertionError("udta size changed while rewriting Xtra")
        f.seek(udta.offset)
        f.write(new_ubuf)
        f.flush()
        os.fsync(f.fileno())
