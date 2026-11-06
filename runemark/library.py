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
FILTER_INBOX = "inbox"
FILTER_GAPS = "gaps"

FILTERS = [
    (FILTER_ALL, "All videos"),
    (FILTER_INBOX, "Inbox (new, not saved yet)"),
    (FILTER_GAPS, "Still to do (saved, but missing something)"),
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


def still_to_do(rec: FileRecord) -> list[str]:
    """What a video doesn't have yet: for working through the inbox, and through gaps left in saved videos."""
    out = []
    if not rec.tags:
        out.append("tags")
    if not rec.genre:
        out.append("genre")
    if not rec.people:
        out.append("people")
    if not rec.has_cover:
        out.append("thumbnail")
    if rec.chapters == 0:
        out.append("chapters")
    if not rec.studio:
        out.append("studio")
    if rec.container == "mkv":
        out.append("convert")
    return out


def matches(record: FileRecord, search: Search) -> bool:
    have = {t.casefold() for t in record.tags}
    if any(t.casefold() not in have for t in search.tags):
        return False
    text = search.text.strip().casefold()
    if text and text not in Path(record.path).name.casefold() and text not in record.title.casefold() \
            and text not in record.studio.casefold() \
            and not any(text in n.casefold() for n in record.people + record.people_aliases):
        return False
    f = search.filter
    if f == FILTER_MISSING:
        return record.missing
    if record.missing and f != FILTER_ALL:
        return False
    if f == FILTER_INBOX:
        return record.in_inbox
    if f == FILTER_GAPS:                       # already curated (saved or marked done), with something left to add
        return not record.in_inbox and bool(still_to_do(record))
    if f == FILTER_UNTAGGED:
        return not record.tags
    if f == FILTER_NO_COVER:
        return not record.has_cover
    if f == FILTER_UNRATED:
        return not record.rating
    if f == FILTER_MKV:
        return record.container == "mkv"
    return True


# ------------------------------------------------------------------ smart filters (0.9.0)
# A smart filter is a named set of rules, matched all together (AND) or any of them (OR); pinned ones sit above the
# Library's list with how many videos match. Rules: [field, op, value].
SMART_FIELDS = {
    # field: (label, [(op, label)], takes a value)
    "tag": ("Tag", [("has", "is on it"), ("lacks", "isn't on it")], True),
    "person": ("Person", [("has", "is in it"), ("lacks", "isn't in it")], True),
    "studio": ("Studio", [("is", "is"), ("not", "isn't"), ("contains", "contains")], True),
    "text": ("Name or title", [("contains", "contains"), ("lacks", "doesn't contain")], True),
    "rating": ("Rating", [("ge", "at least"), ("le", "at most"), ("eq", "exactly")], True),
    "year": ("Year", [("ge", "from"), ("le", "up to"), ("eq", "is")], True),
    "genre": ("Genre", [("has", "is"), ("lacks", "isn't"), ("none", "has none")], True),
    "thumbnail": ("Thumbnail", [("yes", "has one"), ("no", "has none")], False),
    "chapters": ("Chapters", [("yes", "has some"), ("no", "has none")], False),
    "people": ("People", [("yes", "has some"), ("no", "has none")], False),
    "saved": ("Saved in Runemark", [("yes", "yes"), ("no", "not yet")], False),
    "inbox": ("Inbox", [("yes", "in the inbox"), ("no", "not in the inbox")], False),
    "mkv": ("MKV", [("yes", "is an MKV"), ("no", "is an MP4")], False),
}


@dataclass
class SmartRule:
    field: str
    op: str
    value: str = ""

    def describe(self) -> str:
        label, ops, takes = SMART_FIELDS.get(self.field, (self.field, [], True))
        op = dict(ops).get(self.op, self.op)
        return f"{label} {op}" + (f" “{self.value}”" if takes and self.value and self.op != "none" else "")


@dataclass
class SmartFilter:
    name: str
    rules: list[SmartRule] = field(default_factory=list)
    match: str = "all"              # "all" (AND) or "any" (OR)
    pinned: bool = True

    def to_dict(self) -> dict:
        return {"name": self.name, "match": self.match, "pinned": self.pinned,
                "rules": [[r.field, r.op, r.value] for r in self.rules]}

    @classmethod
    def from_dict(cls, data: dict) -> "SmartFilter":
        rules = [SmartRule(str(r[0]), str(r[1]), str(r[2]) if len(r) > 2 else "") for r in data.get("rules", [])]
        return cls(str(data["name"]), rules, "any" if data.get("match") == "any" else "all",
                   bool(data.get("pinned", True)))

    def describe(self) -> str:
        if not self.rules:
            return "Every video"
        joiner = " and " if self.match == "all" else " or "
        return joiner.join(r.describe() for r in self.rules)


def _number(text: str) -> int | None:
    try:
        return int(str(text).strip())
    except ValueError:
        return None


def rule_matches(rec: FileRecord, rule: SmartRule) -> bool:
    f, op, value = rule.field, rule.op, (rule.value or "").strip()
    folded = value.casefold()
    if f == "tag":
        has = folded in {t.casefold() for t in rec.tags}
        return has if op == "has" else not has
    if f == "person":
        names = {n.casefold() for n in rec.people + rec.people_aliases}
        return (folded in names) if op == "has" else (folded not in names)
    if f == "studio":
        studio = (rec.studio or "").casefold()
        return {"is": studio == folded, "not": studio != folded, "contains": bool(folded) and folded in studio}.get(
            op, False)
    if f == "text":
        hay = (Path(rec.path).name + " " + (rec.title or "")).casefold()
        return (folded in hay) if op == "contains" else (folded not in hay)
    if f in ("rating", "year"):
        have = (rec.rating or 0) if f == "rating" else rec.year
        want = _number(value)
        if want is None or have is None:
            return False
        return {"ge": have >= want, "le": have <= want, "eq": have == want}.get(op, False)
    if f == "genre":
        genres = {g.casefold() for g in rec.genre}
        if op == "none":
            return not genres
        return (folded in genres) if op == "has" else (folded not in genres)
    yes = op == "yes"
    if f == "thumbnail":
        return bool(rec.has_cover) == yes
    if f == "chapters":
        return bool(rec.chapters) == yes
    if f == "people":
        return bool(rec.people) == yes
    if f == "saved":
        return bool(rec.saved_at) == yes
    if f == "inbox":
        return rec.in_inbox == yes
    if f == "mkv":
        return (rec.container == "mkv") == yes
    return False


def smart_matches(rec: FileRecord, smart: SmartFilter) -> bool:
    """Missing videos never match (they'd only clutter a filter)."""
    if rec.missing:
        return False
    if not smart.rules:
        return True
    results = (rule_matches(rec, r) for r in smart.rules)
    return all(results) if smart.match == "all" else any(results)


# ------------------------------------------------------------------ libraries (0.7.0)
# A library is a name, its folders, and subfolders left out of it ("skip"). One video can belong to several
# libraries (a folder of favourites inside a bigger collection). Scans walk every library's folders once.
def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _inside(path: str, folder: str) -> bool:
    p, f = _norm(path), _norm(folder)
    return p == f or p.startswith(f.rstrip(os.sep) + os.sep)


def in_library(path: str, library: dict) -> bool:
    """Is this file in that library: under one of its folders and not under a subfolder it leaves out?"""
    return any(_inside(path, f) for f in library.get("folders", [])) \
        and not any(_inside(path, s) for s in library.get("skip", []))


def library_named(settings, name: str) -> dict | None:
    return next((lib for lib in settings.libraries if lib.get("name", "").casefold() == (name or "").casefold()), None)


def libraries_of(settings, path: str) -> list[str]:
    """Names of the libraries a file belongs to."""
    return [lib["name"] for lib in settings.libraries if in_library(path, lib)]


def scan_skips(settings) -> list[str]:
    """Subfolders a scan doesn't go into: those left out of a library, unless another library includes them."""
    out = []
    for lib in settings.libraries:
        for skip in lib.get("skip", []):
            if not any(in_library(os.path.join(skip, "x"), other) for other in settings.libraries if other is not lib):
                out.append(skip)
    return out


# ------------------------------------------------------------------ scanning
def find_videos(folders: list[str], cancel: threading.Event | None = None, skip: list[str] = ()) -> list[Path]:
    found: list[Path] = []
    skipped = {_norm(x) for x in skip}
    for folder in folders:
        for root, dirs, files in os.walk(folder):
            if cancel is not None and cancel.is_set():
                return found
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() != "$recycle.bin"
                       and _norm(os.path.join(root, d)) not in skipped]
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
        from .media.atoms import movie_duration
        with open(path, "rb") as fh:
            mp4 = MP4(fh)
            # the movie's length (moov/mvhd), as the player and the details show it; mutagen's is the audio's
            duration = movie_duration(fh) or getattr(mp4.info, "length", None)
        tags = read_mp4_tags(path, mp4)
        try:
            payload = xtra.read_payload(path)
            explorer_tags = xtra.category_from_payload(payload)
            rating = value_to_stars(xtra.rating_from_payload(payload))
        except Exception:  # noqa: BLE001
            explorer_tags, rating = [], 0
        chapters = getattr(mp4, "chapters", None)
        if chapters:
            from .media.chapters import without_lead_in
            chapters = without_lead_in(sorted(chapters, key=lambda c: c.start))      # 0.11.0: the hidden "Start"
        return dict(container="mp4", tags=merge_unique(tags.keywords, explorer_tags, tags.genre),
                    title=tags.title, year=tags.year, rating=rating, has_cover=bool(tags.cover),
                    duration=duration, genre=tags.genre, data=tags.data, studio=tags.studio,
                    chapters=len(chapters) if chapters is not None else 0,
                    chapter_names=named_chapters(c.title for c in (chapters or [])))
    info = probe(path)
    keywords = split_values([info.tag("keywords")]) if info.tag("keywords") else []
    genre = split_values([info.tag("genre")]) if info.tag("genre") else []
    return dict(container="mkv" if ext in MKV_EXTENSIONS else "other", tags=merge_unique(keywords, genre),
                title=info.tag("title"), year=parse_year(info.tag("date") or info.tag("year")), rating=0,
                has_cover=bool(info.cover_sources), duration=info.duration, genre=genre, data=None,
                studio=info.tag("studio") or info.tag("publisher"), chapters=len(_shown(info.chapters)),
                chapter_names=named_chapters(c.title for c in _shown(info.chapters)))


