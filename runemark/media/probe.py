"""Read what's inside a video file with ffprobe, and describe it for people."""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from .. import proc

log = logging.getLogger(__name__)

MP4_EXTENSIONS = {".mp4", ".m4v"}
MKV_EXTENSIONS = {".mkv"}
VIDEO_EXTENSIONS = MP4_EXTENSIONS | MKV_EXTENSIONS


def _fraction(text: Any) -> Fraction | None:
    if not text or not isinstance(text, str):
        return None
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            if int(den) == 0:
                return None
            value = Fraction(int(num), int(den))
        elif ":" in text:
            num, den = text.split(":", 1)
            if int(den) == 0:
                return None
            value = Fraction(int(num), int(den))
        else:
            value = Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # drop NaN


def _lower_keys(tags: dict | None) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (tags or {}).items()}


@dataclass
class Stream:
    index: int
    kind: str  # video, audio, subtitle, attachment, data
    codec: str
    codec_long: str
    profile: str | None
    tags: dict[str, str]
    disposition: dict[str, int]
    raw: dict[str, Any]

    # --- common -------------------------------------------------------------
    @property
    def language(self) -> str:
        lang = self.tags.get("language", "")
        return "" if lang in ("und", "") else lang

    @property
    def title(self) -> str:
        return self.tags.get("title", "")

    @property
    def is_default(self) -> bool:
        return bool(self.disposition.get("default"))

    @property
    def is_forced(self) -> bool:
        return bool(self.disposition.get("forced"))

    @property
    def is_attached_pic(self) -> bool:
        return self.kind == "video" and bool(self.disposition.get("attached_pic"))

    @property
    def bit_rate(self) -> int | None:
        value = _int(self.raw.get("bit_rate"))
        if value:
            return value
        # Matroska statistics tags written by mkvmerge
        for key in ("bps", "bps-eng"):
            value = _int(self.tags.get(key))
            if value:
                return value
        return None

    @property
    def duration(self) -> float | None:
        return _float(self.raw.get("duration"))

    # --- video --------------------------------------------------------------
    @property
    def width(self) -> int | None:
        return _int(self.raw.get("width"))

    @property
    def height(self) -> int | None:
        return _int(self.raw.get("height"))

    @property
    def sar(self) -> Fraction | None:
        return _fraction(self.raw.get("sample_aspect_ratio"))

    @property
    def dar(self) -> Fraction | None:
        return _fraction(self.raw.get("display_aspect_ratio"))

    @property
    def rotation(self) -> int:
        for item in self.raw.get("side_data_list", []) or []:
            if "rotation" in item:
                try:
                    return int(round(float(item["rotation"])))
                except (TypeError, ValueError):
                    pass
        rotate = _int(self.tags.get("rotate"))
        return -rotate if rotate else 0

    @property
    def display_size(self) -> tuple[int, int] | None:
        """Width x height as shown on screen: aspect ratio and rotation applied."""
        if not self.width or not self.height:
            return None
        width, height = self.width, self.height
        sar = self.sar
        if sar and sar != 1:
            width = int(round(width * sar))
        if abs(self.rotation) % 180 == 90:
            width, height = height, width
        return width, height

    @property
    def avg_fps(self) -> Fraction | None:
        return _fraction(self.raw.get("avg_frame_rate"))

    @property
    def r_fps(self) -> Fraction | None:
        return _fraction(self.raw.get("r_frame_rate"))

    @property
    def fps(self) -> Fraction | None:
        avg, real = self.avg_fps, self.r_fps
        if avg and avg < 1000:
            return avg
        if real and real < 1000:
            return real
        return None

    @property
    def pix_fmt(self) -> str:
        return self.raw.get("pix_fmt", "") or ""

    @property
    def bit_depth(self) -> int | None:
        depth = _int(self.raw.get("bits_per_raw_sample"))
        if depth:
            return depth
        match = re.search(r"p(\d{2})(le|be)$", self.pix_fmt)
        if match:
            return int(match.group(1))
        if self.pix_fmt.startswith(("yuv", "yuvj", "nv12", "rgb", "bgr", "gbr")):
            return 8
        return None

    @property
    def chroma(self) -> str | None:
        fmt = self.pix_fmt
        if fmt.startswith(("yuv420", "yuvj420", "nv12", "p010", "nv21")):
            return "4:2:0"
        if fmt.startswith(("yuv422", "yuvj422")):
            return "4:2:2"
        if fmt.startswith(("yuv444", "yuvj444", "gbr", "rgb", "bgr")):
            return "4:4:4"
        return None

    @property
    def color_transfer(self) -> str:
        return self.raw.get("color_transfer", "") or ""

    @property
    def hdr_format(self) -> str | None:
        dovi = None
        for item in self.raw.get("side_data_list", []) or []:
            if "DOVI" in str(item.get("side_data_type", "")):
                profile = item.get("dv_profile")
                dovi = f"Dolby Vision (profile {profile})" if profile is not None else "Dolby Vision"
        transfer = self.color_transfer
        base = None
        if transfer == "smpte2084":
            base = "HDR10 (PQ)"
        elif transfer == "arib-std-b67":
            base = "HLG"
        if dovi and base:
            return f"{dovi}, {base} base layer"
        return dovi or base

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer in ("smpte2084", "arib-std-b67")

    @property
    def scan_type(self) -> str | None:
        order = self.raw.get("field_order")
        if not order or order == "unknown":
            return None
        return "Progressive" if order == "progressive" else "Interlaced"

    # --- audio --------------------------------------------------------------
    @property
    def channels(self) -> int | None:
        return _int(self.raw.get("channels"))

    @property
    def channel_layout(self) -> str:
        return self.raw.get("channel_layout", "") or ""

    @property
    def sample_rate(self) -> int | None:
        return _int(self.raw.get("sample_rate"))

    # --- attachments --------------------------------------------------------
    @property
    def mimetype(self) -> str:
        return self.tags.get("mimetype", "")

    @property
    def filename(self) -> str:
        return self.tags.get("filename", "")

    @property
    def is_image_attachment(self) -> bool:
        return self.kind == "attachment" and self.mimetype.startswith("image/")


