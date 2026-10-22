"""Grab exact frames from a video and turn images into cover JPEGs."""
from __future__ import annotations

import functools
import io
import logging
import math
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

from .. import proc
from .probe import MediaInfo, Stream, packet_times

log = logging.getLogger(__name__)

# Seek a hair before the frame's own timestamp: ffmpeg returns the first frame at or after
# the seek point, so this lands on the intended frame even with rounding in the timestamps.
SEEK_EPSILON = 0.0005


@functools.lru_cache(maxsize=1)
def available_filters() -> frozenset[str]:
    try:
        result = proc.run([proc.ffmpeg(), "-hide_banner", "-filters"], timeout=30)
    except Exception as exc:  # noqa: BLE001
        log.warning("couldn't list ffmpeg filters: %s", exc)
        return frozenset()
    names = set()
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 3:
            names.add(parts[1])
    return frozenset(names)


def can_tonemap() -> bool:
    filters = available_filters()
    return "zscale" in filters and "tonemap" in filters


def _filter_chain(stream: Stream | None, tonemap: bool) -> str:
    filters = []
    if tonemap and stream is not None and stream.is_hdr and can_tonemap():
        filters.append(
            "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
            "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv"
        )
    sar = stream.sar if stream is not None else None
    if sar and sar != 1:
        filters.append("scale=trunc(iw*sar/2)*2:ih,setsar=1")
    filters.append("format=rgb24")
    return ",".join(filters)


def grab_frame(path: str | os.PathLike, seconds: float, stream: Stream | None = None,
               *, max_edge: int | None = None, tonemap: bool = True, timeout: float = 90) -> Image.Image:
    """Decode the frame shown at `seconds` (accurate seek), with aspect ratio and rotation applied."""
    seconds = max(0.0, seconds)
    args = [proc.ffmpeg(), "-v", "error", "-nostdin", "-ss", f"{seconds:.6f}", "-i", str(path)]
    args += ["-map", f"0:{stream.index}" if stream is not None else "0:V:0"]
    args += ["-frames:v", "1", "-vf", _filter_chain(stream, tonemap), "-f", "image2pipe", "-c:v", "png", "-"]
    result = proc.run(args, timeout=timeout)
    if not result.stdout:
        raise RuntimeError(f"No frame at {seconds:.3f}s (past the end of the video?)")
    image = Image.open(io.BytesIO(result.stdout))
    image.load()
    image = image.convert("RGB")
    if max_edge:
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    return image


def frame_time_at(path: str | os.PathLike, stream: Stream, timestamp: float) -> float:
    """Timestamp of the frame on screen at `timestamp`: the last frame starting at or before it.

    Timestamps here are the stream's own presentation times (what ffprobe reports for
    packets and what Qt reports as a video frame's start time).
    """
    try:
        times = packet_times(path, stream.index, timestamp - 3.0, timestamp + 0.5)
    except Exception as exc:  # noqa: BLE001
        log.info("packet scan failed, using raw position: %s", exc)
        return timestamp
    before = [t for t in times if t <= timestamp + 1e-6]
    if before:
        return before[-1]
    return times[0] if times else timestamp


def neighbour_times(path: str | os.PathLike, stream: Stream, pts: float, window: float = 3.0) -> tuple[float | None, float | None]:
    """Timestamps of the frames just before and just after the frame at `pts`."""
    times = packet_times(path, stream.index, pts - window, pts + window)
    earlier = [t for t in times if t < pts - 1e-6]
    later = [t for t in times if t > pts + 1e-6]
    return (earlier[-1] if earlier else None), (later[0] if later else None)


def grab_at_pts(path: str | os.PathLike, info: MediaInfo, pts: float, *,
                max_edge: int | None = None, tonemap: bool = True) -> Image.Image:
    """Decode exactly the frame whose presentation time is `pts`.

    ffmpeg's -ss counts from the file's start time, so subtract it, and aim a hair early
    because ffmpeg returns the first frame at or after the seek point.
    """
    stream = info.main_video
    if stream is None:
        raise RuntimeError("This file has no video track.")
    seek = max(0.0, pts - info.start_time - SEEK_EPSILON)
    return grab_frame(path, seek, stream, max_edge=max_edge, tonemap=tonemap)


