"""Convert MKV to MP4 without re-encoding video, after showing what happens to every track."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import proc
from .probe import Chapter, MediaInfo, Stream, codec_label, language_name, probe

log = logging.getLogger(__name__)

# What each track can become
COPY = "copy"
TO_AAC = "aac"
TO_FLAC = "flac"
TO_MOVTEXT = "mov_text"
SIDECAR = "sidecar"
DROP = "drop"

ACTION_LABELS = {
    COPY: "Keep as is",
    TO_AAC: "Convert to AAC",
    TO_FLAC: "Convert to FLAC (lossless)",
    TO_MOVTEXT: "Convert to MP4 text subtitles",
    SIDECAR: "Save beside the video",
    DROP: "Leave out",
}

VIDEO_COPY = {"h264", "hevc", "av1", "vp9", "mpeg4", "mpeg2video", "mjpeg"}
AUDIO_COPY = {"aac", "mp3", "ac3", "eac3", "alac", "flac", "opus", "dts", "mp2"}
AUDIO_LOSSLESS = {"truehd", "mlp", "wavpack", "tta", "ape"}  # plus pcm_*
TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "webvtt", "text", "mov_text"}
IMAGE_SUBS_SIDECAR = {"hdmv_pgs_subtitle": ".sup", "dvd_subtitle": ".mks", "dvb_subtitle": ".mks"}
TEXT_SIDECAR = {"subrip": ".srt", "srt": ".srt", "ass": ".ass", "ssa": ".ssa", "webvtt": ".vtt"}

AAC_KBPS_PER_CHANNEL = 80


@dataclass
class TrackPlan:
    stream: Stream
    action: str
    choices: list[str]
    note: str = ""

    @property
    def kind(self) -> str:
        return self.stream.kind

    def describe(self) -> str:
        s = self.stream
        parts = [codec_label(s)]
        if s.kind == "audio":
            channels = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(s.channels or 0)
            if channels:
                parts.append(channels)
        if s.kind in ("audio", "subtitle"):
            parts.append(language_name(s.language))
        if s.title:
            parts.append(f"“{s.title}”")
        if s.kind == "attachment" or s.is_attached_pic:
            parts = [s.filename or s.mimetype or "attachment"]
        if s.is_forced:
            parts.append("forced")
        return " · ".join(parts)

    @property
    def kept_in_mp4(self) -> bool:
        return self.action in (COPY, TO_AAC, TO_FLAC, TO_MOVTEXT)


@dataclass
class ConversionPlan:
    info: MediaInfo
    output: Path
    tracks: list[TrackPlan]
    blockers: list[str] = field(default_factory=list)
    cover_stream: Stream | None = None
    chapters: list[Chapter] | None = None   # replacement chapters; None copies the MKV's as they are
    chapter_note: str = ""

    @property
    def source(self) -> Path:
        return self.info.path

    @property
    def possible(self) -> bool:
        return not self.blockers

    def kept(self) -> list[TrackPlan]:
        return [t for t in self.tracks if t.kept_in_mp4]

    def sidecars(self) -> list[TrackPlan]:
        return [t for t in self.tracks if t.action == SIDECAR]

    def expected_counts(self) -> dict[str, int]:
        counts = {"video": 0, "audio": 0, "subtitle": 0}
        for t in self.kept():
            counts[t.kind] = counts.get(t.kind, 0) + 1
        return counts

    def losses(self) -> list[str]:
        """Plain-language list of what changes, for the confirmation dialog."""
        out = []
        for t in self.tracks:
            if t.action == COPY:
                continue
            out.append(f"{t.describe()}: {ACTION_LABELS[t.action].lower()}")
        return out


def default_output(source: Path) -> Path:
    candidate = source.with_suffix(".mp4")
    n = 1
    while candidate.exists():
        candidate = source.with_name(f"{source.stem} ({n}).mp4")
        n += 1
    return candidate


def plan_conversion(info: MediaInfo, output: Path | None = None) -> ConversionPlan:
    tracks: list[TrackPlan] = []
    blockers: list[str] = []
    cover = None
    videos = 0
    for s in info.streams:
        if s.kind == "video" and s.is_attached_pic:
            if cover is None:
                cover = s
            tracks.append(TrackPlan(s, DROP, [DROP], "Cover image: used as the thumbnail unless you pick a frame"))
        elif s.kind == "video":
            videos += 1
            if s.codec in VIDEO_COPY:
                note = "Stored as 'hvc1' so Windows and Apple players recognise it" if s.codec == "hevc" else ""
                tracks.append(TrackPlan(s, COPY, [COPY], note))
            else:
                tracks.append(TrackPlan(s, DROP, [DROP]))
                blockers.append(f"The video is {codec_label(s)}, which MP4 can't hold without re-encoding "
                                f"the picture. Runemark doesn't re-encode video, so this file can't be converted.")
        elif s.kind == "audio":
            if s.codec in AUDIO_COPY:
                tracks.append(TrackPlan(s, COPY, [COPY, DROP]))
            elif s.codec in AUDIO_LOSSLESS or s.codec.startswith("pcm_"):
                note = "Lossless audio stays lossless" + (
                    "; Dolby Atmos height data isn't kept" if s.codec == "truehd" else "")
                tracks.append(TrackPlan(s, TO_FLAC, [TO_FLAC, TO_AAC, DROP], note))
            else:
                tracks.append(TrackPlan(s, TO_AAC, [TO_AAC, DROP], "MP4 has no standard place for this format"))
        elif s.kind == "subtitle":
            if s.codec == "mov_text":
                tracks.append(TrackPlan(s, COPY, [COPY, DROP]))
            elif s.codec in TEXT_SUBS:
                note = "Styling (fonts, colours, positions) is lost inside MP4" if s.codec in ("ass", "ssa") else ""
                choices = [TO_MOVTEXT, DROP]
                if s.codec in TEXT_SIDECAR:
                    choices.insert(1, SIDECAR)
                tracks.append(TrackPlan(s, TO_MOVTEXT, choices, note))
            elif s.codec in IMAGE_SUBS_SIDECAR:
                tracks.append(TrackPlan(s, SIDECAR, [SIDECAR, DROP],
                                        "Picture-based subtitles can't go inside an MP4"))
            else:
                tracks.append(TrackPlan(s, DROP, [DROP], "This subtitle format can't go into MP4"))
        elif s.kind == "attachment":
            note = "Fonts are only used by styled subtitles" if "font" in s.mimetype or s.filename.lower().endswith(
                (".ttf", ".otf")) else ""
            tracks.append(TrackPlan(s, DROP, [DROP], note))
        else:
            tracks.append(TrackPlan(s, DROP, [DROP], "Extra data track"))
    if videos == 0:
        blockers.append("This file has no video track.")
    chapters, chapter_note = fit_chapters(info)
    return ConversionPlan(info, output or default_output(info.path), tracks, blockers, cover,
                          chapters, chapter_note)


def fit_chapters(info: MediaInfo) -> tuple[list[Chapter] | None, str]:
    """Keep chapters inside the video.

    In an MP4 the chapters are a track of their own, and a chapter running past the end makes
    Windows and media servers report the movie as longer than it is. Returns (chapters to write
    instead, a note for the user), or (None, "") when the MKV's chapters can be copied as they are.
    """
    if not info.chapters or not info.duration:
        return None, ""
    return fit_chapter_list(info.chapters, info.start_time + info.duration)


def fit_chapter_list(chapters: list[Chapter], end: float, always: bool = False) -> tuple[list[Chapter] | None, str]:
    """Trim `chapters` to the video's end. Returns (None, "") when nothing needed changing,
    unless `always` (then the list is returned either way)."""
    kept: list[Chapter] = []
    dropped = trimmed = 0
    for chap in chapters:
        if chap.start >= end - 0.5:
            dropped += 1
        elif chap.end > end + 0.05:
            kept.append(Chapter(chap.start, end, chap.title))
            trimmed += 1
        else:
            kept.append(chap)
    if not dropped and not trimmed:
        return (kept if always else None), ""
    notes = []
    if dropped:
        notes.append(f"{dropped} chapter{'s' if dropped != 1 else ''} after the end of the video "
                     f"{'are' if dropped != 1 else 'is'} left out")
    if trimmed:
        notes.append("the last chapter is trimmed to the end of the video")
    note = "; ".join(notes)
    return kept, note[0].upper() + note[1:] + ", so Windows shows the right length."


def ffmetadata_chapters(chapters: list[Chapter], offset: float = 0.0) -> str:
    """Chapters in FFmpeg's metadata file format."""
    from .chapters import ffmetadata
    return ffmetadata(chapters, offset)


