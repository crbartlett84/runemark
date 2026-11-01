"""Read what Runemark wrote into a video, with nothing but the Python standard library (Stash's Docker image has a
bare Python). Format: docs/data-format.md in the Runemark repository.

    info = read("video.mp4")      # -> dict, or None for files Runemark can't have tagged (not MP4)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess

CONTAINERS = {b"moov", b"udta", b"ilst", b"trak", b"mdia", b"minf"}
FREEFORM = b"----"


def _boxes(buf: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", buf, pos)
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield kind, pos + header, pos + size
        pos += size


def _moov(path: str) -> bytes | None:
    """The moov box's bytes (the index and tags), found without reading the media data."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        total = f.tell()
        pos = 0
        while pos + 8 <= total:
            f.seek(pos)
            head = f.read(16)
            size, kind = struct.unpack_from(">I4s", head, 0)
            header = 8
            if size == 1:
                size, header = struct.unpack_from(">Q", head, 8)[0], 16
            elif size == 0:
                size = total - pos
            if size < header:
                return None
            if kind == b"moov":
                f.seek(pos)
                return f.read(size)
            pos += size
    return None


def _child(buf: bytes, start: int, end: int, kind: bytes):
    for k, s, e in _boxes(buf, start, end):
        if k == kind:
            return s, e
    return None


def _data_value(buf: bytes, start: int, end: int):
    """The value of an ilst item: (type, bytes) of its 'data' child."""
    found = _child(buf, start, end, b"data")
    if not found:
        return None
    s, e = found
    if e - s < 8:
        return None
    kind = struct.unpack_from(">I", buf, s)[0] & 0xFFFFFF
    return kind, buf[s + 8:e]


def _xtra_rating(payload: bytes) -> int:
    """Explorer's star rating (1-99) from a Microsoft Xtra box; 0 if none."""
    pos = 0
    while pos + 8 <= len(payload):
        size, name_len = struct.unpack_from(">II", payload, pos)
        if size < 8 or pos + size > len(payload):
            break
        name = payload[pos + 8:pos + 8 + name_len]
        if name == b"WM/SharedUserRating":
            v = pos + 8 + name_len
            if v + 4 + 6 + 8 <= pos + size:
                vsize, vtype = struct.unpack_from(">IH", payload, v + 4)
                if vtype == 19:
                    return struct.unpack_from("<Q", payload, v + 10)[0]
        pos += size
    return 0


def stars(value: int) -> int:
    """Explorer's 1-99 rating as 0-5 stars (the same bands Explorer uses)."""
    if not value:
        return 0
    for s, upper in ((1, 12), (2, 37), (3, 62), (4, 87)):
        if value <= upper:
            return s
    return 5


def read(path: str) -> dict | None:
    if not path.lower().endswith((".mp4", ".m4v", ".mov")):
        return None
    moov = _moov(path)
    if moov is None:
        return None
    out = {"title": "", "date": "", "description": "", "comment": "", "keywords": [], "genre": [], "cover": None,
           "rating": 0, "data": None}
    udta = _child(moov, 8, len(moov), b"udta")
    if not udta:
        return out
    meta = _child(moov, *udta, b"meta")
    if meta:
        ilst = _child(moov, meta[0] + 4, meta[1], b"ilst")        # meta has 4 bytes of version/flags first
        if ilst:
            for kind, s, e in _boxes(moov, *ilst):
                if kind == FREEFORM:
                    mean = _child(moov, s, e, b"mean")
                    name = _child(moov, s, e, b"name")
                    value = _data_value(moov, s, e)
                    if not (mean and name and value):
                        continue
                    key = (moov[mean[0] + 4:mean[1]].decode("utf-8", "replace") + ":"
                           + moov[name[0] + 4:name[1]].decode("utf-8", "replace"))
                    text = value[1].decode("utf-8", "replace")
                    if key == "com.runemark:data":
                        try:
                            data = json.loads(text)
                            out["data"] = data if isinstance(data, dict) else None
                        except ValueError:
                            pass
                    elif key.rsplit(":", 1)[-1].casefold() == "keywords":
                        out["keywords"] += [t.strip() for t in text.split(";") if t.strip()]
                    continue
                value = _data_value(moov, s, e)
                if not value:
                    continue
                if kind == b"\xa9nam":
                    out["title"] = value[1].decode("utf-8", "replace").strip()
                elif kind == b"\xa9day":
                    out["date"] = value[1].decode("utf-8", "replace").strip()
                elif kind == b"desc":
                    out["description"] = value[1].decode("utf-8", "replace").strip()
                elif kind == b"\xa9cmt":
                    out["comment"] = value[1].decode("utf-8", "replace").strip()
                elif kind == b"\xa9gen":
                    out["genre"] = [t.strip() for t in value[1].decode("utf-8", "replace").split(";") if t.strip()]
                elif kind == b"covr" and value[1]:
                    out["cover"] = (value[1], "image/png" if value[0] == 14 else "image/jpeg")
    xtra = _child(moov, *udta, b"Xtra")
    if xtra:
        out["rating"] = stars(_xtra_rating(moov[xtra[0]:xtra[1]]))
    out["description"] = out["description"] or out["comment"]
    return out


def chapters(path: str, ffprobe: str | None = None) -> list[dict]:
    """[{start, end, title}] read with ffprobe (Stash's image has it); [] when ffprobe isn't there."""
    tool = ffprobe or shutil.which("ffprobe") or next(
        (p for p in ("/usr/bin/ffprobe", "/usr/local/bin/ffprobe", "/root/.stash/ffprobe") if os.path.exists(p)), None)
    if not tool:
        return []
    try:
        result = subprocess.run([tool, "-v", "error", "-show_chapters", "-of", "json", path], capture_output=True,
                                timeout=60, check=True)
        found = json.loads(result.stdout.decode("utf-8", "replace") or "{}").get("chapters", [])
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    out = []
    for c in found:
        try:
            out.append({"start": float(c["start_time"]), "end": float(c["end_time"]),
                        "title": (c.get("tags") or {}).get("title", "").strip()})
        except (KeyError, ValueError):
            continue
    return out


def date_of(text: str, year_only_as_jan_1: bool = False) -> str:
    """A Stash date (YYYY-MM-DD) from ©day, which may be a full date, a timestamp or just a year."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text or "")
    if m:
        return m.group(0)
    m = re.match(r"(\d{4})", text or "")
    if m and year_only_as_jan_1:
        return f"{m.group(1)}-01-01"
    return ""


def hand_and_profile_tags(info: dict) -> tuple[list[str], list[str]]:
    """(tags typed by hand, tags that came from people's profiles), per the data block."""
    data = info.get("data") or {}
    profile = data.get("profile_tags", []) if data.get("format") == "runemark-data" else []
    also = {t.casefold() for t in data.get("also_hand", [])}
    given = {t.casefold() for t in profile} - also
    hand = [t for t in info["keywords"] if t.casefold() not in given]
    return hand, profile


def people(info: dict) -> list[dict]:
    data = info.get("data") or {}
    if data.get("format") != "runemark-data":
        return []
    return [p for p in data.get("people", []) if isinstance(p, dict) and p.get("name")]
