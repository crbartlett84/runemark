"""Thumbnail suggestions: sample frames across the video and pick a varied set of good ones.

"Good" means sharp, well exposed, with some contrast and colour; frames that are black,
washed out, flat (fades, title cards) or blurry are skipped, and near-duplicates are dropped
so the suggestions show different moments.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from PIL import Image, ImageFilter, ImageStat

from . import frames
from .probe import MediaInfo

log = logging.getLogger(__name__)


@dataclass
class Suggestion:
    pts: float
    image: Image.Image
    score: float


@dataclass
class _Candidate:
    pts: float
    image: Image.Image
    sharp: float
    contrast: float
    colour: float
    brightness: float
    signature: list[int]


def measure(pts: float, image: Image.Image) -> _Candidate:
    gray = image.convert("L")
    stat = ImageStat.Stat(gray)
    edges = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES))
    sat = ImageStat.Stat(image.convert("HSV").getchannel("S"))
    small = gray.resize((24, 14))
    return _Candidate(pts, image, edges.var[0], stat.stddev[0], sat.mean[0], stat.mean[0], list(small.tobytes()))


def usable(c: _Candidate) -> bool:
    return 30 <= c.brightness <= 225 and c.contrast >= 18


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    for rank, i in enumerate(order):
        out[i] = rank / max(1, len(values) - 1)
    return out


def _distance(a: list[int], b: list[int]) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def pick(candidates: list[_Candidate], count: int, min_distance: float = 10.0, min_gap: float = 0.0) -> list[Suggestion]:
    good = [c for c in candidates if usable(c)] or list(candidates)
    if not good:
        return []
    sharp, contrast, colour = _ranks([c.sharp for c in good]), _ranks([c.contrast for c in good]), \
        _ranks([c.colour for c in good])
    exposure = [1 - abs(c.brightness - 120) / 120 for c in good]
    scored = sorted(((0.45 * sharp[i] + 0.3 * contrast[i] + 0.15 * colour[i] + 0.1 * exposure[i], c)
                     for i, c in enumerate(good)), key=lambda x: -x[0])
    chosen: list[tuple[float, _Candidate]] = []
    # strictest first: different-looking and spread out; then just spread out; then anything
    for distance, gap in ((min_distance, min_gap), (0.0, min_gap), (0.0, 0.0)):
        for score, c in scored:
            if len(chosen) == count:
                break
            if any(c is other for _, other in chosen):
                continue
            if all(_distance(c.signature, other.signature) >= distance and abs(c.pts - other.pts) >= gap
                   for _, other in chosen):
                chosen.append((score, c))
    chosen.sort(key=lambda x: x[1].pts)
    return [Suggestion(c.pts, c.image, round(score, 3)) for score, c in chosen]


def candidate_times(info: MediaInfo, samples: int) -> list[float]:
    duration = info.duration or 0
    if duration <= 0:
        return []
    fps = float(info.main_video.fps) if info.main_video is not None and info.main_video.fps else 25.0
    samples = max(1, min(samples, int(duration * fps * 0.9)))
    start, span = duration * 0.04, duration * 0.92
    moments = [start + span * (i + 0.5) / samples for i in range(samples)]
    return sorted(set(frames.snap_to_frames(info, moments)))


def suggest(info: MediaInfo, count: int = 8, samples: int = 36, *, edge: int = 320,
            progress: Callable[[int, str], None] | None = None,
            cancel: threading.Event | None = None, workers: int = 4) -> list[Suggestion]:
    """Up to `count` good, varied frames (presentation times and small pictures)."""
    if info.main_video is None:
        return []
    times = candidate_times(info, samples)
    found: list[_Candidate] = []
    lock = threading.Lock()

    def grab(pts: float):
        if cancel is not None and cancel.is_set():
            return
        try:
            image = frames.grab_at_pts(info.path, info, pts, max_edge=edge, tonemap=True)
        except Exception as exc:  # noqa: BLE001
            log.info("suggestion frame %.3f failed: %s", pts, exc)
            return
        candidate = measure(pts, image)
        with lock:
            found.append(candidate)
            if progress:
                progress(int(len(found) * 100 / len(times)), "Looking for good frames…")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(grab, times))
    if cancel is not None and cancel.is_set():
        return []
    return pick(found, count, min_gap=(info.duration or 0) / (count * 3))