@dataclass
class Chapter:
    start: float
    end: float
    title: str


@dataclass
class MediaInfo:
    path: Path
    format_name: str
    format_long: str
    size: int
    duration: float | None
    bit_rate: int | None
    start_time: float
    tags: dict[str, str]
    streams: list[Stream]
    chapters: list[Chapter] = field(default_factory=list)

    @property
    def container(self) -> str:
        ext = self.path.suffix.lower()
        if "matroska" in self.format_name or ext in MKV_EXTENSIONS:
            return "mkv"
        if "mp4" in self.format_name or "mov" in self.format_name or ext in MP4_EXTENSIONS:
            return "mp4"
        return "other"

    @property
    def video_streams(self) -> list[Stream]:
        return [s for s in self.streams if s.kind == "video" and not s.is_attached_pic]

    @property
    def main_video(self) -> Stream | None:
        videos = self.video_streams
        for stream in videos:
            if stream.is_default:
                return stream
        return videos[0] if videos else None

    @property
    def audio_streams(self) -> list[Stream]:
        return [s for s in self.streams if s.kind == "audio"]

    @property
    def subtitle_streams(self) -> list[Stream]:
        return [s for s in self.streams if s.kind == "subtitle"]

    @property
    def attachments(self) -> list[Stream]:
        return [s for s in self.streams if s.kind == "attachment"]

    @property
    def cover_sources(self) -> list[Stream]:
        """Embedded images that act as a cover: MP4 attached pictures or MKV image attachments."""
        pics = [s for s in self.streams if s.is_attached_pic]
        images = [s for s in self.attachments if s.is_image_attachment]

        def rank(stream: Stream) -> int:
            name = stream.filename.lower()
            return 0 if name.startswith("cover") and "small" not in name else 1

        return pics + sorted(images, key=rank)

    def tag(self, name: str) -> str:
        return self.tags.get(name.lower(), "")


def probe(path: str | os.PathLike) -> MediaInfo:
    """Run ffprobe and parse the result."""
    path = Path(path)
    result = proc.run([
        proc.ffprobe(), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", "-show_chapters", str(path),
    ], timeout=60)
    data = json.loads(result.stdout.decode("utf-8", "replace") or "{}")
    return parse_probe(path, data)