def _shown(chapters) -> list:
    from .media.chapters import without_lead_in
    return without_lead_in(sorted(chapters or [], key=lambda c: c.start))


def named_chapters(titles) -> list[str]:
    """Chapter names worth suggesting again: not blank, not "Chapter 3", once each."""
    from .media.chapters import is_auto_name
    out, seen = [], set()
    for title in titles:
        name = " ".join(str(title or "").split())
        if name and not is_auto_name(name) and name != "Start" and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    return out


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


# ------------------------------------------------------------------ scan providers (plugin API 3, 0.10.0)
# A plugin can list what's in some folders without Runemark opening the files: a service on the computer that holds
# them, say, which reads them at disk speed and sends only the results. The scan asks each provider first, then
# lists the folders itself (names, sizes and times: cheap even over a network). A file whose size and time match
# the provider's record is imported from it without being opened; any other new or changed file is read here, so
# a file the provider hasn't caught up with yet (just downloaded, just moved, just saved here) is still found. Only
# files that aren't on disk are marked missing; a provider's records for files that aren't there are ignored.
INDEX_FIELDS = ("container", "tags", "title", "year", "rating", "has_cover", "duration", "genre", "data", "studio",
                "chapters", "chapter_names")
STAMP_SLACK = 0.01                 # the same modified time read through two routes can differ in the last digits
PROVIDER_SLACK = 2.0               # a provider's time (another computer, another protocol) may be rounded (0.10.1)


