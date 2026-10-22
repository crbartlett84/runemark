"""Reading a file's tags from every place they live, and saving them back in sync.

For MP4 the same tag list is written to:
  * Keywords: a standard text field inside the file (read by Mp3tag, ExifTool, ffprobe, ...)
  * Explorer's Tags field (Windows property system / Microsoft Xtra box)
  * Genre: only tags from categories flagged "write to Genre" (Plex, Jellyfin, Explorer's Genre)
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import explorer
from .media import xtra
from .media.mp4meta import (MIN_PADDING, UNCHANGED, VideoMeta, estimate_growth, edit_moves_media, parse_year,
                            read_mp4_tags, split_values, stars_to_value, value_to_stars, write_mp4_tags)
from .media.probe import MediaInfo, MKV_EXTENSIONS, MP4_EXTENSIONS

log = logging.getLogger(__name__)

Progress = Callable[[int, str], None]  # percent (0-100, or -1 for "busy"), message


def merge_unique(*lists: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for values in lists:
        for value in values or []:
            key = value.casefold()
            if key not in seen:
                seen.add(key)
                out.append(value)
    return out


def same_tags(a: list[str], b: list[str]) -> bool:
    return {x.casefold() for x in a} == {x.casefold() for x in b}


def container_of(path: str | os.PathLike) -> str:
    ext = Path(path).suffix.lower()
    if ext in MP4_EXTENSIONS:
        return "mp4"
    if ext in MKV_EXTENSIONS:
        return "mkv"
    return "other"


@dataclass
class SyncReport:
    in_sync: bool
    only_in_keywords: list[str] = field(default_factory=list)
    only_in_explorer: list[str] = field(default_factory=list)


@dataclass
class FileState:
    path: Path
    container: str
    keywords: list[str]
    genre: list[str]
    explorer: list[str] | None          # None when Explorer tags don't apply (MKV)
    cover: bytes | None
    explorer_error: str | None = None
    meta: VideoMeta = field(default_factory=VideoMeta)

    def all_tags(self) -> list[str]:
        return merge_unique(self.keywords, self.explorer, self.genre)

    def sync(self) -> SyncReport:
        if self.explorer is None:
            return SyncReport(True)
        kw = {k.casefold() for k in self.keywords}
        ex = {e.casefold() for e in self.explorer}
        only_kw = [k for k in self.keywords if k.casefold() not in ex]
        only_ex = [e for e in self.explorer if e.casefold() not in kw]
        return SyncReport(not only_kw and not only_ex, only_kw, only_ex)


def read_state(path: str | os.PathLike, info: MediaInfo | None = None,
               cover_reader: Callable[[MediaInfo], bytes | None] | None = None) -> FileState:
    path = Path(path)
    kind = container_of(path)
    if kind == "mp4":
        tags = read_mp4_tags(path)
        explorer_tags: list[str] | None = None
        error = None
        try:
            explorer_tags = explorer.read_tags(path)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            explorer_tags = []
            log.warning("couldn't read Explorer tags for %s: %s", path, exc)
        rating = 0
        try:
            rating = value_to_stars(explorer.read_rating(path))
        except Exception as exc:  # noqa: BLE001
            log.info("couldn't read the rating of %s: %s", path, exc)
        meta = VideoMeta(tags.title, tags.year, tags.description, rating)
        return FileState(path, kind, tags.keywords, tags.genre, explorer_tags, tags.cover, error, meta)
    # MKV (or anything else ffprobe can read): Matroska tags, cover from an image attachment
    keywords: list[str] = []
    genre: list[str] = []
    cover = None
    if info is not None:
        keywords = split_values([info.tag("keywords")]) if info.tag("keywords") else []
        genre = split_values([info.tag("genre")]) if info.tag("genre") else []
        if cover_reader is not None:
            try:
                cover = cover_reader(info)
            except Exception as exc:  # noqa: BLE001
                log.info("no cover read from %s: %s", path, exc)
    meta = VideoMeta()
    if info is not None:
        meta = VideoMeta(info.tag("title"), parse_year(info.tag("date") or info.tag("date_released") or info.tag("year")),
                         info.tag("description") or info.tag("synopsis") or info.tag("comment"), 0)
    return FileState(path, kind, keywords, genre, None, cover, meta=meta)


def route_fields(tags: list[str], *, is_genre: Callable[[str], bool], is_known: Callable[[str], bool],
                 original_genre: list[str]) -> tuple[list[str], list[str]]:
    """Split the tag list into (keywords, genre).

    Every tag goes to Keywords and Explorer. Genre gets tags from flagged categories, plus any
    genre the file already had that isn't in the database yet (so saving never silently drops
    a genre another program wrote; once you file the tag in a category, that category decides).
    """
    original = {g.casefold() for g in original_genre}
    genre = [t for t in tags if is_genre(t) or (not is_known(t) and t.casefold() in original)]
    return list(tags), genre


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------

class SaveError(RuntimeError):
    pass


@dataclass
class SaveOutcome:
    path: Path
    explorer_method: str | None
    used_copy: bool
    problems: list[str]

    @property
    def ok(self) -> bool:
        return not self.problems


def retry(fn, attempts: int = 5, delay: float = 0.4):
    """Windows briefly locks files (Explorer thumbnails, antivirus); try again a few times."""
    for attempt in range(attempts):
        try:
            return fn()
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay * (attempt + 1))
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (32, 33) or attempt == attempts - 1:
                raise
            time.sleep(delay * (attempt + 1))


def copy_with_progress(src: Path, dst: Path, progress: Progress | None = None, label: str = "Copying") -> None:
    total = src.stat().st_size or 1
    done = 0
    chunk = 8 * 1024 * 1024
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            block = fin.read(chunk)
            if not block:
                break
            fout.write(block)
            done += len(block)
            if progress:
                progress(int(done * 100 / total), f"{label}… {done * 100 // total}%")
        fout.flush()
        os.fsync(fout.fileno())
    shutil.copystat(src, dst)


def free_space_ok(folder: Path, needed: int) -> bool:
    try:
        return shutil.disk_usage(folder).free > needed + 64 * 1024 * 1024
    except OSError:
        return True


def _write_explorer(target: Path, tags: list[str], genre: list[str], mode: str, rating: int | None = None) -> str:
    try:
        return explorer.write_tags(target, tags, mode, rating)
    except xtra.NeedsPadding as need:
        log.info("making room for Explorer tags (%d bytes)", need.needed)
        write_mp4_tags(target, keywords=tags, genre=genre, cover=UNCHANGED,
                       min_padding=MIN_PADDING + need.needed, ensure_padding=need.needed + 4096)
        return explorer.write_tags(target, tags, explorer.MODE_BUILTIN, rating)


def verify_meta(target: Path, meta) -> list[str]:
    if not isinstance(meta, VideoMeta):
        return []
    problems = []
    tags = read_mp4_tags(target)
    if tags.title != meta.title.strip():
        problems.append(f"Title read back as: {tags.title or '(none)'}")
    if tags.year != meta.year:
        problems.append(f"Year read back as: {tags.year or '(none)'}")
    if tags.description != meta.description.strip():
        problems.append("The description didn't read back correctly.")
    try:
        stars = value_to_stars(explorer.read_rating(target))
    except Exception as exc:  # noqa: BLE001
        problems.append(f"Couldn't read the rating back: {exc}")
    else:
        if stars != meta.rating:
            problems.append(f"Rating read back as {stars} star(s) instead of {meta.rating}.")
    return problems


def verify_mp4(target: Path, keywords: list[str], genre: list[str], cover, check_explorer: bool) -> list[str]:
    problems = []
    tags = read_mp4_tags(target)
    if [k.casefold() for k in tags.keywords] != [k.casefold() for k in keywords]:
        problems.append(f"Keywords read back as: {', '.join(tags.keywords) or '(none)'}")
    if [g.casefold() for g in tags.genre] != [g.casefold() for g in genre]:
        problems.append(f"Genre read back as: {', '.join(tags.genre) or '(none)'}")
    if cover is not UNCHANGED:
        if cover and tags.cover != cover:
            problems.append("The thumbnail didn't read back correctly.")
        if not cover and tags.cover:
            problems.append("The thumbnail is still there after removing it.")
    if check_explorer:
        try:
            shown = explorer.read_tags(target)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"Couldn't read Explorer's Tags back: {exc}")
        else:
            if not same_tags(shown, keywords):
                problems.append(f"Explorer's Tags read back as: {', '.join(shown) or '(none)'}")
    return problems


def save_mp4(path: str | os.PathLike, *, keywords: list[str], genre: list[str], cover=UNCHANGED,
             explorer_mode: str = explorer.MODE_AUTO, preserve_mtime: bool = False,
             progress: Progress | None = None, meta=UNCHANGED) -> SaveOutcome:
    """Write tags, genre, cover, title/year/description/rating and Explorer's Tags to an MP4,
    then read everything back. meta=UNCHANGED leaves title, year, description and rating alone."""
    path = Path(path)
    if container_of(path) != "mp4":
        raise SaveError("Only MP4 files can be saved directly; MKV files are converted first.")
    stat = path.stat()
    growth = estimate_growth(path, keywords=keywords, genre=genre, cover=cover, meta=meta)
    use_copy = edit_moves_media(path, growth)
    target, tmp = path, None
    if use_copy:
        if not free_space_ok(path.parent, stat.st_size):
            raise SaveError("Not enough free disk space to save this file safely.")
        tmp = path.with_name(f"{path.stem}.runemark-tmp{path.suffix}")
        if progress:
            progress(0, "Making a working copy…")
        copy_with_progress(path, tmp, progress, "Making a working copy")
        target = tmp
    try:
        if progress:
            progress(-1, "Writing tags and thumbnail…")
        retry(lambda: write_mp4_tags(target, keywords=keywords, genre=genre, cover=cover, meta=meta))
        rating = stars_to_value(meta.rating) if isinstance(meta, VideoMeta) else None
        method = None
        if explorer.supported(target):
            if progress:
                progress(-1, "Writing Explorer's Tags…")
            method = retry(lambda: _write_explorer(target, keywords, genre, explorer_mode, rating))
        if progress:
            progress(-1, "Checking what was written…")
        problems = verify_mp4(target, keywords, genre, cover, explorer.supported(target))
        problems += verify_meta(target, meta)
        if tmp is not None:
            retry(lambda: os.replace(tmp, path))
            tmp = None
    finally:
        if tmp is not None and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                log.warning("couldn't remove %s", tmp)
    if preserve_mtime:
        try:
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        except OSError as exc:
            log.info("couldn't restore modified date: %s", exc)
    explorer.notify_changed(path)
    return SaveOutcome(path, method, use_copy, problems)


def save_mp4_with_chapters(path: str | os.PathLike, info: MediaInfo, marks, *, keywords: list[str],
                           genre: list[str], cover: bytes | None, meta: VideoMeta,
                           explorer_mode: str = explorer.MODE_AUTO, preserve_mtime: bool = False,
                           progress: Progress | None = None, cancel=None) -> SaveOutcome:
    """Write new chapters (which means writing the file again, without re-encoding), then the
    tags, thumbnail and details into the new file, check it, and swap it in for the original.

    `cover` and `meta` must be the complete wanted values: the rewrite starts without them.
    """
    from .media import chapters as chapter_io
    path = Path(path)
    if container_of(path) != "mp4":
        raise SaveError("Chapters can only be written to MP4 files; MKV files are converted first.")
    stat = path.stat()
    if not free_space_ok(path.parent, stat.st_size):
        raise SaveError("Not enough free disk space to write the chapters: Runemark needs room for a "
                        "second copy of the file while it works.")
    tmp = chapter_io.rewrite_with_chapters(info, marks, progress=progress, cancel=cancel)
    try:
        outcome = save_mp4(tmp, keywords=keywords, genre=genre, cover=cover or None,
                           explorer_mode=explorer_mode, progress=progress, meta=meta)
        if progress:
            progress(-1, "Replacing the original…")
        retry(lambda: os.replace(tmp, path))
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            log.warning("couldn't remove %s", tmp)
        raise
    if preserve_mtime:
        try:
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        except OSError as exc:
            log.info("couldn't restore modified date: %s", exc)
    explorer.notify_changed(path)
    return SaveOutcome(path, outcome.explorer_method, True, outcome.problems)
