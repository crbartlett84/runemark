"""Tags, genre and cover art inside MP4 files (the MP4 equivalent of ID3), via mutagen."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Iterable

from mutagen.mp4 import MP4, AtomDataType, MP4Cover, MP4FreeForm

from .atoms import read_layout

log = logging.getLogger(__name__)

KEYWORDS_KEY = "----:com.apple.iTunes:KEYWORDS"
GENRE_KEY = "\xa9gen"
COVER_KEY = "covr"
TITLE_KEY = "\xa9nam"
YEAR_KEY = "\xa9day"
DESCRIPTION_KEYS = ("desc", "\xa9cmt")   # Plex/Jellyfin read desc; Explorer shows ©cmt as Comments
SEPARATOR = "; "
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

MIN_PADDING = 32 * 1024      # room kept beside the tag list for future edits and Explorer's Tags
KEEP_PADDING_ABOVE = 8 * 1024


class _Unchanged:
    def __repr__(self) -> str:
        return "UNCHANGED"


UNCHANGED = _Unchanged()


@dataclass
class VideoMeta:
    """Title, year, description and star rating (0 = not rated, 1-5 stars)."""
    title: str = ""
    year: int | None = None
    description: str = ""
    rating: int = 0

    def key(self) -> tuple:
        return (self.title.strip(), self.year, self.description.strip(), self.rating)


# Explorer stores ratings as 1-99; these are the values it writes for 1 to 5 stars
STAR_VALUES = {0: 0, 1: 1, 2: 25, 3: 50, 4: 75, 5: 99}


def stars_to_value(stars: int) -> int:
    return STAR_VALUES.get(max(0, min(5, int(stars))), 0)


def value_to_stars(value: int | None) -> int:
    if not value or value <= 0:
        return 0
    for stars, upper in ((1, 12), (2, 37), (3, 62), (4, 87)):
        if value <= upper:
            return stars
    return 5


def parse_year(text) -> int | None:
    import re
    match = re.search(r"(1[89]|2[0-9])\d\d", str(text or ""))
    return int(match.group(0)) if match else None


@dataclass
class Mp4Tags:
    keywords: list[str]
    genre: list[str]
    cover: bytes | None
    title: str = ""
    year: int | None = None
    description: str = ""


def split_values(values: Iterable[str]) -> list[str]:
    """Split 'a; b; c' style values and drop blanks and case-insensitive duplicates."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in str(value).split(";"):
            part = " ".join(part.split())
            key = part.casefold()
            if part and key not in seen:
                seen.add(key)
                out.append(part)
    return out


def _is_keywords_key(key: str) -> bool:
    return key.startswith("----:") and key.rsplit(":", 1)[-1].casefold() == "keywords"


def read_mp4_tags(path: str | os.PathLike, mp4: MP4 | None = None) -> Mp4Tags:
    """Pass an already-parsed `mp4` to avoid reading the file again (the library scan does)."""
    mp4 = mp4 if mp4 is not None else MP4(path)
    tags = mp4.tags or {}
    keywords: list[str] = []
    for key in tags.keys():
        if _is_keywords_key(key):
            for value in tags[key]:
                keywords.append(bytes(value).decode("utf-8", "replace"))
    genre = [str(v) for v in tags.get(GENRE_KEY, [])]
    if not genre and "gnre" in tags:
        try:
            from mutagen._constants import GENRES
            genre = [GENRES[int(n) - 1] for n in tags["gnre"] if 0 < int(n) <= len(GENRES)]
        except Exception:  # noqa: BLE001
            genre = []
    covers = tags.get(COVER_KEY, [])
    cover = bytes(covers[0]) if covers else None
    title = str(tags.get(TITLE_KEY, [""])[0]) if tags.get(TITLE_KEY) else ""
    year = parse_year(tags.get(YEAR_KEY, [""])[0]) if tags.get(YEAR_KEY) else None
    description = ""
    for key in DESCRIPTION_KEYS:
        if tags.get(key):
            description = str(tags[key][0])
            break
    return Mp4Tags(split_values(keywords), split_values(genre), cover, title, year, description)


def _padding_func(min_padding: int, ensure_padding: int = KEEP_PADDING_ABOVE):
    keep_above = max(KEEP_PADDING_ABOVE, ensure_padding)

    def choose(info) -> int:
        if keep_above <= info.padding <= max(min_padding * 32, 4 * 1024 * 1024):
            return info.padding  # fits with room to spare: write in place, nothing moves
        return max(min_padding, keep_above, info.get_default_padding())
    return choose


