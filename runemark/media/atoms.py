"""Just enough MP4 box parsing to find moov/udta/meta and edit in place safely."""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import BinaryIO, Iterator


class MalformedMP4(ValueError):
    pass


@dataclass
class Atom:
    type: bytes
    offset: int   # where the header starts
    size: int     # header + payload
    header: int   # 8, or 16 for 64-bit sizes

    @property
    def data_offset(self) -> int:
        return self.offset + self.header

    @property
    def end(self) -> int:
        return self.offset + self.size

    @property
    def name(self) -> str:
        return self.type.decode("latin-1")


def iter_atoms(fileobj: BinaryIO, start: int, end: int) -> Iterator[Atom]:
    pos = start
    while pos + 8 <= end:
        fileobj.seek(pos)
        head = fileobj.read(8)
        if len(head) < 8:
            return
        size, kind = struct.unpack(">I4s", head)
        header = 8
        if size == 1:
            big = fileobj.read(8)
            if len(big) < 8:
                raise MalformedMP4("truncated 64-bit box size")
            size = struct.unpack(">Q", big)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise MalformedMP4(f"bad size for '{kind!r}' box at {pos}")
        yield Atom(kind, pos, size, header)
        pos += size


def iter_atoms_bytes(buf: bytes, start: int, end: int) -> Iterator[Atom]:
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", buf, pos)
        header = 8
        if size == 1:
            if pos + 16 > end:
                raise MalformedMP4("truncated 64-bit box size")
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise MalformedMP4(f"bad size for '{kind!r}' box at {pos}")
        yield Atom(kind, pos, size, header)
        pos += size


def render(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def top_level(fileobj: BinaryIO) -> list[Atom]:
    fileobj.seek(0, os.SEEK_END)
    size = fileobj.tell()
    return list(iter_atoms(fileobj, 0, size))


@dataclass
class Layout:
    moov: Atom
    moov_before_media: bool  # True when mdat comes after moov (editing moov may shift media)
    ilst_size: int | None
    free_size: int | None    # 'free' box mutagen keeps next to ilst (its padding)


def read_layout(path: str | os.PathLike) -> Layout:
    with open(path, "rb") as f:
        tops = top_level(f)
        moovs = [a for a in tops if a.type == b"moov"]
        if len(moovs) != 1:
            raise MalformedMP4("expected exactly one moov box")
        moov = moovs[0]
        media_after = any(a.type == b"mdat" and a.offset > moov.offset for a in tops)
        ilst_size = free_size = None
        try:
            udta = _child(f, moov, b"udta")
            meta = _child(f, udta, b"meta", skip=0) if udta else None
            if meta:
                kids = list(iter_atoms(f, meta.data_offset + 4, meta.end))
                for i, kid in enumerate(kids):
                    if kid.type == b"ilst":
                        ilst_size = kid.size
                        for j in (i + 1, i - 1):
                            if 0 <= j < len(kids) and kids[j].type == b"free":
                                free_size = kids[j].size
                                break
                        break
        except MalformedMP4:
            pass
        return Layout(moov, media_after, ilst_size, free_size)


def _child(fileobj: BinaryIO, parent: Atom, kind: bytes, skip: int = 0) -> Atom | None:
    for atom in iter_atoms(fileobj, parent.data_offset + skip, parent.end):
        if atom.type == kind:
            return atom
    return None