def use_marks(plan: "ConversionPlan", marks) -> None:
    """Convert with these chapters (edited in Runemark) instead of the MKV's own."""
    from .chapters import with_ends
    info = plan.info
    end = info.start_time + (info.duration or 0)
    chapters, note = fit_chapter_list(with_ends(marks, end), end, always=True)
    plan.chapters, plan.chapter_note = chapters, note


def sidecar_paths(plan: ConversionPlan) -> dict[int, Path]:
    """File names that Plex, Jellyfin and most players pick up: Movie.eng.forced.sup."""
    out: dict[int, Path] = {}
    used: set[str] = set()
    stem = plan.output.with_suffix("")
    for t in plan.sidecars():
        s = t.stream
        ext = IMAGE_SUBS_SIDECAR.get(s.codec) or TEXT_SIDECAR.get(s.codec, ".mks")
        parts = [s.language or "und"]
        if s.is_forced:
            parts.append("forced")
        base = f"{stem}.{'.'.join(parts)}"
        name = base + ext
        n = 2
        while name.casefold() in used or Path(name).exists():
            name = f"{base}.{n}{ext}"
            n += 1
        used.add(name.casefold())
        out[s.index] = Path(name)
    return out


def temp_output(plan: ConversionPlan) -> Path:
    return plan.output.with_name(f"{plan.output.stem}.runemark-tmp.mp4")