def parse_probe(path: Path, data: dict) -> MediaInfo:
    fmt = data.get("format", {}) or {}
    streams = []
    for raw in data.get("streams", []) or []:
        streams.append(Stream(
            index=int(raw.get("index", len(streams))),
            kind=raw.get("codec_type", "data") or "data",
            codec=raw.get("codec_name", "") or "",
            codec_long=raw.get("codec_long_name", "") or "",
            profile=raw.get("profile"),
            tags=_lower_keys(raw.get("tags")),
            disposition={k: int(v) for k, v in (raw.get("disposition") or {}).items()
                         if isinstance(v, (int, bool))},
            raw=raw,
        ))
    chapters = []
    for chap in data.get("chapters", []) or []:
        chapters.append(Chapter(
            start=_float(chap.get("start_time")) or 0.0,
            end=_float(chap.get("end_time")) or 0.0,
            title=_lower_keys(chap.get("tags")).get("title", ""),
        ))
    size = _int(fmt.get("size"))
    if size is None:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
    return MediaInfo(
        path=path,
        format_name=fmt.get("format_name", "") or "",
        format_long=fmt.get("format_long_name", "") or "",
        size=size,
        duration=_float(fmt.get("duration")),
        bit_rate=_int(fmt.get("bit_rate")),
        start_time=_float(fmt.get("start_time")) or 0.0,
        tags=_lower_keys(fmt.get("tags")),
        streams=streams,
        chapters=chapters,
    )


# --------------------------------------------------------------------------
# Frame timing
# --------------------------------------------------------------------------

def packet_times(path: str | os.PathLike, stream_index: int, start: float, end: float) -> list[float]:
    """Presentation times (seconds) of the video packets between start and end, sorted.

    Packets are read without decoding, so this is quick even for 4K files.
    """
    start = max(0.0, start)
    result = proc.run([
        proc.ffprobe(), "-v", "error", "-select_streams", str(stream_index),
        "-read_intervals", f"{start:.3f}%{end:.3f}",
        "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path),
    ], timeout=60)
    times = []
    for line in result.stdout.decode("ascii", "replace").splitlines():
        value = _float(line.strip().rstrip(","))
        if value is not None:
            times.append(value)
    return sorted(set(times))


def frame_rate_mode(path: str | os.PathLike, stream: Stream, sample_seconds: float = 20.0) -> str | None:
    """'Constant' or 'Variable', judged from packet timing in the first stretch of the video."""
    try:
        times = packet_times(path, stream.index, 0.0, sample_seconds)
    except Exception as exc:  # noqa: BLE001 - informational only
        log.info("frame-rate scan failed: %s", exc)
        return None
    if len(times) < 10:
        return None
    deltas = [b - a for a, b in zip(times, times[1:]) if b > a]
    if not deltas:
        return None
    deltas.sort()
    typical = deltas[len(deltas) // 2]
    if typical <= 0:
        return None
    off = [d for d in deltas if abs(d - typical) > max(0.002, typical * 0.05)]
    return "Variable" if len(off) > max(2, len(deltas) * 0.02) else "Constant"


# --------------------------------------------------------------------------
# Human-readable description
# --------------------------------------------------------------------------

def fmt_duration(seconds: float | None, millis: bool = False) -> str:
    if seconds is None:
        return "Unknown"
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int(seconds % 3600 // 60)
    secs = seconds % 60
    if millis:
        return f"{hours:d}:{minutes:02d}:{secs:06.3f}"
    return f"{hours:d}:{minutes:02d}:{int(secs):02d}"


def fmt_size(num: int | None) -> str:
    if not num:
        return "Unknown"
    value = float(num)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:,.0f} {unit}" if unit == "bytes" else f"{value:,.2f} {unit}"
        value /= 1024
    return f"{num} bytes"


def fmt_bitrate(bps: int | None) -> str:
    if not bps:
        return "Unknown"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.2f} Mb/s"
    return f"{bps / 1000:.0f} kb/s"


def fmt_fps(fps: Fraction | None) -> str:
    if not fps:
        return "Unknown"
    value = float(fps)
    known = {Fraction(24000, 1001): "23.976", Fraction(30000, 1001): "29.97",
             Fraction(60000, 1001): "59.94", Fraction(120000, 1001): "119.88"}
    text = known.get(fps)
    if text is None:
        text = f"{value:.3f}".rstrip("0").rstrip(".")
    return f"{text} fps"