@dataclass
class ScanListing:
    """What a scan provider has for some folders. covered: folders it lists completely (the ones it was asked for,
    or folders inside them). records: one dict per video under them: "path", "size", "mtime" and what
    read_for_index() gives (chapters may be the count or a list of {"start", "end", "title"}); or "path", "size",
    "mtime" and "unreadable": True for a file the provider couldn't read (Runemark reads it itself).
    not_covered: folders inside the covered ones that it doesn't list (they're scanned here as usual)."""
    covered: list[str] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)
    not_covered: list[str] = field(default_factory=list)

    def covers(self, path: str) -> bool:
        return any(_inside(str(path), c) for c in self.covered) \
            and not any(_inside(str(path), n) for n in self.not_covered)


@dataclass
class ScanProvider:
    owner: str
    listing: Callable[[list[str], threading.Event | None], ScanListing | dict | None]
    thumbnail: Callable[[str], bytes | None] | None = None
    last: ScanListing | None = None                         # its last listing's folders (for thumbnails)


_providers: list[ScanProvider] = []


def add_scan_provider(owner: str, listing, thumbnail=None) -> ScanProvider:
    provider = ScanProvider(owner, listing, thumbnail)
    _providers.append(provider)
    return provider


def clear_scan_providers(owner: str | None = None) -> None:
    _providers[:] = [p for p in _providers if owner is not None and p.owner != owner]


