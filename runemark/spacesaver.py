"""Space saver: re-encode big older videos (H.264, MPEG-4…) to HEVC, which needs about half the space for the same
picture and plays directly on most TVs and streaming boxes.

Nothing changes until Chris confirms, one video at a time is rewritten, and each new copy goes through the same path
as Upgrade to a better copy: it takes the old one's place (same folder and name) with every tag, person, detail,
thumbnail and chapter, and the old file goes to the Recycle Bin. A new copy that doesn't check out (wrong length,
not HEVC, not smaller) is thrown away and the original is left alone.

The encoder is the fastest one that works on this PC: Intel Quick Sync, NVIDIA or AMD hardware, else x265 on the
processor (slow but good).
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import proc
from .media.convert import Cancelled
from .media.probe import MediaInfo, probe

log = logging.getLogger(__name__)

Progress = Callable[[int, str], None]

ALREADY_SMALL = {"hevc", "h265", "av1", "vp9"}     # modern codecs: nothing worth gaining
# Bitrate (Mb/s) an HEVC copy needs to look like a good H.264 one, by picture height
HEVC_TARGET_MBPS = [(2160, 14.0), (1440, 8.0), (1080, 5.0), (720, 2.8), (0, 1.5)]
HEVC_SHARE = 0.55            # an HEVC copy at the same quality is about this share of the H.264 size
MIN_SAVING_BYTES = 100 * 1024 * 1024
MIN_SAVING_SHARE = 0.20


@dataclass
class Candidate:
    path: Path
    codec: str
    height: int
    duration: float
    size: int
    bitrate: int                 # whole file, bits per second
    estimate: int                # bytes after re-encoding (estimate)
    reason: str = ""             # why it isn't worth it ("" when it is)

    @property
    def saving(self) -> int:
        return max(0, self.size - self.estimate)

    @property
    def worth_it(self) -> bool:
        return not self.reason


def _target_mbps(height: int) -> float:
    return next(rate for h, rate in HEVC_TARGET_MBPS if height >= h)


def assess(path: str | Path, info: MediaInfo | None = None) -> Candidate:
    """What re-encoding this video would save, without changing it."""
    path = Path(path)
    info = info or probe(path)
    video = info.main_video
    size = path.stat().st_size
    duration = info.duration or 0.0
    codec = (video.codec if video else "") or ""
    height = (video.height if video else 0) or 0
    bitrate = int(size * 8 / duration) if duration else 0
    audio_bps = sum((getattr(s, "bit_rate", None) or 160_000) for s in info.streams if s.kind == "audio")
    target = int((_target_mbps(height) * 1_000_000 + audio_bps) * duration / 8) if duration else size
    estimate = min(int(size * HEVC_SHARE), max(target, int(size * 0.25)))
    c = Candidate(path, codec, height, duration, size, bitrate, estimate)
    if video is None or not duration:
        c.reason, c.estimate = "no video to re-encode", size
    elif codec.lower() in ALREADY_SMALL:
        c.reason, c.estimate = f"already {codec.upper()}", size
    elif c.saving < MIN_SAVING_BYTES or c.saving < size * MIN_SAVING_SHARE:
        c.reason = "already small for its quality"
    return c


# ------------------------------------------------------------------ encoders
ENCODERS = [            # (name, ffmpeg arguments for good quality), best first
    ("hevc_qsv", ["-c:v", "hevc_qsv", "-preset", "medium", "-global_quality", "24"]),
    ("hevc_nvenc", ["-c:v", "hevc_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "24", "-b:v", "0"]),
    ("hevc_amf", ["-c:v", "hevc_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "22", "-qp_p", "24"]),
    ("libx265", ["-c:v", "libx265", "-preset", "medium", "-crf", "23"]),
]
LABELS = {"hevc_qsv": "Intel Quick Sync (fast)", "hevc_nvenc": "NVIDIA graphics (fast)",
          "hevc_amf": "AMD graphics (fast)", "libx265": "the processor (slow, best quality)"}
_chosen: tuple[str, list[str]] | None = None


def choose_encoder() -> tuple[str, list[str]]:
    """The best HEVC encoder that actually works here: each candidate encodes one second of a test picture."""
    global _chosen
    if _chosen is not None:
        return _chosen
    listed = proc.run([proc.ffmpeg(), "-hide_banner", "-encoders"], check=False).stdout.decode("utf-8", "replace")
    for name, args in ENCODERS:
        if not re.search(rf"\b{name}\b", listed):
            continue
        test = proc.run([proc.ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                         "testsrc2=duration=1:size=640x360:rate=25", "-pix_fmt", "yuv420p", *args, "-f", "null", "-"],
                        check=False, timeout=60)
        if test.returncode == 0:
            _chosen = (name, args)
            return _chosen
        log.info("space saver: %s listed but doesn't work here", name)
    raise proc.ToolMissing("No HEVC encoder works in this copy of FFmpeg.")


def encode(c: Candidate, progress: Progress | None = None, cancel: threading.Event | None = None) -> Path:
    """The HEVC copy, written beside the original as a temporary MP4 (video re-encoded, audio copied). The caller
    checks it and puts it in place."""
    name, args = choose_encoder()
    target = c.path.with_name(f".{c.path.stem}.runemark-hevc.mp4")
    cmd = [proc.ffmpeg(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", str(c.path),
           "-map", "0:v:0", "-map", "0:a?", *args, "-pix_fmt", "yuv420p", "-tag:v", "hvc1", "-c:a", "copy",
           "-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart",
           "-f", "mp4", "-progress", "pipe:1", "-nostats", str(target)]
    process = proc.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         text=True, encoding="utf-8", errors="replace")
    err: list[str] = []
    drainer = threading.Thread(target=lambda: err.extend(line.rstrip() for line in process.stderr), daemon=True)
    drainer.start()
    try:
        for line in process.stdout:
            if cancel is not None and cancel.is_set():
                process.kill()
                process.wait()
                raise Cancelled("Cancelled.")
            m = re.match(r"out_time_(?:us|ms)=(\d+)", line.strip())
            if m and c.duration and progress:
                progress(min(99, int(int(m.group(1)) / 1_000_000 * 100 / c.duration)),
                         f"Re-encoding with {LABELS.get(name, name)}…")
        code = process.wait()
        drainer.join(timeout=5)
        if code != 0:
            raise proc.ToolFailed(cmd, code, "\n".join(err))
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def check(c: Candidate, new: Path) -> list[str]:
    """Why the new copy can't replace the original ([] when it can)."""
    try:
        info = probe(new)
    except Exception as exc:  # noqa: BLE001
        return [f"the new copy can't be read ({exc})"]
    problems = []
    v = info.main_video
    if v is None or (v.codec or "").lower() != "hevc":
        problems.append("the new copy isn't HEVC")
    if abs((info.duration or 0) - c.duration) > 1.5:
        problems.append(f"the new copy is {info.duration or 0:.1f} s long, the original {c.duration:.1f} s")
    if new.stat().st_size >= c.size:
        problems.append("the new copy isn't smaller")
    return problems


def shrink(db, c: Candidate, *, progress: Progress | None = None, cancel: threading.Event | None = None,
           old_to: str | Path | None = None):
    """Re-encode one video and put the HEVC copy in its place with everything Runemark knows about it (the original
    goes to the Recycle Bin). Returns the upgrade result; raises with the reason when it couldn't."""
    from . import duplicates as D
    from . import upgrade as U
    from .media.chapters import marks_from_info
    report = progress or (lambda *_: None)
    new = encode(c, report, cancel)
    try:
        problems = check(c, new)
        if problems:
            raise RuntimeError("The re-encoded copy didn't check out, so the original was left alone: "
                               + "; ".join(problems))
        info = probe(c.path)
        plan = U.UpgradePlan(c.path, new, U.final_path(c.path), D.describe_copy(str(c.path)),
                             D.describe_copy(str(new)), marks_from_info(info), relation="Same video", sharper=False,
                             notes=["Space saver: same video, re-encoded to HEVC"])
        return U.do_upgrade(db, plan, old_to=old_to, progress=report, cancel=cancel, label="Re-encoded to save space")
    finally:
        if new.exists():
            new.unlink(missing_ok=True)


def fmt_bytes(n: int) -> str:
    for unit, size in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} B"