def fmt_ratio(width: int, height: int) -> str:
    if not width or not height:
        return ""
    common = {(16, 9): "16:9", (4, 3): "4:3", (21, 9): "21:9", (1, 1): "1:1", (9, 16): "9:16"}
    ratio = Fraction(width, height)
    for (w, h), label in common.items():
        if abs(float(ratio) - w / h) < 0.01:
            return label
    return f"{float(ratio):.2f}:1"


CHANNEL_NAMES = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}

LANGUAGES = {
    "eng": "English", "en": "English", "fre": "French", "fra": "French", "fr": "French",
    "spa": "Spanish", "es": "Spanish", "ger": "German", "deu": "German", "de": "German",
    "ita": "Italian", "it": "Italian", "jpn": "Japanese", "ja": "Japanese", "kor": "Korean",
    "chi": "Chinese", "zho": "Chinese", "por": "Portuguese", "rus": "Russian", "hin": "Hindi",
    "ara": "Arabic", "dut": "Dutch", "nld": "Dutch", "swe": "Swedish", "nor": "Norwegian",
    "dan": "Danish", "fin": "Finnish", "pol": "Polish", "tur": "Turkish",
}


def language_name(code: str) -> str:
    if not code:
        return "Unknown language"
    return LANGUAGES.get(code.lower(), code)


def codec_label(stream: Stream) -> str:
    names = {
        "h264": "H.264 / AVC", "hevc": "H.265 / HEVC", "av1": "AV1", "vp9": "VP9", "vp8": "VP8",
        "mpeg2video": "MPEG-2", "mpeg4": "MPEG-4 Part 2", "mjpeg": "Motion JPEG", "vc1": "VC-1",
        "aac": "AAC", "mp3": "MP3", "ac3": "Dolby Digital (AC-3)", "eac3": "Dolby Digital Plus (E-AC-3)",
        "truehd": "Dolby TrueHD", "dts": "DTS", "flac": "FLAC", "opus": "Opus", "vorbis": "Vorbis",
        "alac": "Apple Lossless", "mp2": "MPEG Audio Layer II",
        "subrip": "SubRip (SRT)", "ass": "ASS/SSA", "ssa": "SSA", "webvtt": "WebVTT",
        "mov_text": "MP4 text (tx3g)", "hdmv_pgs_subtitle": "PGS (Blu-ray, image)",
        "dvd_subtitle": "VobSub (DVD, image)", "dvb_subtitle": "DVB (image)",
    }
    label = names.get(stream.codec, stream.codec.upper() if stream.codec else "Unknown")
    if stream.codec.startswith("pcm_"):
        label = "PCM (uncompressed)"
    if (stream.profile and str(stream.profile) not in ("unknown",) and not str(stream.profile).isdigit()
            and stream.kind in ("video", "audio")):
        profile = str(stream.profile)
        if stream.codec == "dts" and profile != "DTS":
            label = profile
        elif profile.lower() not in label.lower():
            label = f"{label}, {profile}"
    return label


@dataclass
class Section:
    title: str
    rows: list[tuple[str, str]]


