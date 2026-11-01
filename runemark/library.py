"""The library: every video in your library folders (and every file Runemark has saved),
with what each one holds, so you can search by tag and change many files at once.

Scanning only reads files that are new or changed since the last scan.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape

from . import explorer
from .media import xtra
from .media.mp4meta import VideoMeta, parse_year, read_mp4_tags, split_values, value_to_stars
from .media.probe import MKV_EXTENSIONS, MP4_EXTENSIONS, VIDEO_EXTENSIONS, probe
from .tagdb import FileRecord, TagDB, file_key
from .tagsync import merge_unique, read_state, route_fields, save_mp4

log = logging.getLogger(__name__)

Progress = Callable[[int, str], None]

# ------------------------------------------------------------------ filters
FILTER_ALL = "all"
FILTER_UNTAGGED = "untagged"
FILTER_NO_COVER = "no_cover"
FILTER_MKV = "mkv"
FILTER_UNRATED = "unrated"
FILTER_MISSING = "missing"

FILTERS = [
    (FILTER_ALL, "All videos"),
    (FILTER_UNTAGGED, "No tags yet"),
    (FILTER_NO_COVER, "No thumbnail"),
    (FILTER_UNRATED, "Not rated"),
    (FILTER_MKV, "MKV (not converted yet)"),
    (FILTER_MISSING, "Missing (moved or deleted)"),
]


@dataclass
class Search:
    tags: list[str] = field(default_factory=list)     # every one of these (AND)
    text: str = ""                                    # in the file name or title
    filter: str = FILTER_ALL

    def to_dict(self) -> dict:
        return {"tags": list(self.tags), "text": self.text, "filter": self.filter}

    @classmethod
    def from_dict(cls, data: dict) -> "Search":
        return cls(list(data.get("tags", [])), data.get("text", ""), data.get("filter", FILTER_ALL))


def matches(record: FileRecord, search: Search) -> bool:
    have = {t.casefold() for t in record.tags}
    if any(t.casefold() not in have for t in search.tags):
        return False
    text = search.text.strip().casefold()
    if text and text not in Path(record.path).name.casefold() and text not in record.title.casefold() \
            and not any(text in n.casefold() for n in record.people + record.people_aliases):
        return False
    f = search.filter
    if f == FILTER_MISSING:
        return record.missing
    if record.missing and f != FILTER_ALL:
        return False
    if f == FILTER_UNTAGGED:
        return not record.tags
    if f == FILTER_NO_COVER:
        return not record.has_cover
    if f == FILTER_UNRATED:
        return not record.rating
    if f == FILTER_MKV:
        return record.container == "mkv"
    return True


# ------------------------------------------------------------------ scanning
def find_videos(folders: list[str], cancel: threading.Event | None = None) -> list[Path]:
    found: list[Path] = []
    for folder in folders:
        for root, dirs, files in os.walk(folder):
            if cancel is not None and cancel.is_set():
                return found
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() != "$recycle.bin"]
            for name in files:
                if Path(name).suffix.lower() in VIDEO_EXTENSIONS and ".runemark-" not in name:
                    found.append(Path(root) / name)
    return found


def read_for_index(path: Path) -> dict:
    """What the library shows for one file. Quick for MP4 (no FFmpeg)."""
    ext = path.suffix.lower()
    if ext in MP4_EXTENSIONS:
        # Two reads per file (one mutagen parse for tags + length, one for Explorer's Xtra box), not four:
        # on a network share each read is a round trip.
        from mutagen.mp4 import MP4
        mp4 = MP4(path)
        tags = read_mp4_tags(path, mp4)
        duration = getattr(mp4.info, "length", None)
        try:
            payload = xtra.read_payload(path)
            explorer_tags = xtra.category_from_payload(payload)
            rating = value_to_stars(xtra.rating_from_payload(payload))
        except Exception:  # noqa: BLE001
            explorer_tags, rating = [], 0
        return dict(container="mp4", tags=merge_unique(tags.keywords, explorer_tags, tags.genre),
                    title=tags.title, year=tags.year, rating=rating, has_cover=bool(tags.cover),
                    duration=duration, genre=tags.genre, data=tags.data)
    info = probe(path)
    keywords = split_values([info.tag("keywords")]) if info.tag("keywords") else []
    genre = split_values([info.tag("genre")]) if info.tag("genre") else []
    return dict(container="mkv" if ext in MKV_EXTENSIONS else "other", tags=merge_unique(keywords, genre),
                title=info.tag("title"), year=parse_year(info.tag("date") or info.tag("year")), rating=0,
                has_cover=bool(info.cover_sources), duration=info.duration, genre=genre, data=None)


@dataclass
class ScanResult:
    found: int = 0
    read: int = 0
    missing: int = 0
    problems: list[str] = field(default_factory=list)
    stopped: bool = False        # cancelled partway; files already read are kept


SCAN_COMMIT_EVERY = 200


def split_tag_input(text: str, known: list[str] | None = None) -> list[str]:
    """Tags typed into one box: "Lake, Summer; Aubree" -> ["Lake", "Summer", "Aubree"].

    Commas and semicolons separate tags, except that text matching a known tag exactly stays whole
    (so a tag that really contains a comma, like "Halifax, NS", still works). Duplicates are dropped."""
    def clean(s: str) -> str:
        return " ".join(s.split())
    known_folded = {clean(k).casefold(): clean(k) for k in (known or [])}
    whole = clean(text)
    if not whole:
        return []
    if whole.casefold() in known_folded:
        return [known_folded[whole.casefold()]]
    out, seen = [], set()
    for part in re.split(r"[;,]", text):
        name = clean(part)
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(known_folded.get(name.casefold(), name))
    return out


def scan(db: TagDB, folders: list[str], *, progress: Progress | None = None,
         cancel: threading.Event | None = None, rescan_all: bool = False) -> ScanResult:
    result = ScanResult()
    if progress:
        progress(-1, "Looking for videos…")
    paths = {file_key(p): p for p in find_videos(folders, cancel)}
    if cancel is not None and cancel.is_set():
        result.stopped = True
        return result
    for record in db.library():                     # files saved by Runemark outside the folders
        paths.setdefault(file_key(record.path), Path(record.path))
    result.found = len(paths)
    items = list(paths.values())
    indexed = db.index_states()                     # one query instead of one per file
    pending = 0
    for n, path in enumerate(items):
        if cancel is not None and cancel.is_set():
            result.stopped = True
            break
        if progress and (n % 5 == 0 or n == len(items) - 1):
            progress(int(n * 100 / max(1, len(items))), f"Reading {path.name}…")
        try:
            stat = path.stat()
        except OSError:
            db.mark_missing(path, True, commit=False)
            result.missing += 1
            pending += 1
            continue
        known = indexed.get(file_key(path))
        if not rescan_all and known is not None and known[0] == stat.st_size and known[1] == stat.st_mtime:
            continue
        try:
            _index(db, path, stat, read_for_index(path), commit=False)
            result.read += 1
            pending += 1
        except Exception as exc:  # noqa: BLE001
            result.problems.append(f"{path.name}: {exc}")
            log.info("library: couldn't read %s: %s", path, exc)
        if pending >= SCAN_COMMIT_EVERY:
            db.commit()                             # batches keep a stopped or crashed scan's work, cheaply
            pending = 0
    db.commit()
    return result


def reindex(db: TagDB, path: str | os.PathLike) -> None:
    """Refresh one file's entry (after Runemark saved it)."""
    path = Path(path)
    try:
        stat = path.stat()
        _index(db, path, stat, read_for_index(path))
    except Exception as exc:  # noqa: BLE001
        log.info("library: couldn't refresh %s: %s", path, exc)


