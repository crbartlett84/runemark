"""Upgrading a curated video to a better copy of it.

The better copy (a sharper or more complete encode of the same video) takes the curated one's place: same folder,
same name, and everything Runemark knows about the curated one (tags, people, title, year and date, description,
rating, studio, links, thumbnail and chapters). Chapters are moved to the matching moments of the better copy,
found by lining the two videos' fingerprints up (duplicates.compare), so a copy with a longer intro or a trimmed
ending still gets its chapters in the right places. A chapter whose moment isn't in the better copy is left out
and named in the report.

Keeping the same path matters to anything that tracks files by path (a media server's play counts and ratings):
it sees the same file, changed.

The better copy is moved into place (written again with the details, without re-encoding; an MKV becomes an
MP4). The curated copy goes to the Recycle Bin, or to a folder the caller names; where neither is possible it's
kept beside the new one, renamed "(replaced)". Nothing is deleted outright.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import duplicates as D
from . import explorer
from .media.chapters import ChapterMark, marks_from_info, renumber
from .media.fingerprint import Fingerprint
from .media.mp4meta import VideoMeta, clean_links
from .media.probe import MediaInfo, probe

log = logging.getLogger(__name__)

Progress = Callable[[int, str], None]

NEAR_RUN_SECONDS = 10.0     # a chapter this close to a matched stretch moves with it (fades, a scene change)
START_SECONDS = 1.0         # a chapter this close to the start stays at the start


@dataclass
class UpgradePlan:
    curated: Path
    better: Path
    final: Path                                  # where the better copy ends up (the curated one's place)
    curated_copy: D.Copy
    better_copy: D.Copy
    marks: list[ChapterMark]                     # the curated chapters, moved to the better copy's timing
    dropped: list[str] = field(default_factory=list)   # chapters whose moment isn't in the better copy
    shift: float = 0.0                           # typical shift (s): better = curated - shift
    relation: str = ""                           # "Same video", "missing 0:40 at 12:00", …
    sharper: bool = True                         # the better copy really is the better one by the quality rule
    notes: list[str] = field(default_factory=list)

    def summary(self) -> list[str]:
        c, b = self.curated_copy, self.better_copy
        lines = [f"{_quality(c)} → {_quality(b)}"]
        if self.relation and self.relation != "Same video":
            lines.append(f"Compared with yours, the better copy: {self.relation[0].lower()}{self.relation[1:]}")
        if self.marks or self.dropped:
            moved = f"{len(self.marks)} chapter{'s' if len(self.marks) != 1 else ''} carried over"
            if abs(self.shift) >= 0.5:
                moved += f", moved {abs(self.shift):.1f} s {'earlier' if self.shift > 0 else 'later'}"
            lines.append(moved)
        lines += [f"Not in the better copy, so left out: {d}" for d in self.dropped]
        if not self.sharper:
            lines.append("By the quality rule (resolution, then bitrate) the other copy isn't better than yours.")
        return lines + self.notes


@dataclass
class UpgradeResult:
    final: Path
    old_now: Path | None                         # where the curated copy went (None: the Recycle Bin)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _quality(c: D.Copy) -> str:
    bits = [f"{c.height}p" if c.height else "unknown resolution"]
    if c.bit_rate:
        bits.append(f"{c.bit_rate / 1_000_000:.1f} Mb/s")
    if c.duration:
        bits.append(D._fmt(c.duration))
    return " · ".join(bits)


def final_path(curated: Path) -> Path:
    return curated if curated.suffix.lower() in (".mp4", ".m4v") else curated.with_suffix(".mp4")


# ------------------------------------------------------------------ chapters
def map_marks(marks: list[ChapterMark], match: D.Match, new_duration: float) -> tuple[list[ChapterMark], list[str],
                                                                                         float]:
    """Move chapters from the curated video (A in `match`) to the better copy (B). Returns (moved marks, chapters
    left out as "Name (12:30)", the typical shift in seconds)."""
    fps = match.fps
    runs = match.runs
    near = NEAR_RUN_SECONDS * fps
    out: list[ChapterMark] = []
    dropped: list[str] = []
    weights: dict[int, int] = {}
    for r in runs:
        weights[r.offset] = weights.get(r.offset, 0) + r.length
    shift = (max(weights, key=weights.get) / fps) if weights else 0.0
    for m in marks:
        if m.start <= START_SECONDS:
            out.append(ChapterMark(0.0, m.name, m.auto))
            continue
        s = m.start * fps
        best, distance = None, None
        for r in runs:
            d = 0.0 if r.a_start <= s <= r.a_start + r.length else min(abs(s - r.a_start),
                                                                          abs(s - (r.a_start + r.length)))
            if distance is None or d < distance:
                best, distance = r, d
        if best is None or distance > near:
            dropped.append(f"{m.name or 'Chapter'} ({D._fmt(m.start)})")
            continue
        t = m.start - best.offset / fps
        if t < 0 or (new_duration and t >= new_duration):
            dropped.append(f"{m.name or 'Chapter'} ({D._fmt(m.start)})")
            continue
        out.append(ChapterMark(round(t, 3), m.name, m.auto))
    seen: set[float] = set()
    unique = []
    for m in sorted(out, key=lambda x: x.start):
        if round(m.start, 1) in seen:
            continue
        seen.add(round(m.start, 1))
        unique.append(m)
    return renumber(unique), dropped, shift


# ------------------------------------------------------------------ planning
def _print_of(db, path: Path, duration: float, cancel) -> Fingerprint:
    from .media import fingerprint as F
    st = path.stat()
    if db is not None:
        fp = db.fingerprint(path, st.st_size, st.st_mtime, F.VERSION)
        if fp is not None:
            return fp
    fp = F.fingerprint(path, duration=duration or None, cancel=cancel)
    if db is not None:
        db.save_fingerprint(path, st.st_size, st.st_mtime, fp)
    return fp


def plan_upgrade(db, curated: str | os.PathLike, better: str | os.PathLike, *, progress: Progress | None = None,
                 cancel: threading.Event | None = None) -> UpgradePlan:
    """Compare the two copies and work out where the curated chapters go. Raises ValueError when the two aren't
    the same video."""
    curated, better = Path(curated), Path(better)
    if curated.resolve() == better.resolve():
        raise ValueError("That's the same file.")
    report = progress or (lambda *_: None)
    report(2, "Reading both copies…")
    c_info, b_info = probe(curated), probe(better)
    c_copy, b_copy = D.describe_copy(str(curated)), D.describe_copy(str(better))
    report(10, f"Reading {curated.name}…")
    a = _print_of(db, curated, c_info.duration or 0.0, cancel)
    report(55, f"Reading {better.name}…")
    b = _print_of(db, better, b_info.duration or 0.0, cancel)
    report(95, "Lining them up…")
    match = D.compare(a, b)
    if match is None or match.kind not in (D.DUPLICATE, D.CUT):
        raise ValueError(f"{better.name} doesn't look like a copy of {curated.name}, so nothing was changed.")
    marks, dropped, shift = map_marks(marks_from_info(c_info), match, b_info.duration or 0.0)
    relation = D.relation(str(curated), str(better), [D.Found(str(curated), str(better), match)])
    sharper = D.best_quality([c_copy, b_copy]).path == str(better)
    plan = UpgradePlan(curated, better, final_path(curated), c_copy, b_copy, marks, dropped, shift, relation, sharper)
    if final_path(curated) != curated:
        plan.notes.append(f"Yours is an MKV: the upgraded video will be {final_path(curated).name}.")
    report(100, "Ready")
    return plan


# ------------------------------------------------------------------ doing it
def _unique(folder: Path, name: str) -> Path:
    target = folder / name
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 2
    while target.exists():
        target = folder / f"{stem} ({n}){suffix}"
        n += 1
    return target


def _rewrite(info: MediaInfo, marks: list[ChapterMark], progress, cancel) -> tuple[Path, list[Path]]:
    """The better copy written again as an MP4 with the curated chapters: (temporary file beside it, sidecars)."""
    from .media import chapters as chapter_io
    from .media import convert as C
    from .tagsync import container_of
    if container_of(info.path) == "mp4":
        return chapter_io.rewrite_with_chapters(info, marks, progress=progress, cancel=cancel), []
    plan = C.plan_conversion(info)
    if not plan.possible:
        raise ValueError("The better copy can't be converted to MP4: " + "; ".join(plan.blockers))
    C.use_marks(plan, marks)
    tmp, sidecars = C.run_conversion(plan, progress=progress, cancel=cancel)
    problems = C.verify_conversion(plan, tmp)
    if problems:
        C.cleanup([tmp, *sidecars])
        raise RuntimeError("The converted file didn't check out, so nothing was changed:\n• " + "\n• ".join(problems))
    return tmp, sidecars


def do_upgrade(db, plan: UpgradePlan, *, old_to: str | os.PathLike | None = None,
               explorer_mode: str = explorer.MODE_AUTO, progress: Progress | None = None,
               cancel: threading.Event | None = None) -> UpgradeResult:
    """Put the better copy in the curated one's place with all its details (see the module docstring)."""
    from .library import history_fields, reindex
    from .tagsync import (
        UNCHANGED,
        free_space_ok,
        merge_unique,
        read_state,
        remove_file,
        retry,
        save_mp4,
    )
    report = progress or (lambda *_: None)
    curated, better, final = plan.curated, plan.better, plan.final
    if not free_space_ok(better.parent, better.stat().st_size):
        raise RuntimeError("Not enough free space to write the better copy again beside it.")
    report(-1, "Reading your copy's details…")
    kept = read_state(curated)
    b_state = read_state(better)
    meta = VideoMeta(**vars(kept.meta))
    meta.links = clean_links([*(kept.meta.links or []), *(b_state.meta.links or [])]) or kept.meta.links
    keywords = merge_unique(kept.keywords, kept.explorer or [])
    info = probe(better)
    tmp, sidecars = _rewrite(info, plan.marks, report, cancel)
    notes: list[str] = []
    placed = None
    try:
        report(-1, "Writing tags, people and thumbnail…")
        outcome = save_mp4(tmp, keywords=keywords, genre=kept.genre, cover=kept.cover or UNCHANGED,
                           explorer_mode=explorer_mode, meta=meta, data=kept.data)
        report(-1, "Putting it in place…")
        placed = final.with_name(f".{final.stem}.runemark-upgrade.mp4")
        if placed.exists():
            remove_file(placed)
        shutil.move(str(tmp), str(placed))
    except BaseException:
        for p in (tmp, placed, *sidecars):
            if p is not None and Path(p).exists():
                remove_file(Path(p))
        raise
    # the curated copy makes room
    old_now: Path | None = None
    try:
        if old_to is not None:
            folder = Path(old_to)
            folder.mkdir(parents=True, exist_ok=True)
            old_now = _unique(folder, curated.name)
            shutil.move(str(curated), str(old_now))
        else:
            try:
                explorer.recycle(curated)
            except explorer.NoRecycleBin:
                old_now = _unique(curated.parent, f"{curated.stem} (replaced){curated.suffix}")
                os.replace(curated, old_now)
                notes.append(f"There's no Recycle Bin there, so your old copy was kept as {old_now.name}; delete it "
                             "once you're happy with the new one.")
    except BaseException:
        remove_file(placed)
        raise
    try:
        retry(lambda: os.replace(placed, final))
    except BaseException:
        if old_now is not None and old_now.exists() and not curated.exists():
            shutil.move(str(old_now), str(curated))
        remove_file(placed)
        raise
    for s in sidecars:                           # subtitles kept beside a converted MKV follow it
        try:
            rest = s.name[len(better.stem):] if s.name.startswith(better.stem) else s.suffix
            shutil.move(str(s), str(_unique(final.parent, final.stem + rest)))
        except OSError as exc:
            notes.append(f"{s.name} stayed beside the better copy's old place ({exc})")
    if better.exists():
        remove_file(better)                      # its picture and sound now live at `final`
    explorer.notify_changed(final)
    if db is not None:
        if final != curated:
            db.move_file(curated, final)
        db.forget_file(better)
        db.record_history(final, **history_fields(kept), label="Upgraded to a better copy")
        db.remember_file(final, keywords)
        reindex(db, final)
    return UpgradeResult(final, old_now, outcome.problems, notes)
