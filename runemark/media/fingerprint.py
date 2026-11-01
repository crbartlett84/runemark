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
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .. import proc

VERSION = 1                 # bump when the hash changes, so stored fingerprints are recomputed
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


def detect_crop(path: str | Path, duration: float | None) -> str:
    """Black bars to remove, found by FFmpeg's cropdetect on frames spread through the video ("" for none)."""
    start = max(0.0, (duration or 0) * 0.1)
    args = [proc.ffmpeg(), "-hide_banner", "-nostdin", "-ss", f"{start:.2f}", "-i", str(path), "-an", "-sn",
            "-vf", "fps=1/2,cropdetect=limit=24:round=2:reset=1", "-frames:v", "30", "-f", "null", "-"]
    result = proc.run(args, timeout=300, check=False)
    found = Counter(re.findall(rb"crop=(\d+:\d+:\d+:\d+)", result.stderr))
    if not found:
        return ""
    crop = found.most_common(1)[0][0].decode()
    w, h, x, y = (int(v) for v in crop.split(":"))
    return "" if (x == 0 and y == 0) or w < 32 or h < 32 else crop


def fingerprint(path: str | Path, *, duration: float | None = None, fps: float = DEFAULT_FPS,
                cancel: threading.Event | None = None) -> Fingerprint:
    """Read the whole video once (decoded small and greyscale) and hash a frame every 1/fps seconds."""
    crop = detect_crop(path, duration)
    filters = ([f"crop={crop}"] if crop else []) + [f"fps={fps}", f"scale={W}:{H}:flags=area", "format=gray"]
    args = [proc.ffmpeg(), "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-an", "-sn", "-dn",
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