def grab_shown_frame(path: str | os.PathLike, info: MediaInfo, timestamp: float, *,
                     max_edge: int | None = None, tonemap: bool = True) -> tuple[Image.Image, float]:
    """The frame on screen at `timestamp` (presentation time), and that frame's own timestamp."""
    stream = info.main_video
    if stream is None:
        raise RuntimeError("This file has no video track.")
    exact = frame_time_at(path, stream, timestamp)
    return grab_at_pts(path, info, exact, max_edge=max_edge, tonemap=tonemap), exact


def filmstrip_times(duration: float | None, count: int) -> list[float]:
    """Evenly spaced moments, skipping the very start and end (often black)."""
    if not duration or duration <= 0 or count <= 0:
        return []
    if count == 1:
        return [duration / 2]
    start, span = duration * 0.03, duration * 0.94
    return [start + span * i / (count - 1) for i in range(count)]


def filmstrip_frames(info: MediaInfo, count: int) -> list[float]:
    """Presentation times for the filmstrip, snapped onto the video's frame grid.

    Snapping means the picture in the filmstrip and the frame the player jumps to are the same
    frame (exactly so for constant frame rate video; within a frame for variable frame rate).
    """
    return snap_to_frames(info, filmstrip_times(info.duration, count))


def snap_to_frames(info: MediaInfo, moments: list[float]) -> list[float]:
    """Turn times measured from the start of the file into presentation times of real frames."""
    stream = info.main_video
    if stream is None:
        return moments
    try:
        first = float(stream.raw.get("start_time"))
    except (TypeError, ValueError):
        first = info.start_time
    fps = float(stream.fps) if stream.fps else 0.0
    out = []
    for t in moments:
        pts = info.start_time + t
        if fps > 0:
            pts = first + max(0, math.floor((pts - first) * fps + 1e-6)) / fps
        out.append(round(pts, 6))
    return out


def to_jpeg(image: Image.Image, *, max_edge: int | None = 1280, quality: int = 90) -> bytes:
    """Encode an image as a baseline JPEG suitable for MP4 cover art."""
    image = ImageOps.exif_transpose(image) if hasattr(image, "getexif") else image
    if image.mode != "RGB":
        background = Image.new("RGB", image.size, (0, 0, 0))
        if image.mode in ("RGBA", "LA") or "transparency" in image.info:
            rgba = image.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
        else:
            background.paste(image.convert("RGB"))
        image = background
    if max_edge and max(image.size) > max_edge:
        image = image.copy()
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=quality, optimize=True, progressive=False)
    return out.getvalue()


def image_file_to_jpeg(path: str | os.PathLike, *, max_edge: int | None = 1280, quality: int = 90) -> bytes:
    with Image.open(path) as image:
        image.load()
        return to_jpeg(image, max_edge=max_edge, quality=quality)


def image_from_bytes(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def dump_attachment(path: str | os.PathLike, stream: Stream) -> bytes:
    """Read an MKV attachment (e.g. cover.jpg) or an MP4 attached picture into memory."""
    if stream.kind == "video":  # MP4 attached picture: copy the encoded image out
        result = proc.run([proc.ffmpeg(), "-v", "error", "-nostdin", "-i", str(path),
                           "-map", f"0:{stream.index}", "-c", "copy", "-frames:v", "1",
                           "-f", "image2pipe", "-"], timeout=60)
        return result.stdout
    with tempfile.TemporaryDirectory(prefix="runemark-") as tmp:
        target = Path(tmp) / "attachment.bin"
        # ffmpeg dumps the attachment while opening the input, then complains that no
        # output was given; the dump itself still succeeds, so don't treat that as failure.
        proc.run([proc.ffmpeg(), "-v", "error", "-nostdin", "-y",
                  f"-dump_attachment:{stream.index}", str(target), "-i", str(path)],
                 timeout=60, check=False)
        if not target.exists() or target.stat().st_size == 0:
            raise RuntimeError("Couldn't read the attachment from the file.")
        return target.read_bytes()
