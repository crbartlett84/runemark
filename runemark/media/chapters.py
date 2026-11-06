"""Chapters: marks you place on frames, numbering, and writing them into an MP4.

A chapter is just a start time and a name; it runs until the next chapter starts (the last one
runs to the end). Names like "Chapter 3" are automatic and follow the chapter's position.

A chapter can also have an end before the next one starts (0.8.2): "End here" where the action stops and filler
begins. Ends live only in Runemark's data block ("chapter_ends", start -> end), never in the chapter track, so
players' timelines and chapter lists stay clean. The Stash plugin sends them as each marker's end, and RuneStash's
moments stop there.

MP4 keeps chapters in a track of their own, so changing them means writing the file again.
Runemark copies every video, audio and subtitle track as is (nothing is re-encoded) into a
new file beside the original, checks it, and only then replaces the original.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import proc
from .atoms import read_layout
from .probe import Chapter, MediaInfo, probe

log = logging.getLogger(__name__)

AUTO_NAME = re.compile(r"^Chapter \d+$")
# 0.11.0: MP4 players need the chapter list to start at 0:00, so when the first real chapter starts later, a
# lead-in chapter called "Start" is written before it. Runemark never shows it (nor makes a marker of it): the
# first chapter you place is the first chapter. A chapter you put at 0:00 yourself stays a chapter.
LEAD_IN = "Start"


@dataclass
class ChapterMark:
    start: float          # presentation time of the chapter's first frame (seconds)
    name: str
    auto: bool = False    # named "Chapter N" by position
    end: float | None = None   # where it stops, when that's before the next chapter (data block only, 0.8.2)

    def key(self) -> tuple:
        return (round(self.start, 3), self.name)


ENDS_KEY = "chapter_ends"


def copy_marks(marks) -> list[ChapterMark]:
    return [ChapterMark(m.start, m.name, m.auto, getattr(m, "end", None)) for m in marks]


def marks_to_json(marks) -> list[dict]:
    """Chapters as plain data, for the save history (0.9.0). Ends aren't included: they live in the data block."""
    return [{"start": round(m.start, 6), "name": m.name, "auto": bool(m.auto)}
            for m in sorted(marks, key=lambda m: m.start)]


def marks_from_json(items) -> list[ChapterMark]:
    return [ChapterMark(float(i["start"]), str(i.get("name") or ""), bool(i.get("auto"))) for i in items or []]


def _start_key(start: float) -> str:
    return f"{start:.3f}"


def apply_ends(marks: list[ChapterMark], data: dict | None) -> list[ChapterMark]:
    """Give the marks the ends recorded in a file's data block (by start time, to the millisecond)."""
    ends = (data or {}).get(ENDS_KEY) or {}
    if isinstance(ends, dict):
        for m in marks:
            try:
                value = ends.get(_start_key(m.start))
                m.end = float(value) if value is not None else None
            except (TypeError, ValueError):
                m.end = None
    return marks


def ends_block(marks: list[ChapterMark]) -> dict:
    """{start: end} for the chapters with a real end: after their start and no later than the next chapter."""
    ordered = sorted(marks, key=lambda m: m.start)
    out = {}
    for i, m in enumerate(ordered):
        end = getattr(m, "end", None)
        if end is None or end <= m.start + 0.05:
            continue
        nxt = ordered[i + 1].start if i + 1 < len(ordered) else None
        if nxt is not None and end > nxt + 0.001:
            continue
        out[_start_key(m.start)] = round(end, 3)
    return out


def chapter_at(marks: list[ChapterMark], pts: float) -> ChapterMark | None:
    """The chapter the playhead is in at pts: the last one starting at or before it (to the millisecond)."""
    before = [m for m in sorted(marks, key=lambda m: m.start) if m.start <= pts + 0.0005]
    return before[-1] if before else None


def end_at(marks: list[ChapterMark], pts: float) -> ChapterMark | None:
    """Set the end of the chapter the playhead is in at pts. Returns that chapter; None when no chapter has
    started yet, or when pts is that chapter's own first frame (an end there would leave nothing of it).
    It never ends the chapter before: at a chapter's start, that one is playing, not the previous one."""
    mark = chapter_at(marks, pts)
    if mark is None or pts <= mark.start + 0.0005:
        return None
    mark.end = round(pts, 3)
    return mark