def scan_providers() -> list[ScanProvider]:
    return list(_providers)


def provider_listings(folders: list[str], cancel: threading.Event | None = None) -> list[ScanListing]:
    """Each provider's listing for these folders. A provider that has nothing, or fails, covers nothing."""
    out = []
    for provider in scan_providers():
        try:
            found = provider.listing(list(folders), cancel)
        except Exception:  # noqa: BLE001
            log.exception("scan provider of %s failed; scanning its folders here", provider.owner)
            found = None
        if isinstance(found, dict):
            found = ScanListing(list(found.get("covered") or []), list(found.get("records") or []),
                                list(found.get("not_covered") or []))
        if not isinstance(found, ScanListing):
            provider.last = None
            continue
        found.covered = [str(c) for c in found.covered if any(_inside(str(c), f) for f in folders)]
        found.not_covered = [str(n) for n in found.not_covered]
        provider.last = ScanListing(list(found.covered), [], list(found.not_covered))
        out.append(found)
    return out


def provider_thumbnail(path: str | os.PathLike) -> bytes | None:
    """A small JPEG of the cover from the provider whose last listing covered this file: None when no provider
    covers it (read it here), b"" when the provider says it has no cover."""
    for provider in scan_providers():
        if provider.thumbnail is None or provider.last is None or not provider.last.covers(str(path)):
            continue
        try:
            data = provider.thumbnail(str(path))
        except Exception as exc:  # noqa: BLE001
            log.info("scan provider of %s couldn't give the thumbnail of %s: %s", provider.owner, path, exc)
            continue
        if data is not None:
            return bytes(data)
    return None


class _Stamp:
    def __init__(self, size, mtime):
        self.st_size, self.st_mtime = int(size), float(mtime)


def index_fields(record: dict) -> dict:
    """read_for_index()'s fields from a provider's record."""
    fields = {k: record[k] for k in INDEX_FIELDS if k in record}
    chapters = fields.get("chapters")
    if isinstance(chapters, list):
        fields["chapters"] = len(chapters)
        if "chapter_names" not in fields:
            fields["chapter_names"] = named_chapters(c.get("title", "") for c in chapters if isinstance(c, dict))
    for key in ("tags", "genre", "chapter_names"):
        fields[key] = [str(x) for x in fields.get(key) or []]
    fields.setdefault("container", "mp4")
    if fields.get("data") is not None and not isinstance(fields["data"], dict):
        fields["data"] = None
    return fields


def find_video_stamps(folders: list[str], cancel: threading.Event | None = None,
                      skip: list[str] = ()) -> dict[str, tuple[Path, int, float]]:
    """{file key: (path, size, modified time)} of the videos under the folders, as find_videos() finds them, with
    each file's size and time from the folder listing itself (on Windows that costs nothing more per file, even on
    a network share)."""
    found: dict[str, tuple[Path, int, float]] = {}
    skipped = {_norm(x) for x in skip}
    todo = [str(f) for f in folders]
    while todo:
        if cancel is not None and cancel.is_set():
            return found
        folder = todo.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        subdirs = []
        for entry in entries:
            name = entry.name
            try:
                if entry.is_dir():
                    if not name.startswith(".") and name.lower() != "$recycle.bin" \
                            and _norm(entry.path) not in skipped:
                        subdirs.append(entry.path)
                    continue
                if Path(name).suffix.lower() not in VIDEO_EXTENSIONS or ".runemark-" in name:
                    continue
                st = entry.stat()
            except OSError:
                continue
            path = Path(entry.path)
            found.setdefault(file_key(path), (path, st.st_size, st.st_mtime))
        todo.extend(reversed(sorted(subdirs)))
    return found


