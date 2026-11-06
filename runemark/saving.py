"""Saves another program can carry out (plugin API 3, 0.10.0).

Runemark normally writes a video itself. A plugin can offer to do it somewhere else instead: a service on the
computer that holds the files, say, so a save over a slow network sends a few kilobytes instead of the whole video.
Each queued save (the editor's, change_file's and Undo last save's) is offered to the save handlers first, as a
SaveRequest: the file, everything the local writer would write, and the size and modified time the file had when it
was read. A handler answers:

* SaveResult.handled(...)      it wrote the file; Runemark reads it back and checks it, as after its own saves;
* SaveResult.not_handled()     (or None, or raising): Runemark saves it itself, exactly as before;
* SaveResult.failed(reason)    it couldn't, and the file wasn't changed: the save fails with that reason (Retry).

The same request can be turned back into the file it describes anywhere Runemark's code runs: write_copy() writes
the saved version of a video to another file, with Runemark's own writer, so a handler on another computer produces
exactly what a local save would.
"""
from __future__ import annotations

import base64
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import explorer
from .media.mp4meta import UNCHANGED, VideoMeta
from .tagdb import file_key
from .tagsync import (Progress, SaveError, SaveOutcome, copy_with_progress, remove_file, save_mp4,
                      save_mp4_with_chapters, verify_meta, verify_mp4)

log = logging.getLogger(__name__)

HANDLED = "handled"
NOT_HANDLED = "not_handled"
FAILED = "failed"
FORMAT = 1                       # version of SaveRequest.to_dict()'s layout
STAMP_SLACK = 0.01               # modified times read through different routes may differ in the last digits


@dataclass
class SaveResult:
    status: str
    reason: str = ""
    problems: list[str] = field(default_factory=list)   # things that didn't read back as expected (a warning)
    size: int | None = None                               # the file's size and modified time after the save
    mtime: float | None = None

    @classmethod
    def handled(cls, problems=(), size: int | None = None, mtime: float | None = None) -> "SaveResult":
        return cls(HANDLED, "", [str(p) for p in problems], size, mtime)

    @classmethod
    def not_handled(cls, reason: str = "") -> "SaveResult":
        return cls(NOT_HANDLED, reason)

    @classmethod
    def failed(cls, reason: str) -> "SaveResult":
        return cls(FAILED, str(reason or "the save handler couldn't save it"))


class SaveFailed(SaveError):
    """A save handler said it couldn't save the file (and didn't change it)."""