def is_auto_name(name: str) -> bool:
    return bool(AUTO_NAME.match(name.strip()))


def renumber(marks: list[ChapterMark]) -> list[ChapterMark]:
    """Sort by time and give automatic chapters their position's number."""
    marks = sorted(marks, key=lambda m: m.start)
    for index, mark in enumerate(marks, start=1):
        if mark.auto:
            mark.name = f"Chapter {index}"
    return marks


def is_lead_in(chapters: list, index: int = 0) -> bool:
    """Whether chapters[index] is the hidden lead-in Runemark writes: the first chapter, at 0:00, named "Start",
    with a later chapter after it."""
    if index != 0 or len(chapters) < 2:
        return False
    first = chapters[0]
    name = (getattr(first, "title", None) if hasattr(first, "title") else getattr(first, "name", "")) or ""
    return first.start <= 0.0005 and name.strip() == LEAD_IN


def without_lead_in(chapters: list) -> list:
    return list(chapters[1:]) if is_lead_in(chapters) else list(chapters)


def marks_from_info(info: MediaInfo) -> list[ChapterMark]:
    """The file's existing chapters, ready to edit (the hidden lead-in left out)."""
    marks = []
    for chap in without_lead_in(sorted(info.chapters, key=lambda c: c.start)):
        name = chap.title.strip() or ""
        auto = not name or is_auto_name(name)
        marks.append(ChapterMark(max(0.0, chap.start), name, auto))   # ends: apply_ends(marks, state.data)
    return renumber(marks)


def add_mark(marks: list[ChapterMark], start: float, at_zero_too: bool = False) -> tuple[list[ChapterMark], ChapterMark]:
    """Add an automatic chapter at `start` (or return the one already there).

    0.11.0: the first chapter is just that chapter. No chapter at 0:00 is added any more (it got in the way of
    skipping to the first real chapter and was deleted by hand every time); at_zero_too=True brings it back.
    """
    for mark in marks:
        if abs(mark.start - start) < 0.0005:
            return marks, mark
    new = ChapterMark(start, "", auto=True)
    marks = list(marks) + [new]
    if at_zero_too and len(marks) == 1 and start > 0.5:
        marks.append(ChapterMark(0.0, "", auto=True))
    return renumber(marks), new


def same_marks(a: list[ChapterMark], b: list[ChapterMark]) -> bool:
    return [m.key() for m in sorted(a, key=lambda m: m.start)] == [m.key() for m in sorted(b, key=lambda m: m.start)]


def with_ends(marks: list[ChapterMark], end: float) -> list[Chapter]:
    """Chapters with end times: each runs to the next one, the last to `end`. When the first starts after 0:00,
    the hidden "Start" lead-in comes first (MP4 chapter lists begin at 0:00)."""
    ordered = sorted(marks, key=lambda m: m.start)
    out = []
    if ordered and ordered[0].start > 0.0005:
        out.append(Chapter(0.0, ordered[0].start, LEAD_IN))
    for i, mark in enumerate(ordered):
        stop = ordered[i + 1].start if i + 1 < len(ordered) else end
        out.append(Chapter(mark.start, max(stop, mark.start), mark.name.strip() or f"Chapter {i + 1}"))
    return out


def ffmetadata(chapters: list[Chapter], offset: float = 0.0) -> str:
    """Chapters in FFmpeg's metadata file format. `offset` shifts them onto the output's timeline."""
    def esc(text: str) -> str:
        return re.sub(r"([=;#\\\n])", r"\\\1", text)

    lines = [";FFMETADATA1"]
    for chap in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                  f"START={0 if chap.start <= 0.0005 else max(0, round((chap.start + offset) * 1000))}",
                  f"END={max(0, round((chap.end + offset) * 1000))}"]
        if chap.title:
            lines.append(f"title={esc(chap.title)}")
    return "\n".join(lines) + "\n"


def write_metadata_file(chapters: list[Chapter], offset: float = 0.0) -> Path:
    handle, name = tempfile.mkstemp(prefix="runemark-chapters-", suffix=".txt")
    with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
        out.write(ffmetadata(chapters, offset))
    return Path(name)


class Cancelled(RuntimeError):
    pass


def temp_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.runemark-chapters{path.suffix}")