def provider_matches(record: dict, size: int, mtime: float) -> bool:
    """A provider's record describes the file on disk: the same size, and the same modified time give or take what
    the two routes to it round away."""
    try:
        return int(record["size"]) == int(size) and abs(float(record["mtime"]) - float(mtime)) <= PROVIDER_SLACK
    except (KeyError, TypeError, ValueError):
        return False


def scan(db: TagDB, folders: list[str], *, progress: Progress | None = None,
         cancel: threading.Event | None = None, rescan_all: bool = False, skip: list[str] = ()) -> ScanResult:
    """Bring the library's index of these folders up to date. 0.10.1: every folder is listed here (names, sizes and
    times only, which is cheap even over a network); a file a scan provider describes with the same size and time
    is imported from its record, any other new or changed file is read here, and only files that aren't there any
    more are marked missing. A provider's records for files that aren't on disk are ignored."""
    result = ScanResult()
    if progress:
        progress(-1, "Looking for videos…")
    listings = provider_listings(folders, cancel) if scan_providers() else []

    provided: dict[str, dict] = {}
    for listing in listings:
        for record in listing.records:
            try:
                path = Path(str(record["path"]))
                int(record["size"]), float(record["mtime"])
            except (KeyError, TypeError, ValueError):
                continue
            if record.get("unreadable") or Path(path.name).suffix.lower() not in VIDEO_EXTENSIONS \
                    or ".runemark-" in path.name or not listing.covers(str(path)) \
                    or any(_inside(str(path), s) for s in skip):
                continue                                    # read here if it's on disk
            provided.setdefault(file_key(path), record)
    on_disk = find_video_stamps(folders, cancel, list(skip))
    if cancel is not None and cancel.is_set():
        result.stopped = True
        return result
    was_missing: set[str] = set()
    elsewhere: dict[str, Path] = {}
    for record in db.library():                     # files saved by Runemark outside the folders, or gone
        key = file_key(record.path)
        if record.missing:
            was_missing.add(key)
        if key not in on_disk:
            elsewhere[key] = Path(record.path)
    result.found = len(on_disk) + len(elsewhere)
    indexed = db.index_states()                     # one query instead of one per file
    imports: list[tuple[Path, int, float, dict]] = []
    items: list[Path] = []
    for key, (path, size, mtime) in on_disk.items():
        known = indexed.get(key)
        if not rescan_all and known is not None and known[0] == size and known[1] is not None \
                and abs(known[1] - mtime) <= STAMP_SLACK and key not in was_missing:
            continue                                # unchanged since it was last read
        record = provided.get(key)
        if record is not None and provider_matches(record, size, mtime):
            imports.append((path, size, mtime, record))
        else:
            items.append(path)                      # new or changed, and the provider hasn't read it yet: here
    items += list(elsewhere.values())
    pending = 0
    total = max(1, len(imports) + len(items))
    for n, (path, size, mtime, record) in enumerate(imports):
        if cancel is not None and cancel.is_set():
            result.stopped = True
            db.commit()
            return result
        if progress and (n % 50 == 0 or n == len(imports) - 1):
            progress(int(n * 100 / total), f"Reading {path.name}…")
        try:
            _index(db, path, _Stamp(size, mtime), index_fields(record), commit=False)
            result.read += 1
            pending += 1
        except Exception as exc:  # noqa: BLE001
            result.problems.append(f"{path.name}: {exc}")
            log.info("library: couldn't import %s: %s", path, exc)
        if pending >= SCAN_COMMIT_EVERY:
            db.commit()
            pending = 0
    for n, path in enumerate(items):
        if cancel is not None and cancel.is_set():
            result.stopped = True
            break
        if progress and (n % 5 == 0 or n == len(items) - 1):
            progress(int((len(imports) + n) * 100 / total), f"Reading {path.name}…")
        try:
            stat = path.stat()
        except OSError:
            db.mark_missing(path, True, commit=False)
            result.missing += 1
            pending += 1
            continue
        key = file_key(path)
        known = indexed.get(key)
        if not rescan_all and known is not None and known[0] == stat.st_size and known[1] == stat.st_mtime \
                and key not in was_missing:
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
    adding = db.with_implied([db.canonical(a) for a in change.add])      # tag rules add their tags too
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
            tags = merge_unique(tags, adding)
            keywords, genre = route_fields(tags, is_genre=is_genre, is_known=is_known, original_genre=state.genre)
            meta = None
            if change.rating is not None:
                meta = VideoMeta(state.meta.title, state.meta.year, state.meta.description, change.rating,
                                 state.meta.studio)
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
            "rating": state.meta.rating or 0, "data": getattr(state, "data", None),
            "studio": state.meta.studio or ""}