@dataclass
class SaveRequest:
    """Everything one save writes. cover: UNCHANGED, image bytes, or None/b"" to remove it. meta: UNCHANGED or a
    VideoMeta. data: UNCHANGED, Runemark's data block (a dict), or None to remove it. chapters: None leaves the
    chapter track alone; a list of ChapterMarks writes the file again with them (cover, meta and data are then the
    complete values, as the rewrite starts without them)."""
    path: Path
    keywords: list[str]
    genre: list[str]
    cover: Any = UNCHANGED
    meta: Any = UNCHANGED
    data: Any = UNCHANGED
    chapters: list | None = None
    preserve_mtime: bool = False
    expected_size: int | None = None
    expected_mtime: float | None = None
    kind: str = "save"               # "save" (the editor, change_file) or "undo" (Undo last save)

    def __post_init__(self):
        self.path = Path(self.path)

    @property
    def rewrite(self) -> bool:
        return self.chapters is not None

    @property
    def expected(self) -> dict:
        return {"size": self.expected_size, "mtime": self.expected_mtime}

    def changed_fields(self) -> list[str]:
        """Names of what this save writes (for logs: no values)."""
        out = ["keywords", "genre"]
        if self.cover is not UNCHANGED:
            out.append("cover")
        if isinstance(self.meta, VideoMeta):
            out.append("details")
        if self.data is not UNCHANGED:
            out.append("data")
        if self.rewrite:
            out.append("chapters")
        return out

    def to_dict(self) -> dict:
        """The changes as plain JSON-safe data (no path, no expected size or time). Missing keys mean unchanged."""
        out: dict = {"format": FORMAT, "kind": self.kind, "keywords": list(self.keywords),
                     "genre": list(self.genre), "preserve_mtime": bool(self.preserve_mtime)}
        if self.cover is not UNCHANGED:
            out["cover"] = base64.b64encode(self.cover).decode("ascii") if self.cover else None
        if isinstance(self.meta, VideoMeta):
            out["meta"] = {"title": self.meta.title, "year": self.meta.year, "description": self.meta.description,
                           "rating": self.meta.rating, "studio": self.meta.studio,
                           "links": list(self.meta.links) if self.meta.links is not None else None,
                           "date": self.meta.date}
        if self.data is not UNCHANGED:
            out["data"] = self.data
        if self.rewrite:
            out["chapters"] = [{"start": float(m.start), "name": m.name, "auto": bool(m.auto),
                                "end": getattr(m, "end", None)} for m in self.chapters]
        return out

    @classmethod
    def from_dict(cls, path, changes: dict, expected: dict | None = None) -> "SaveRequest":
        """The request to_dict() described. Raises ValueError for anything malformed."""
        from .media.chapters import ChapterMark
        if not isinstance(changes, dict):
            raise ValueError("changes must be an object")
        if int(changes.get("format", FORMAT)) > FORMAT:
            raise ValueError(f"changes format {changes.get('format')} is newer than this Runemark reads ({FORMAT})")

        def names(key):
            value = changes.get(key, [])
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"{key} must be a list of text")
            return list(value)

        cover = UNCHANGED
        if "cover" in changes:
            raw = changes["cover"]
            if raw is None or raw == "":
                cover = None
            elif isinstance(raw, str):
                cover = base64.b64decode(raw.encode("ascii"), validate=True)
            else:
                raise ValueError("cover must be base64 text or null")
        meta = UNCHANGED
        if changes.get("meta") is not None:
            m = changes["meta"]
            if not isinstance(m, dict):
                raise ValueError("meta must be an object")
            links = m.get("links")
            meta = VideoMeta(str(m.get("title") or ""), int(m["year"]) if m.get("year") not in (None, "") else None,
                             str(m.get("description") or ""), max(0, min(5, int(m.get("rating") or 0))),
                             None if m.get("studio") is None else str(m["studio"]),
                             None if links is None else [str(x) for x in links],
                             None if not m.get("date") else str(m["date"]))
        data = UNCHANGED
        if "data" in changes:
            data = changes["data"]
            if data is not None and not isinstance(data, dict):
                raise ValueError("data must be an object or null")
        chapters = None
        if changes.get("chapters") is not None:
            if not isinstance(changes["chapters"], list):
                raise ValueError("chapters must be a list")
            chapters = []
            for c in changes["chapters"]:
                end = c.get("end")
                chapters.append(ChapterMark(float(c["start"]), str(c.get("name") or ""), bool(c.get("auto")),
                                            float(end) if end is not None else None))
        expected = expected or {}
        return cls(Path(path), names("keywords"), names("genre"), cover, meta, data, chapters,
                   bool(changes.get("preserve_mtime")),
                   int(expected["size"]) if expected.get("size") is not None else None,
                   float(expected["mtime"]) if expected.get("mtime") is not None else None,
                   str(changes.get("kind") or "save"))


# ------------------------------------------------------------------ handlers
_handlers: list[tuple[str, Callable]] = []
_lock = threading.Lock()


def add_save_handler(owner: str, handler: Callable[[SaveRequest, Progress], SaveResult | None]) -> None:
    with _lock:
        _handlers.append((owner, handler))


def clear_save_handlers(owner: str | None = None) -> None:
    with _lock:
        _handlers[:] = [h for h in _handlers if owner is not None and h[0] != owner]


def save_handlers() -> list[tuple[str, Callable]]:
    with _lock:
        return list(_handlers)


def offer(request: SaveRequest, progress: Progress | None = None) -> SaveResult | None:
    """Offer a save to the handlers, in the order they were added. The first that handles it (or fails it) answers;
    None when none did (save it here)."""
    for owner, handler in save_handlers():
        try:
            result = handler(request, progress or (lambda _p, _m: None))
        except Exception:  # noqa: BLE001
            log.exception("save handler of %s failed on %s; saving it here instead", owner, request.path.name)
            continue
        if result is None or result.status == NOT_HANDLED:
            if result is not None and result.reason:
                log.info("save handler of %s passed on %s: %s", owner, request.path.name, result.reason)
            continue
        if result.status not in (HANDLED, FAILED):
            log.warning("save handler of %s gave %r; saving it here instead", owner, result.status)
            continue
        log.info("save of %s %s by %s%s", request.path.name, result.status, owner,
                 f": {result.reason}" if result.reason else "")
        return result
    return None


# ------------------------------------------------------------------ the file's size and time
_written: dict[str, tuple[tuple, tuple, float]] = {}
REMEMBER_WRITTEN_S = 60.0


def same_stamp(a, b, slack: float = STAMP_SLACK) -> bool:
    return a is not None and b is not None and a[0] == b[0] and abs(float(a[1]) - float(b[1])) <= slack


