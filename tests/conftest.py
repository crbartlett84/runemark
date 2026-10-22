"""Test fixtures: small generated videos covering the cases Runemark has to handle."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"


def ff(*args: str) -> None:
    subprocess.run([FFMPEG, "-v", "error", "-y", *args], check=True)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


SRT = """1
00:00:00,500 --> 00:00:02,000
Hello there

2
00:00:02,500 --> 00:00:04,000
Second line
"""

ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 640
PlayResY: 360

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,28,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\b1}Styled{\\b0} subtitle
"""

CHAPTERS = """;FFMETADATA1
title=Chaptered Test
[CHAPTER]
TIMEBASE=1/1000
START=0
END=3000
title=Opening
[CHAPTER]
TIMEBASE=1/1000
START=3000
END=6000
title=Ending
"""


def build_media(folder: Path) -> dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    m: dict[str, Path] = {}
    video = ["-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=6"]
    audio = ["-f", "lavfi", "-i", "sine=f=440:d=6:sample_rate=48000"]

    m["mp4"] = folder / "basic.mp4"
    ff(*video, *audio, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-shortest", str(m["mp4"]))

    m["mp4_faststart"] = folder / "faststart.mp4"
    ff("-i", str(m["mp4"]), "-c", "copy", "-movflags", "+faststart", str(m["mp4_faststart"]))

    cover_png = folder / "cover.png"
    ff("-f", "lavfi", "-i", "color=c=0x2266aa:s=320x180:d=1", "-frames:v", "1", str(cover_png))
    m["cover_png"] = cover_png
    cover_jpg = folder / "cover.jpg"
    ff("-f", "lavfi", "-i", "color=c=0xaa2266:s=320x180:d=1", "-frames:v", "1", str(cover_jpg))
    m["cover_jpg"] = cover_jpg

    m["mp4_cover"] = folder / "with_cover.mp4"
    ff("-i", str(m["mp4"]), "-i", str(cover_jpg), "-map", "0", "-map", "1", "-c", "copy",
       "-disposition:v:1", "attached_pic", str(m["mp4_cover"]))

    srt = _write(folder / "subs.srt", SRT)
    ass = _write(folder / "subs.ass", ASS)
    chapters = _write(folder / "chapters.txt", CHAPTERS)
    font = folder / "font.ttf"
    font.write_bytes(b"\x00\x01\x00\x00" + b"\x00" * 60)

    # MKV that converts cleanly: H.264, AAC (eng), AC-3 5.1 (fre), SRT, ASS, chapters,
    # global tags (title, keywords, genre) and attachments (cover image and a font).
    m["mkv"] = folder / "full.mkv"
    ff(*video, *audio,
       "-f", "lavfi", "-i", "sine=f=220:d=6:sample_rate=48000",
       "-i", str(srt), "-i", str(ass), "-i", str(chapters),
       "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s", "-map", "4:s",
       "-map_metadata", "5", "-map_chapters", "5",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
       "-c:a:0", "aac", "-c:a:1", "ac3", "-ac:a:1", "6",
       "-c:s:0", "srt", "-c:s:1", "ass",
       "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=fre",
       "-metadata:s:s:0", "language=eng", "-metadata:s:s:1", "language=eng",
       "-metadata", "KEYWORDS=Old Tag; Second", "-metadata", "GENRE=Documentary",
       "-attach", str(cover_jpg), "-metadata:s:t:0", "mimetype=image/jpeg",
       "-metadata:s:t:0", "filename=cover.jpg",
       "-attach", str(font), "-metadata:s:t:1", "mimetype=application/x-truetype-font",
       "-metadata:s:t:1", "filename=font.ttf",
       "-shortest", str(m["mkv"]))

    # MKV whose audio has to be converted: Vorbis 5.1 (side) and 24-bit PCM.
    m["mkv_convert"] = folder / "needs_convert.mkv"
    ff(*video, *audio, "-f", "lavfi", "-i", "sine=f=330:d=6:sample_rate=48000",
       "-map", "0:v", "-map", "1:a", "-map", "2:a",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
       "-c:a:0", "libvorbis", "-ac:a:0", "6", "-c:a:1", "pcm_s24le",
       "-shortest", str(m["mkv_convert"]))

    # MKV with VP8 video, which MP4 can't hold without re-encoding.
    m["mkv_vp8"] = folder / "vp8.mkv"
    ff("-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=2", "-c:v", "libvpx", "-b:v", "300k", str(m["mkv_vp8"]))

    # Anamorphic DVD-style video (720x480 shown as 16:9)
    m["anamorphic"] = folder / "anamorphic.mkv"
    ff("-f", "lavfi", "-i", "testsrc2=s=720x480:r=25:d=2", "-vf", "setsar=32/27",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(m["anamorphic"]))

    # HDR10-flagged 10-bit HEVC
    m["hdr"] = folder / "hdr.mkv"
    ff("-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=2",
       "-c:v", "libx265", "-x265-params", "log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
       "-pix_fmt", "yuv420p10le",
       "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc", str(m["hdr"]))

    # Variable frame rate: 1 s at 25 fps followed by 1 s at 10 fps
    part_a, part_b = folder / "vfr_a.mkv", folder / "vfr_b.mkv"
    ff("-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=1", "-c:v", "libx264", "-preset", "ultrafast", str(part_a))
    ff("-f", "lavfi", "-i", "testsrc2=s=320x240:r=10:d=1", "-c:v", "libx264", "-preset", "ultrafast", str(part_b))
    concat = _write(folder / "concat.txt", f"file '{part_a.name}'\nfile '{part_b.name}'\n")
    m["vfr"] = folder / "vfr.mkv"
    ff("-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(m["vfr"]))

    # Phone-style rotated video
    m["rotated"] = folder / "rotated.mp4"
    ff("-display_rotation", "90", "-i", str(m["mp4"]), "-c", "copy", str(m["rotated"]))
    return m


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> dict[str, Path]:
    cache = os.environ.get("RUNEMARK_TEST_MEDIA")
    if cache and Path(cache, "rotated.mp4").exists():
        folder = Path(cache)
        return {
            "mp4": folder / "basic.mp4", "mp4_faststart": folder / "faststart.mp4",
            "cover_png": folder / "cover.png", "cover_jpg": folder / "cover.jpg",
            "mp4_cover": folder / "with_cover.mp4", "mkv": folder / "full.mkv",
            "mkv_convert": folder / "needs_convert.mkv", "mkv_vp8": folder / "vp8.mkv",
            "anamorphic": folder / "anamorphic.mkv", "hdr": folder / "hdr.mkv",
            "vfr": folder / "vfr.mkv", "rotated": folder / "rotated.mp4",
        }
    folder = Path(cache) if cache else tmp_path_factory.mktemp("media")
    return build_media(folder)


@pytest.fixture
def copy_of(media, tmp_path):
    def make(name: str) -> Path:
        src = media[name]
        dst = tmp_path / src.name
        shutil.copy2(src, dst)
        return dst
    return make


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNEMARK_HOME", str(tmp_path / "home"))
