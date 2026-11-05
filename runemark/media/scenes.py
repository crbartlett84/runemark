"""Suggested cut points (0.9.0): where the picture changes completely (a new shot or scene), found with FFmpeg's
scene filter in a background pass and kept per file, so it's done once. They're suggestions for chapter starts:
`[` and `]` jump between them, and adding a chapter near one snaps to it."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable

from .. import proc
from ..paths import IS_WINDOWS, app_data_dir
from .probe import MediaInfo

log = logging.getLogger(__name__)

THRESHOLD = 0.30          # how different two frames must be (0-1) to count as a cut
MIN_GAP = 1.0             # cuts closer together than this (flashes, strobes) count once
SNAP = 1.0                # adding a chapter within this many seconds of a cut starts it on the cut
CACHE_VERSION = 1

_PTS = re.compile(r"pts_time:\s*(-?[0-9.]+)")
_OUT_TIME = re.compile(r"out_time_us=(\d+)")


class Cancelled(RuntimeError):
    pass


def _cache_dir() -> Path:
    folder = app_data_dir() / "cache" / "cuts"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _fingerprint(info: MediaInfo) -> dict:
    """What has to match for cached cuts to belong to this file's picture. Saving tags, chapters or a thumbnail
    doesn't change it (the picture is the same); re-encoding or a different copy does."""
    video = info.main_video
    return {"v": CACHE_VERSION, "duration": round(info.duration or 0, 2),
            "codec": getattr(video, "codec", "") if video else "",
            "size": [getattr(video, "width", 0), getattr(video, "height", 0)] if video else [],
            "threshold": THRESHOLD}


def _cache_file(path: str | os.PathLike) -> Path:
    from ..tagdb import file_key
    return _cache_dir() / (hashlib.sha1(file_key(path).encode("utf-8")).hexdigest() + ".json")


def cached_cuts(path: str | os.PathLike, info: MediaInfo) -> list[float] | None:
    """The cuts found before for this file (None if it hasn't been looked through, or has changed since)."""
    try:
        data = json.loads(_cache_file(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("fingerprint") != _fingerprint(info):
        return None
    return [float(c) for c in data.get("cuts", [])]


def store_cuts(path: str | os.PathLike, info: MediaInfo, cuts: list[float]) -> None:
    try:
        _cache_file(path).write_text(json.dumps({"path": str(path), "fingerprint": _fingerprint(info),
                                                 "cuts": [round(c, 4) for c in cuts]}), encoding="utf-8")
    except OSError as exc:
        log.info("couldn't keep the cuts of %s: %s", path, exc)


def move_cache(old: str | os.PathLike, new: str | os.PathLike) -> None:
    """A video was moved or renamed: its cuts go with it."""
    try:
        src = _cache_file(old)
        if src.exists():
            os.replace(src, _cache_file(new))
    except OSError:
        pass


def thin(times: list[float], gap: float = MIN_GAP) -> list[float]:
    out: list[float] = []
    for t in sorted(times):
        if not out or t - out[-1] >= gap:
            out.append(t)
    return out


def detect_cuts(info: MediaInfo, *, threshold: float = THRESHOLD, progress: Callable[[int, str], None] | None = None,
                cancel: threading.Event | None = None) -> list[float]:
    """Look through the whole video (small and at low priority, sound skipped) for cuts. Presentation times of
    each new shot's first frame, the very start left out."""
    video = info.main_video
    if video is None:
        return []
    # Decoding is nearly all the work. Frames nothing refers to are skipped, and so is the deblocking filter (0.9.1):
    # the picture is shrunk to 256 px before it's compared, where deblocking makes no difference, and the same cuts
    # come out about 10-20% sooner.
    args = [proc.ffmpeg(), "-hide_banner", "-nostdin", "-v", "info", "-copyts", "-skip_frame", "noref",
            "-skip_loop_filter", "all", "-i", str(info.path), "-map", f"0:{video.index}", "-an", "-sn", "-dn",
            "-vf", f"scale=256:-2,select='gt(scene\\,{threshold})',showinfo", "-f", "null", "-",
            "-progress", "pipe:2", "-stats_period", "1"]
    flags = proc.NO_WINDOW | (getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0) if IS_WINDOWS else 0)
    kwargs = {} if IS_WINDOWS else {"preexec_fn": lambda: os.nice(10)}
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                               creationflags=flags, **kwargs)
    found: list[float] = []
    duration = info.duration or 0
    tail: list[str] = []
    try:
        for raw in process.stderr:
            if cancel is not None and cancel.is_set():
                process.kill()
                raise Cancelled("stopped")
            line = raw.decode("utf-8", "replace")
            if "showinfo" in line and "pts_time" in line:
                m = _PTS.search(line)
                if m:
                    found.append(float(m.group(1)))
                continue
            m = _OUT_TIME.search(line)
            if m and progress and duration > 0:
                progress(min(99, int(int(m.group(1)) / 1e6 * 100 / duration)), "Looking for cuts…")
                continue
            tail = (tail + [line.strip()])[-6:]
        code = process.wait()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    if code != 0:
        raise RuntimeError("FFmpeg couldn't look through the video: " + " | ".join(t for t in tail if t))
    start = info.start_time or 0.0
    return [t for t in thin(found) if t > start + 0.5]


def nearest(cuts: list[float], pts: float, within: float = SNAP) -> float | None:
    """The cut closest to pts, if it's within `within` seconds."""
    best = min(cuts or [], key=lambda c: abs(c - pts), default=None)
    return best if best is not None and abs(best - pts) <= within else None


def neighbour(cuts: list[float], pts: float, direction: int) -> float | None:
    """The next cut after pts (direction 1) or the one before it (-1), ignoring one right at pts."""
    if direction > 0:
        return next((c for c in sorted(cuts) if c > pts + 0.05), None)
    return next((c for c in sorted(cuts, reverse=True) if c < pts - 0.05), None)