def stamp(path) -> tuple[int, float] | None:
    """(size, modified time) of a file, as a save handler is told to expect it. Right after a handler wrote the file
    somewhere else, a network share can still report the old values for a few seconds: then the values the handler
    reported are used."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    now = (st.st_size, st.st_mtime)
    seen = _written.get(file_key(path))
    if seen is not None and time.monotonic() - seen[2] < REMEMBER_WRITTEN_S and same_stamp(now, seen[0]):
        return seen[1]
    return now


def _remember_written(request: SaveRequest, result: SaveResult) -> None:
    if result.size is None or result.mtime is None or request.expected_size is None:
        return
    _written[file_key(request.path)] = ((request.expected_size, request.expected_mtime),
                                        (int(result.size), float(result.mtime)), time.monotonic())


# ------------------------------------------------------------------ saving
def verify(request: SaveRequest, path=None) -> list[str]:
    """Read the saved file back and compare it with the request: the same checks as after a local save."""
    target = Path(path or request.path)
    cover = (request.cover or None) if request.rewrite else request.cover
    problems = verify_mp4(target, request.keywords, request.genre, cover, explorer.supported(target), request.data)
    problems += verify_meta(target, request.meta)
    if request.rewrite:
        from .media import chapters as chapter_io
        from .media.probe import probe
        try:
            info = probe(target)
        except Exception as exc:  # noqa: BLE001
            return problems + [f"The saved file couldn't be read: {exc}"]
        end = (info.start_time + info.duration) if info.duration else 0
        wanted = [c.title for c in chapter_io.with_ends(request.chapters, end)]
        if [c.title for c in info.chapters] != wanted:
            problems.append("The chapters didn't read back as written.")
    return problems


def save(request: SaveRequest, *, info=None, explorer_mode: str = explorer.MODE_AUTO,
         progress: Progress | None = None, cancel=None) -> SaveOutcome:
    """A queued save: offered to the save handlers first, else written here (tagsync.save_mp4, or
    save_mp4_with_chapters when the chapters change). Raises SaveFailed when a handler failed it."""
    result = offer(request, progress)
    if result is not None:
        if result.status == FAILED:
            raise SaveFailed(result.reason)
        _remember_written(request, result)
        if progress:
            progress(-1, "Checking what was written…")
        problems = list(result.problems)
        try:
            problems += [p for p in verify(request) if p not in problems]
        except Exception as exc:  # noqa: BLE001
            problems.append(f"Couldn't read the saved file back: {exc}")
        explorer.notify_changed(request.path)
        return SaveOutcome(request.path, None, True, problems)
    if request.rewrite:
        if info is None:
            from .media.probe import probe
            info = probe(request.path)
        meta = request.meta if isinstance(request.meta, VideoMeta) else VideoMeta()
        data = None if request.data is UNCHANGED else request.data
        cover = None if request.cover is UNCHANGED else request.cover
        return save_mp4_with_chapters(request.path, info, request.chapters, keywords=request.keywords,
                                      genre=request.genre, cover=cover, meta=meta, explorer_mode=explorer_mode,
                                      preserve_mtime=request.preserve_mtime, progress=progress, cancel=cancel,
                                      data=data)
    return save_mp4(request.path, keywords=request.keywords, genre=request.genre, cover=request.cover,
                    explorer_mode=explorer_mode, preserve_mtime=request.preserve_mtime, progress=progress,
                    meta=request.meta, data=request.data)


def write_copy(request: SaveRequest, source, dest, *, progress: Progress | None = None, cancel=None) -> list[str]:
    """Write the saved version of `source` to `dest` (an .mp4 name, best in the same folder), leaving `source`
    untouched: what a local save of `request` would have made of it, written by the same code. Returns the problems
    the read-back found (like SaveOutcome.problems). Explorer's Tags are written by Runemark's own writer (the
    built-in one Runemark uses wherever Windows' isn't available), and the modified date is left to the caller."""
    source, dest = Path(source), Path(dest)
    if dest.suffix.lower() not in (".mp4", ".m4v"):
        raise SaveError("The working copy needs an .mp4 name.")
    if request.rewrite:
        from .media import chapters as chapter_io
        from .media.probe import probe
        tmp = chapter_io.rewrite_with_chapters(probe(source), request.chapters, progress=progress, cancel=cancel)
        try:
            outcome = save_mp4(tmp, keywords=request.keywords, genre=request.genre, cover=request.cover or None,
                               explorer_mode=explorer.MODE_BUILTIN, progress=progress,
                               meta=request.meta if isinstance(request.meta, VideoMeta) else VideoMeta(),
                               data=None if request.data is UNCHANGED else request.data)
            if tmp.parent == dest.parent:
                os.replace(tmp, dest)
            else:
                shutil.move(str(tmp), str(dest))
        except BaseException:
            if tmp.exists():
                remove_file(tmp)
            raise
        return outcome.problems
    if progress:
        progress(0, "Making a working copy…")
    copy_with_progress(source, dest, progress, "Making a working copy")
    try:
        outcome = save_mp4(dest, keywords=request.keywords, genre=request.genre, cover=request.cover,
                           explorer_mode=explorer.MODE_BUILTIN, progress=progress, meta=request.meta,
                           data=request.data)
    except BaseException:
        remove_file(dest)
        raise
    return outcome.problems
