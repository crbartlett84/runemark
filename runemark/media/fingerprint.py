"""Video fingerprints for finding copies: a 64-bit picture hash ("dHash") for every half-second of video.

The hash records the broad light-and-dark pattern of a frame shrunk to 9x8 greyscale (whether each pixel is
brighter than its right-hand neighbour), so it barely changes when a video is re-encoded, resized, converted to
another format or given a different bitrate. Black bars are cropped first, so a letterboxed copy still matches.
Near-flat frames (black, fades, plain title cards) say nothing about which video they're from and are marked, so
matching can ignore them.
"""
from __future__ import annotations

import re
import statistics
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from .. import proc

VERSION = 2                 # bump when the hash changes, so stored fingerprints are recomputed
DEFAULT_FPS = 2.0
W, H = 9, 8
FRAME_BYTES = W * H
FLAT_SPREAD = 6.0           # a frame whose pixels hardly vary (std. dev. below this) is "flat"


class Cancelled(Exception):
    pass


@dataclass
class Fingerprint:
    fps: float
    hashes: list[int]           # one per sample, in time order
    flat: list[bool]            # samples too plain to tell videos apart
    crop: str = ""              # the crop applied (w:h:x:y), "" for none
    version: int = VERSION

    @property
    def duration(self) -> float:
        return len(self.hashes) / self.fps if self.fps else 0.0

    def useful(self) -> int:
        return sum(1 for f in self.flat if not f)

    # stored in the database as bytes: 8 bytes per hash, then one byte per flat flag
    def to_bytes(self) -> bytes:
        return b"".join(h.to_bytes(8, "big") for h in self.hashes) + bytes(int(f) for f in self.flat)

    @classmethod
    def from_bytes(cls, blob: bytes, fps: float, crop: str = "", version: int = VERSION) -> "Fingerprint":
        n = len(blob) // 9
        hashes = [int.from_bytes(blob[i * 8:(i + 1) * 8], "big") for i in range(n)]
        flat = [bool(b) for b in blob[n * 8:n * 9]]
        return cls(fps, hashes, flat, crop, version)


def dhash(pixels: bytes) -> tuple[int, bool]:
    """(hash, flat) for one 9x8 greyscale frame."""
    value = 0
    for y in range(H):
        row = pixels[y * W:(y + 1) * W]
        for x in range(W - 1):
            value = (value << 1) | (row[x] > row[x + 1])
    return value, statistics.pstdev(pixels) < FLAT_SPREAD


def detect_crop(path: str | Path, duration: float | None = None) -> str:
    """Black bars to remove ("" for none). FFmpeg's cropdetect looks at one keyframe every few seconds through the
    whole video, and the crop kept is the largest picture area any of them showed: bars are the same in every
    frame, while a dark scene (which looks like bars on its own) is always contradicted by a brighter one."""
    def run(keyframes_only: bool):
        args = [proc.ffmpeg(), "-hide_banner", "-nostdin", *(["-skip_frame", "nokey"] if keyframes_only else []),
                "-i", str(path), "-an", "-sn", "-dn", "-vf",
                ("select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,5)'," if keyframes_only else "fps=1/2,")
                + "cropdetect=limit=24:round=2:reset=1", "-f", "null", "-"]
        out = proc.run(args, timeout=900, check=False).stderr
        return [tuple(int(v) for v in m.split(b":")) for m in re.findall(rb"crop=(\d+:\d+:\d+:\d+)", out)], out

    boxes, stderr = run(keyframes_only=True)
    if len(boxes) < 12:                       # few keyframes (a short clip): look at a frame every 2 s instead
        boxes, stderr = run(keyframes_only=False)
    sizes = re.search(rb"Stream #\S+ Video: .*?, (\d{2,5})x(\d{2,5})", stderr)
    if not boxes or not sizes:
        return ""
    full_w, full_h = int(sizes.group(1)), int(sizes.group(2))
    x0 = min(b[2] for b in boxes)
    y0 = min(b[3] for b in boxes)
    x1 = max(b[2] + b[0] for b in boxes)
    y1 = max(b[3] + b[1] for b in boxes)
    w, h = x1 - x0, y1 - y0
    if w < 32 or h < 32 or (w >= full_w - 4 and h >= full_h - 4):
        return ""
    return f"{w - w % 2}:{h - h % 2}:{x0}:{y0}"


def fingerprint(path: str | Path, *, duration: float | None = None, fps: float = DEFAULT_FPS,
                cancel: threading.Event | None = None) -> Fingerprint:
    """Read the whole video once (decoded small and greyscale) and hash a frame every 1/fps seconds."""
    crop = detect_crop(path)
    # Each sample is the average of the half-second around it, not one instant: two copies' frames never line up
    # exactly (a trim at 6.3 s, different keyframes, skipped frames), and fast-changing pictures (water, confetti)
    # differ completely from one frame to the next. Averaging first makes the hash depend on the moment, not the frame.
    window = max(2, round(10 / fps))
    filters = ([f"crop={crop}"] if crop else []) + [
        "fps=10", "scale=64:36:flags=area", f"tmix=frames={window}", f"fps={fps}",
        f"scale={W}:{H}:flags=area", "format=gray"]
    # Speed: skip the frames no other frame depends on (the frames that are decoded stay exact), and let FFmpeg
    # use the graphics card if there is one. Not -skip_loop_filter: later frames are built from earlier ones, so
    # skipping the deblocking filter drifts differently in each encoding and copies stop matching.
    args = [proc.ffmpeg(), "-hide_banner", "-nostdin", "-v", "fatal", "-hwaccel", "auto",
            "-skip_frame", "noref", "-i", str(path), "-an", "-sn", "-dn",
            "-vf", ",".join(filters), "-f", "rawvideo", "-"]
    hashes, flat = [], []
    with proc.popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as p:
        while True:
            if cancel is not None and cancel.is_set():
                p.kill()
                raise Cancelled()
            frame = p.stdout.read(FRAME_BYTES)
            if len(frame) < FRAME_BYTES:
                break
            h, f = dhash(frame)
            hashes.append(h)
            flat.append(f)
        err = p.stderr.read().decode("utf-8", "replace")
        p.wait()
    if not hashes:
        raise proc.ToolFailed(args, p.returncode or 1, err or f"no frames read from {Path(path).name}")
    return Fingerprint(fps, hashes, flat, crop)