def remux_command(info: MediaInfo, target: Path, chapter_file: Path | None) -> list[str]:
    args = [proc.ffmpeg(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", str(info.path)]
    if chapter_file is not None:
        args += ["-f", "ffmetadata", "-i", str(chapter_file), "-map_chapters", "1"]
    else:
        args += ["-map_chapters", "-1"]
    args += ["-map_metadata", "0"]
    # Video, audio and subtitles; the old chapter track (a data track) is replaced, and the
    # cover is written again by Runemark afterwards.
    for s in info.streams:
        if (s.kind == "video" and not s.is_attached_pic) or s.kind in ("audio", "subtitle"):
            args += ["-map", f"0:{s.index}"]
    args += ["-c", "copy"]
    if any(s.codec == "hevc" for s in info.video_streams):
        args += ["-tag:v", "hvc1"]
    try:
        if read_layout(info.path).moov_before_media:
            args += ["-movflags", "+faststart"]
    except Exception:  # noqa: BLE001
        pass
    args += ["-f", "mp4", "-progress", "pipe:1", "-nostats", str(target)]
    return args


def extra_tracks_dropped(info: MediaInfo) -> int:
    """Data tracks other than the chapter track (camera timecode, GPS...) that a rewrite leaves out."""
    data = [s for s in info.streams if s.kind not in ("video", "audio", "subtitle", "attachment")]
    return max(0, len(data) - (1 if info.chapters else 0))


def rewrite_with_chapters(info: MediaInfo, marks: list[ChapterMark], *,
                          progress: Callable[[int, str], None] | None = None,
                          cancel: threading.Event | None = None) -> Path:
    """Copy the MP4 into a new file beside it with these chapters. Returns the new file,
    checked against the original (length, track counts, chapters). The caller writes the
    tags and thumbnail into it and then swaps it in."""
    source = Path(info.path)
    target = temp_path(source)
    end = (info.start_time + info.duration) if info.duration else (marks[-1].start + 1 if marks else 0)
    chapters = with_ends(marks, end)
    chapter_file = write_metadata_file(chapters) if chapters else None
    duration = info.duration or 0
    try:
        process = proc.popen(remux_command(info, target, chapter_file), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                             text=True, encoding="utf-8", errors="replace")
        errors: list[str] = []
        drain = threading.Thread(target=lambda: errors.extend(process.stderr), daemon=True)
        drain.start()
        for line in process.stdout:
            if cancel is not None and cancel.is_set():
                process.kill()
                process.wait()
                raise Cancelled("Cancelled.")
            match = re.match(r"out_time_(?:us|ms)=(\d+)", line.strip())
            if match and duration and progress:
                done = int(match.group(1)) / 1_000_000
                progress(min(99, int(done * 100 / duration)), "Writing the chapters…")
        code = process.wait()
        drain.join(timeout=5)
        if code != 0:
            raise proc.ToolFailed(["ffmpeg"], code, "".join(errors))
        problems = verify_rewrite(info, target, chapters)
        if problems:
            raise RuntimeError("The rewritten file didn't check out, so nothing was changed:\n• "
                               + "\n• ".join(problems))
        return target
    except BaseException:
        try:
            if target.exists():
                target.unlink()
        except OSError:
            log.warning("couldn't remove %s", target)
        raise
    finally:
        if chapter_file is not None:
            try:
                chapter_file.unlink()
            except OSError:
                pass


def verify_rewrite(info: MediaInfo, target: Path, chapters: list[Chapter]) -> list[str]:
    try:
        out = probe(target)
    except Exception as exc:  # noqa: BLE001
        return [f"The new file couldn't be read: {exc}"]
    problems = []
    src, new = info.duration or 0, out.duration or 0
    if src and abs(src - new) > max(1.0, src * 0.005):
        problems.append(f"Length changed from {src:.1f}s to {new:.1f}s.")
    for kind, before, after in (("video", info.video_streams, out.video_streams),
                                ("audio", info.audio_streams, out.audio_streams),
                                ("subtitle", info.subtitle_streams, out.subtitle_streams)):
        if len(before) != len(after):
            problems.append(f"Expected {len(before)} {kind} track(s), found {len(after)}.")
    if [c.title for c in out.chapters] != [c.title for c in chapters]:
        problems.append("The chapters didn't read back as written.")
    return problems