def _index(db: TagDB, path: Path, stat, data: dict, commit: bool = True) -> None:
    """Index one file, including the people in its data block (adding people the database doesn't know yet)."""
    from .people import resolve_people
    block = data.pop("data", None)
    people = resolve_people(db, block) if block else []
    db.index_file(path, size=stat.st_size, mtime=stat.st_mtime, commit=commit, people=people, **data)


# ------------------------------------------------------------------ changing many files
def sentence_case(text: str) -> str:
    """Capitalise the first letter only. str.capitalize() lower-cases the rest, turning "add Lake" into "Add lake"."""
    return text[:1].upper() + text[1:]


@dataclass
class BulkChange:
    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    rating: int | None = None          # 0-5 stars, None = leave alone

    def describe(self) -> str:
        parts = []
        if self.add:
            parts.append("add " + ", ".join(self.add))
        if self.remove:
            parts.append("remove " + ", ".join(self.remove))
        if self.rating is not None:
            parts.append(f"set the rating to {self.rating} star{'s' if self.rating != 1 else ''}"
                         if self.rating else "clear the rating")
        return "; ".join(parts)


def apply_bulk(db: TagDB, paths: list[Path], change: BulkChange, *, explorer_mode: str = explorer.MODE_AUTO,
               preserve_mtime: bool = False, progress: Progress | None = None,
               cancel: threading.Event | None = None) -> list[tuple[Path, str]]:
    """Apply a change to many MP4s. Returns (file, problem) for files that weren't changed cleanly."""
    genre_cats = db.genre_categories()
    tags_by_name = {t.name.casefold(): t for t in db.tags()}
    is_genre = lambda n: (t := tags_by_name.get(n.casefold())) is not None and t.category_id in genre_cats  # noqa: E731
    is_known = lambda n: n.casefold() in tags_by_name  # noqa: E731
    remove = {r.casefold() for r in change.remove}
    problems: list[tuple[Path, str]] = []
    batch = new_batch_id()
    for n, path in enumerate(paths):
        if cancel is not None and cancel.is_set():
            problems.append((path, "cancelled before this file"))
            continue
        if progress:
            progress(int(n * 100 / max(1, len(paths))), f"Updating {path.name}…")
        if path.suffix.lower() not in MP4_EXTENSIONS:
            problems.append((path, "MKV files can't hold Explorer's tags; open it and save to convert it to MP4"))
            continue
        try:
            state = read_state(path)
            tags = [db.canonical(t) for t in state.all_tags() if t.casefold() not in remove]
            tags = merge_unique(tags, [db.canonical(a) for a in change.add])
            keywords, genre = route_fields(tags, is_genre=is_genre, is_known=is_known, original_genre=state.genre)
            meta = None
            if change.rating is not None:
                meta = VideoMeta(state.meta.title, state.meta.year, state.meta.description, change.rating)
            kwargs = {"meta": meta} if meta is not None else {}
            outcome = save_mp4(path, keywords=keywords, genre=genre, explorer_mode=explorer_mode,
                               preserve_mtime=preserve_mtime, **kwargs)
            db.record_history(path, **history_fields(state), label=f"Library: {change.describe()}", batch=batch)
            db.remember_file(path, keywords)
            reindex(db, path)
            if outcome.problems:
                problems.append((path, "; ".join(outcome.problems)))
        except Exception as exc:  # noqa: BLE001
            problems.append((path, str(exc)))
    return problems