def describe(info: MediaInfo, frame_mode: str | None = None) -> list[Section]:
    """Group the useful facts about a file into titled sections."""
    sections: list[Section] = []
    path = info.path
    try:
        stat = path.stat()
        modified = _fmt_time(stat.st_mtime)
        created = _fmt_time(getattr(stat, "st_birthtime", None) or stat.st_ctime)
    except OSError:
        modified = created = "Unknown"

    container = {"mp4": "MP4", "mkv": "Matroska (MKV)"}.get(info.container, info.format_long or info.format_name)
    general = [
        ("File", path.name),
        ("Folder", str(path.parent)),
        ("Container", container),
        ("Size", fmt_size(info.size)),
        ("Length", fmt_duration(info.duration, millis=False)),
        ("Overall bit rate", fmt_bitrate(info.bit_rate)),
        ("Created", created),
        ("Modified", modified),
    ]
    title = info.tag("title")
    if title:
        general.insert(1, ("Title", title))
    sections.append(Section("General", general))

    for number, stream in enumerate(info.video_streams, 1):
        rows = [("Codec", codec_label(stream))]
        if stream.width and stream.height:
            rows.append(("Resolution", f"{stream.width} × {stream.height}"))
            display = stream.display_size
            if display and display != (stream.width, stream.height):
                rows.append(("Displayed as", f"{display[0]} × {display[1]}"))
            if display:
                rows.append(("Aspect ratio", fmt_ratio(*display)))
        rows.append(("Frame rate", fmt_fps(stream.fps)))
        if frame_mode and stream is info.main_video:
            rows.append(("Frame rate mode", frame_mode))
        if stream.bit_rate:
            rows.append(("Bit rate", fmt_bitrate(stream.bit_rate)))
        if stream.bit_depth:
            rows.append(("Bit depth", f"{stream.bit_depth}-bit"))
        if stream.chroma:
            rows.append(("Chroma", stream.chroma))
        rows.append(("HDR", stream.hdr_format or "No (SDR)"))
        if stream.scan_type:
            rows.append(("Scan type", stream.scan_type))
        if stream.rotation:
            rows.append(("Rotation", f"{stream.rotation}°"))
        title = stream.tags.get("title")
        if title:
            rows.append(("Track name", title))
        name = "Video" if len(info.video_streams) == 1 else f"Video {number}"
        sections.append(Section(name, rows))

    audio = info.audio_streams
    for number, stream in enumerate(audio, 1):
        channels = stream.channels or 0
        channel_text = CHANNEL_NAMES.get(channels, f"{channels} channels" if channels else "Unknown")
        if stream.channel_layout and stream.channel_layout not in ("stereo", "mono"):
            channel_text = f"{channel_text} ({stream.channel_layout})" if channels not in CHANNEL_NAMES else channel_text
        rows = [
            ("Codec", codec_label(stream)),
            ("Channels", channel_text),
            ("Language", language_name(stream.language)),
        ]
        if stream.sample_rate:
            rows.append(("Sample rate", f"{stream.sample_rate / 1000:g} kHz"))
        if stream.bit_rate:
            rows.append(("Bit rate", fmt_bitrate(stream.bit_rate)))
        if stream.tags.get("title"):
            rows.append(("Track name", stream.tags["title"]))
        flags = _flags(stream)
        if flags:
            rows.append(("Flags", flags))
        sections.append(Section(f"Audio {number}" if len(audio) > 1 else "Audio", rows))

    subs = info.subtitle_streams
    if subs:
        rows = []
        for number, stream in enumerate(subs, 1):
            parts = [language_name(stream.language), codec_label(stream)]
            if stream.tags.get("title"):
                parts.append(f"“{stream.tags['title']}”")
            flags = _flags(stream)
            if flags:
                parts.append(flags.lower())
            rows.append((f"Track {number}", " · ".join(parts)))
        sections.append(Section("Subtitles", rows))

    if info.chapters:
        rows = [("Count", str(len(info.chapters)))]
        for chap in info.chapters[:12]:
            rows.append((fmt_duration(chap.start), chap.title or "(untitled)"))
        if len(info.chapters) > 12:
            rows.append(("…", f"{len(info.chapters) - 12} more"))
        sections.append(Section("Chapters", rows))

    others = []
    for key in ("encoder", "creation_time", "comment", "description", "artist", "date", "year"):
        if info.tag(key):
            others.append((key.replace("_", " ").capitalize(), info.tag(key)))
    attachments = info.attachments
    if attachments:
        names = ", ".join(a.filename or a.mimetype or "unnamed" for a in attachments[:6])
        if len(attachments) > 6:
            names += f" and {len(attachments) - 6} more"
        others.append(("Attachments", names))
    if others:
        sections.append(Section("Embedded metadata", others))
    return sections


def _flags(stream: Stream) -> str:
    flags = []
    if stream.is_default:
        flags.append("Default")
    if stream.is_forced:
        flags.append("Forced")
    if stream.disposition.get("hearing_impaired"):
        flags.append("SDH")
    if stream.disposition.get("comment"):
        flags.append("Commentary")
    return ", ".join(flags)


def _fmt_time(timestamp: float | None) -> str:
    if not timestamp:
        return "Unknown"
    import datetime as _dt
    return _dt.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def sections_as_text(sections: list[Section]) -> str:
    lines = []
    for section in sections:
        lines.append(section.title)
        for label, value in section.rows:
            lines.append(f"  {label}: {value}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