def build_command(plan: ConversionPlan, target: Path, chapter_file: Path | None = None) -> list[str]:
    args = [proc.ffmpeg(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", str(plan.source)]
    if plan.chapters is None:
        args += ["-map_metadata", "0", "-map_chapters", "0"]
    elif plan.chapters and chapter_file is not None:
        args += ["-f", "ffmetadata", "-i", str(chapter_file), "-map_metadata", "0", "-map_chapters", "1"]
    else:
        args += ["-map_metadata", "0", "-map_chapters", "-1"]
    codec_args: list[str] = []
    out_index = 0
    for t in plan.tracks:
        if not t.kept_in_mp4:
            continue
        s = t.stream
        args += ["-map", f"0:{s.index}"]
        spec = str(out_index)
        if t.action == COPY:
            codec_args += [f"-c:{spec}", "copy"]
            if s.codec == "hevc":
                codec_args += [f"-tag:{spec}", "hvc1"]
        elif t.action == TO_AAC:
            channels = min(s.channels or 2, 8)
            layout = {1: "mono", 2: "stereo", 3: "2.1", 4: "quad", 5: "5.0", 6: "5.1", 7: "6.1", 8: "7.1"}[channels]
            codec_args += [f"-c:{spec}", "aac", f"-b:{spec}", f"{AAC_KBPS_PER_CHANNEL * channels}k",
                           f"-filter:{spec}", f"aformat=channel_layouts={layout}"]
        elif t.action == TO_FLAC:
            codec_args += [f"-c:{spec}", "flac"]
        elif t.action == TO_MOVTEXT:
            codec_args += [f"-c:{spec}", "mov_text"]
        out_index += 1
    # Tags and genre are written afterwards by Runemark; don't carry stale copies across.
    args += codec_args + ["-metadata", "keywords=", "-metadata", "genre=",
                          "-f", "mp4", "-progress", "pipe:1", "-nostats", str(target)]
    return args


def sidecar_command(plan: ConversionPlan, stream: Stream, target: Path) -> list[str]:
    fmt = {".sup": "sup", ".mks": "matroska", ".srt": "srt", ".ass": "ass", ".ssa": "ass", ".vtt": "webvtt"}
    return [proc.ffmpeg(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", str(plan.source),
            "-map", f"0:{stream.index}", "-c", "copy", "-f", fmt.get(target.suffix, "matroska"), str(target)]


class Cancelled(RuntimeError):
    pass


def run_conversion(plan: ConversionPlan, progress: Callable[[int, str], None] | None = None,
                   cancel: threading.Event | None = None) -> tuple[Path, list[Path]]:
    """Remux to a temporary MP4 beside the source and write sidecar subtitles.

    Returns (temporary MP4, sidecar files). The caller verifies, tags and renames.
    """
    if not plan.possible:
        raise RuntimeError(plan.blockers[0])
    target = temp_output(plan)
    duration = plan.info.duration or 0
    created: list[Path] = []
    chapter_file: Path | None = None
    try:
        if plan.chapters:
            handle, name = tempfile.mkstemp(prefix="runemark-chapters-", suffix=".txt")
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
                # FFmpeg moves an MKV's timeline to start at zero; move the chapters with it
                out.write(ffmetadata_chapters(plan.chapters, max(0.0, -plan.info.start_time)))
            chapter_file = Path(name)
        process = proc.popen(build_command(plan, target, chapter_file), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                             text=True, encoding="utf-8", errors="replace")
        err_lines: list[str] = []

        def drain() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                err_lines.append(line.rstrip())

        drainer = threading.Thread(target=drain, daemon=True)
        drainer.start()
        assert process.stdout is not None
        for line in process.stdout:
            if cancel is not None and cancel.is_set():
                process.kill()
                process.wait()
                raise Cancelled("Conversion cancelled.")
            match = re.match(r"out_time_(?:us|ms)=(\d+)", line.strip())
            if match and duration and progress:
                seconds = int(match.group(1)) / 1_000_000
                progress(min(99, int(seconds * 100 / duration)), "Converting to MP4…")
        code = process.wait()
        drainer.join(timeout=5)
        if cancel is not None and cancel.is_set():
            raise Cancelled("Conversion cancelled.")
        if code != 0:
            raise proc.ToolFailed(["ffmpeg"], code, "\n".join(err_lines))
        for index, path in sidecar_paths(plan).items():
            stream = next(s for s in plan.info.streams if s.index == index)
            if progress:
                progress(99, f"Saving {path.name}…")
            proc.run(sidecar_command(plan, stream, path), timeout=600)
            created.append(path)
        if progress:
            progress(100, "Converted.")
        return target, created
    except BaseException:
        for leftover in [target, *created]:
            try:
                if leftover.exists():
                    leftover.unlink()
            except OSError:
                log.warning("couldn't remove %s", leftover)
        raise
    finally:
        if chapter_file is not None:
            try:
                chapter_file.unlink()
            except OSError:
                log.warning("couldn't remove %s", chapter_file)


def verify_conversion(plan: ConversionPlan, converted: Path) -> list[str]:
    """Compare the new MP4 against the plan: length, track counts and video codecs."""
    problems = []
    try:
        out = probe(converted)
    except Exception as exc:  # noqa: BLE001
        return [f"The new MP4 couldn't be read: {exc}"]
    src_dur, out_dur = plan.info.duration or 0, out.duration or 0
    if src_dur and abs(src_dur - out_dur) > max(1.0, src_dur * 0.005):
        problems.append(f"Length changed from {src_dur:.1f}s to {out_dur:.1f}s.")
    expected = plan.expected_counts()
    actual = {
        "video": len(out.video_streams),
        "audio": len(out.audio_streams),
        "subtitle": len(out.subtitle_streams),
    }
    for kind in ("video", "audio", "subtitle"):
        if expected.get(kind, 0) != actual[kind]:
            problems.append(f"Expected {expected.get(kind, 0)} {kind} track(s), found {actual[kind]}.")
    want = [t.stream.codec for t in plan.kept() if t.kind == "video"]
    got = [s.codec for s in out.video_streams]
    if want != got:
        problems.append(f"Video codec changed ({', '.join(want)} → {', '.join(got)}).")
    return problems


def cleanup(paths: list[Path]) -> None:
    for path in paths:
        try:
            if path.exists():
                os.remove(path)
        except OSError:
            log.warning("couldn't remove %s", path)