# ------------------------------------------------------------------ undo
def history_fields(state) -> dict:
    """What record_history needs from a FileState read before a save."""
    return {"keywords": merge_unique(state.keywords, state.explorer or []), "genre": list(state.genre),
            "title": state.meta.title, "year": state.meta.year, "description": state.meta.description,
            "rating": state.meta.rating or 0, "data": getattr(state, "data", None)}


def new_batch_id() -> str:
    return uuid.uuid4().hex


def revert_file(db: TagDB, entry, *, explorer_mode: str = explorer.MODE_AUTO, preserve_mtime: bool = False):
    """Put a file's tags, genre and details back to a history entry, then forget that entry
    (so the next undo goes one step further back). Cover and chapters aren't part of history."""
    path = Path(entry.path)
    if not path.exists():
        raise FileNotFoundError(f"{path.name} isn't there any more (moved or deleted?)")
    outcome = save_mp4(path, keywords=entry.keywords, genre=entry.genre, explorer_mode=explorer_mode,
                       preserve_mtime=preserve_mtime,
                       meta=VideoMeta(entry.title, entry.year, entry.description, entry.rating),
                       data=entry.data)      # people too (None removes a block the save added)
    db.remember_file(path, entry.keywords)
    reindex(db, path)
    db.drop_history(entry.id)
    return outcome


def undo_batch(db: TagDB, entries, *, explorer_mode: str = explorer.MODE_AUTO, preserve_mtime: bool = False,
               progress: Progress | None = None, cancel: threading.Event | None = None) -> list[tuple[Path, str]]:
    """Revert every file of one bulk change. A file saved again since then is skipped, so a later edit is
    never thrown away; it can still be undone step by step from the editor."""
    problems: list[tuple[Path, str]] = []
    for n, entry in enumerate(entries):
        path = Path(entry.path)
        if cancel is not None and cancel.is_set():
            problems.append((path, "stopped before this file; undo again to finish"))
            continue
        if progress:
            progress(int(n * 100 / max(1, len(entries))), f"Undoing {path.name}…")
        if db.changed_since(entry):
            problems.append((path, "changed again since; open it and use Undo last save to step back"))
            continue
        try:
            outcome = revert_file(db, entry, explorer_mode=explorer_mode, preserve_mtime=preserve_mtime)
            if outcome.problems:
                problems.append((path, "; ".join(outcome.problems)))
        except Exception as exc:  # noqa: BLE001
            problems.append((path, str(exc)))
    return problems


# ------------------------------------------------------------------ Jellyfin / Kodi
def nfo_text(record: FileRecord, description: str = "", collection: str = "") -> str:
    """A movie .nfo (Kodi format, read by Jellyfin and Emby) with title, year, plot, rating,
    genres and tags, and optionally a collection ("set")."""
    title = record.title or Path(record.path).stem
    genre = {g.casefold() for g in record.genre}
    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "<movie>",
             f"  <title>{escape(title)}</title>"]
    if record.year:
        lines.append(f"  <year>{record.year}</year>")
    if description:
        lines.append(f"  <plot>{escape(description)}</plot>")
    if record.rating:
        lines.append(f"  <userrating>{record.rating * 2}</userrating>")
    for g in record.genre:
        lines.append(f"  <genre>{escape(g)}</genre>")
    for t in record.tags:
        if t.casefold() not in genre:
            lines.append(f"  <tag>{escape(t)}</tag>")
    if collection:
        lines.append(f"  <set>\n    <name>{escape(collection)}</name>\n  </set>")
    lines.append("</movie>")
    return "\n".join(lines) + "\n"


def write_nfo(record: FileRecord, collection: str = "", overwrite: bool = False) -> Path | None:
    """Write Movie.nfo beside Movie.mp4. Returns the path, or None if one exists and overwrite is off."""
    path = Path(record.path)
    target = path.with_suffix(".nfo")
    if target.exists() and not overwrite:
        return None
    description = ""
    if path.suffix.lower() in MP4_EXTENSIONS and path.exists():
        try:
            description = read_mp4_tags(path).description
        except Exception:  # noqa: BLE001
            pass
    target.write_text(nfo_text(record, description, collection), encoding="utf-8")
    return target