def write_mp4_tags(path: str | os.PathLike, *, keywords: list[str], genre: list[str],
                   cover: bytes | None | _Unchanged = UNCHANGED, min_padding: int = MIN_PADDING,
                   ensure_padding: int = KEEP_PADDING_ABOVE, meta: VideoMeta | _Unchanged = UNCHANGED) -> None:
    """Write keywords, genre and (optionally) cover art and title/year/description.
    cover=None or b'' removes the cover. The star rating lives in Explorer's Xtra box (explorer.py).

    ensure_padding: leave at least this much free room beside the tag list afterwards.
    """
    mp4 = MP4(path)
    if mp4.tags is None:
        mp4.add_tags()
    tags = mp4.tags
    for key in [k for k in tags.keys() if _is_keywords_key(k)]:
        del tags[key]
    if keywords:
        tags[KEYWORDS_KEY] = [MP4FreeForm(SEPARATOR.join(keywords).encode("utf-8"),
                                          dataformat=AtomDataType.UTF8)]
    if "gnre" in tags:
        del tags["gnre"]
    if genre:
        tags[GENRE_KEY] = [SEPARATOR.join(genre)]
    elif GENRE_KEY in tags:
        del tags[GENRE_KEY]
    if not isinstance(cover, _Unchanged):
        if cover:
            fmt = MP4Cover.FORMAT_PNG if cover.startswith(PNG_SIGNATURE) else MP4Cover.FORMAT_JPEG
            tags[COVER_KEY] = [MP4Cover(cover, imageformat=fmt)]
        elif COVER_KEY in tags:
            del tags[COVER_KEY]
    if isinstance(meta, VideoMeta):
        _set_text(tags, TITLE_KEY, meta.title)
        _write_year(tags, meta.year)
        _write_description(tags, meta.description)
    mp4.save(padding=_padding_func(min_padding, ensure_padding))


def _first_text(tags, key: str) -> str:
    return str(tags[key][0]) if tags.get(key) else ""


def _write_year(tags, year: int | None) -> None:
    """©day often holds a full date ("2019-07-14T10:00:00Z"). Runemark only edits the year,
    so keep the original value when its year is unchanged instead of cutting it down to "2019"."""
    current = _first_text(tags, YEAR_KEY)
    if year and current and parse_year(current) == year:
        return
    _set_text(tags, YEAR_KEY, str(year) if year else "")


def _write_description(tags, description: str) -> None:
    """desc is what Runemark shows and edits; ©cmt (Explorer's Comments) may hold a separate comment.
    Only touch ©cmt when it was empty or mirrored the description, so a real comment isn't overwritten."""
    desc_key, cmt_key = DESCRIPTION_KEYS
    old_desc, old_cmt = _first_text(tags, desc_key), _first_text(tags, cmt_key)
    shown = old_desc or old_cmt              # what read_mp4_tags reports as the description
    if (description or "").strip() == shown.strip():
        return                               # unchanged: leave both exactly as they were
    _set_text(tags, desc_key, description)
    if not old_cmt.strip() or old_cmt.strip() == shown.strip():
        _set_text(tags, cmt_key, description)


def _set_text(tags, key: str, value: str) -> None:
    value = (value or "").strip()
    if value:
        tags[key] = [value]
    elif key in tags:
        del tags[key]


def estimate_growth(path: str | os.PathLike, *, keywords: list[str], genre: list[str],
                    cover: bytes | None | _Unchanged, meta: VideoMeta | _Unchanged = UNCHANGED) -> int:
    """Rough number of extra bytes a write needs beyond the current tag list."""
    current = read_mp4_tags(path)
    if isinstance(meta, VideoMeta):
        growth_meta = len((meta.title + meta.description * 2).encode("utf-8")) + 128 \
            - len((current.title + current.description * 2).encode("utf-8"))
    else:
        growth_meta = 0
    growth = len(SEPARATOR.join(keywords).encode("utf-8")) - len(SEPARATOR.join(current.keywords).encode("utf-8"))
    growth += len(SEPARATOR.join(genre).encode("utf-8")) - len(SEPARATOR.join(current.genre).encode("utf-8"))
    if not isinstance(cover, _Unchanged):
        growth += len(cover or b"") - len(current.cover or b"")
    growth += 512 + growth_meta  # box headers for newly added items
    # Explorer's Tags (Xtra box) also come out of the same padding
    growth += sum(len(t.encode("utf-16-le")) + 8 for t in keywords) + 64
    return growth


def edit_moves_media(path: str | os.PathLike, growth: int) -> bool:
    """True when the edit can't fit in place and the file's media data sits after moov,
    so mutagen would have to shift the whole video inside the file."""
    layout = read_layout(path)
    if not layout.moov_before_media:
        return False  # moov is last: it can grow at the end of the file
    room = (layout.free_size or 0) - 8
    return growth + MIN_PADDING // 4 > room