def new_batch_id() -> str:
    return uuid.uuid4().hex


def revert_file(db: TagDB, entry, *, explorer_mode: str = explorer.MODE_AUTO, preserve_mtime: bool = False,
                handlers: bool = False, progress: Progress | None = None):
    """Put a file's tags, genre and details back to a history entry, then forget that entry
    (so the next undo goes one step further back). The chapters go back too when that save changed them (0.9.0;
    history from before 0.9.0 has none, and leaves them). The cover isn't part of history.
    handlers=True (Undo last save, 0.10.0): the save is offered to plugins' save handlers first, like the editor's."""
    from . import saving
    path = Path(entry.path)
    if not path.exists():
        raise FileNotFoundError(f"{path.name} isn't there any more (moved or deleted?)")
    stamp = tuple((saving.read_stamp(path) if handlers else saving.stamp(path)) or (None, None)) + (None,)
    meta = VideoMeta(entry.title, entry.year, entry.description, entry.rating, entry.studio)
    request = info = None
    if getattr(entry, "chapters", None) is not None:
        from .media import chapters as chapter_io
        from .media import frames
        info = probe(path)
        wanted = chapter_io.marks_from_json(entry.chapters)
        if not chapter_io.same_marks(chapter_io.marks_from_info(info), wanted):
            sources = info.cover_sources
            state = read_state(path, info, lambda i: frames.dump_attachment(i.path, sources[0]) if sources else None)
            full = VideoMeta(**vars(state.meta))
            for name in ("title", "year", "description", "rating", "studio"):
                setattr(full, name, getattr(meta, name))
            if entry.studio is None:
                full.studio = state.meta.studio
            request = saving.SaveRequest(path, list(entry.keywords), list(entry.genre), state.cover, full,
                                         entry.data, wanted, preserve_mtime, stamp[0], stamp[1], "undo",
                                         expected_fp=stamp[2])
    if request is None:
        request = saving.SaveRequest(path, list(entry.keywords), list(entry.genre), meta=meta,
                                     data=entry.data,     # people too (None removes a block the save added)
                                     preserve_mtime=preserve_mtime, expected_size=stamp[0],
                                     expected_mtime=stamp[1], kind="undo", expected_fp=stamp[2])
    if handlers:
        outcome = saving.save(request, info=info, explorer_mode=explorer_mode, progress=progress)
    elif request.rewrite:
        from .tagsync import save_mp4_with_chapters
        outcome = save_mp4_with_chapters(path, info, request.chapters, keywords=request.keywords,
                                         genre=request.genre, cover=request.cover, meta=request.meta,
                                         explorer_mode=explorer_mode, preserve_mtime=preserve_mtime,
                                         data=request.data)
    else:
        outcome = save_mp4(path, keywords=request.keywords, genre=request.genre, explorer_mode=explorer_mode,
                           preserve_mtime=preserve_mtime, meta=meta, data=request.data)
    db.remember_file(path, entry.keywords)
    reindex(db, path)
    db.drop_history(entry.id)
    db.log_activity(f"Undone: {entry.label or 'a save'}", path)
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
    genres, tags and the people in it (as actors), and optionally a collection ("set")."""
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
    if record.studio:
        lines.append(f"  <studio>{escape(record.studio)}</studio>")
    for g in record.genre:
        lines.append(f"  <genre>{escape(g)}</genre>")
    for t in record.tags:
        if t.casefold() not in genre:
            lines.append(f"  <tag>{escape(t)}</tag>")
    for name in record.people:                      # people in the video: Kodi and Jellyfin show them as cast
        lines.append(f"  <actor>\n    <name>{escape(name)}</name>\n  </actor>")
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
