import io
import subprocess

import pytest
from PIL import Image, ImageChops

from runemark.media import convert as C
from runemark.media import frames
from runemark.media.probe import describe, frame_rate_mode, packet_times, probe, sections_as_text
from runemark.tagsync import read_state


def nth_frame(path, n, stream_index):
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", f"0:{stream_index}",
                          "-vf", f"select=eq(n\\,{n}),format=rgb24", "-frames:v", "1",
                          "-f", "image2pipe", "-c:v", "png", "-"], capture_output=True, check=True).stdout
    return Image.open(io.BytesIO(out)).convert("RGB")


def same(a, b):
    return ImageChops.difference(a, b).getbbox() is None


def test_describe_basic(media):
    info = probe(media["mp4"])
    text = sections_as_text(describe(info, "Constant"))
    for expected in ("Resolution: 640 × 360", "Frame rate: 25 fps", "Frame rate mode: Constant",
                     "Codec: H.264 / AVC", "Container: MP4", "Length: 0:00:06"):
        assert expected in text


def test_describe_tracks_chapters_and_cover(media):
    info = probe(media["mkv"])
    text = sections_as_text(describe(info))
    assert "Dolby Digital (AC-3)" in text and "French" in text
    assert "Subtitles" in text and "ASS/SSA" in text
    assert "Chapters" in text and "Opening" in text
    assert info.cover_sources and info.cover_sources[0].filename == "cover.jpg"


def test_special_video_properties(media):
    assert probe(media["anamorphic"]).main_video.display_size == (853, 480)
    assert probe(media["rotated"]).main_video.display_size == (360, 640)
    hdr = probe(media["hdr"]).main_video
    assert hdr.hdr_format == "HDR10 (PQ)" and hdr.bit_depth == 10
    assert frame_rate_mode(media["vfr"], probe(media["vfr"]).main_video) == "Variable"
    assert frame_rate_mode(media["mp4"], probe(media["mp4"]).main_video) == "Constant"


@pytest.mark.parametrize("name,positions", [("mp4", [0.0, 1.03, 2.519, 5.95]), ("mkv", [0.5, 1.03, 2.0]),
                                            ("vfr", [0.5, 1.05, 1.31, 1.95])])
def test_grab_shown_frame_is_exact(media, name, positions):
    path = media[name]
    info = probe(path)
    stream = info.main_video
    times = packet_times(path, stream.index, 0, 999)
    for position in positions:
        image, pts = frames.grab_shown_frame(path, info, position)
        n = max(i for i, t in enumerate(times) if t <= position + 1e-6)
        assert abs(pts - times[n]) < 1e-6
        assert same(image, nth_frame(path, n, stream.index))
        if n + 1 < len(times):
            assert not same(image, nth_frame(path, n + 1, stream.index))


def test_grab_at_pts_matches_each_frame(media):
    path = media["vfr"]
    info = probe(path)
    times = packet_times(path, info.main_video.index, 0, 999)
    for n in (0, 10, 25, 30, len(times) - 1):
        assert same(frames.grab_at_pts(path, info, times[n]), nth_frame(path, n, info.main_video.index))


def test_neighbour_times(media):
    info = probe(media["vfr"])
    before, after = frames.neighbour_times(media["vfr"], info.main_video, 1.0)
    assert before == pytest.approx(0.96)
    assert after == pytest.approx(1.1)


def test_frames_respect_aspect_and_rotation(media):
    for name, size in (("anamorphic", (852, 480)), ("rotated", (360, 640))):
        info = probe(media[name])
        image, _ = frames.grab_shown_frame(media[name], info, 1.0)
        assert image.size == size


def test_jpeg_encoding_limits_size(media):
    image = frames.grab_frame(media["mp4"], 1.0)
    jpeg = frames.to_jpeg(image, max_edge=320, quality=85)
    decoded = Image.open(io.BytesIO(jpeg))
    assert decoded.format == "JPEG" and max(decoded.size) == 320
    png = frames.image_file_to_jpeg(media["cover_png"], max_edge=1280)
    assert png[:2] == b"\xff\xd8"


def test_mkv_state_reads_matroska_tags_and_cover(media):
    info = probe(media["mkv"])
    state = read_state(media["mkv"], info, lambda i: frames.dump_attachment(i.path, i.cover_sources[0]))
    assert state.keywords == ["Old Tag", "Second"]
    assert state.genre == ["Documentary"]
    assert state.cover == media["cover_jpg"].read_bytes()
    assert state.explorer is None


def test_plan_full_mkv(media):
    plan = C.plan_conversion(probe(media["mkv"]))
    actions = {t.stream.index: t.action for t in plan.tracks}
    assert plan.possible
    assert actions == {0: C.COPY, 1: C.COPY, 2: C.COPY, 3: C.TO_MOVTEXT, 4: C.TO_MOVTEXT, 5: C.DROP, 6: C.DROP}
    assert plan.cover_stream is not None
    assert plan.expected_counts() == {"video": 1, "audio": 2, "subtitle": 2}


def test_plan_blocks_vp8(media):
    plan = C.plan_conversion(probe(media["mkv_vp8"]))
    assert not plan.possible and "VP8" in plan.blockers[0]


def test_convert_and_verify_with_sidecar(copy_of):
    source = copy_of("mkv")
    plan = C.plan_conversion(probe(source))
    for t in plan.tracks:
        if t.stream.codec == "ass":
            t.action = C.SIDECAR
    tmp, sidecars = C.run_conversion(plan)
    try:
        assert C.verify_conversion(plan, tmp) == []
        out = probe(tmp)
        assert [s.codec for s in out.audio_streams] == ["aac", "ac3"]
        assert [s.codec for s in out.subtitle_streams] == ["mov_text"]
        # the test MKV is 3 s long but its "Ending" chapter runs from 3 s to 6 s: left out
        assert [c.title for c in out.chapters] == ["Opening"]
        assert movie_seconds(tmp) < 3.2
        assert out.tag("title") == "Chaptered Test"
        assert out.tag("keywords") == "" and out.tag("genre") == ""   # stale copies not carried over
        assert [p.name for p in sidecars] == ["full.eng.ass"]
        assert "Styled" in sidecars[0].read_text(encoding="utf-8")
    finally:
        C.cleanup([tmp, *sidecars])


def test_convert_audio_that_mp4_cannot_hold(copy_of):
    source = copy_of("mkv_convert")
    plan = C.plan_conversion(probe(source))
    assert [t.action for t in plan.tracks] == [C.COPY, C.TO_AAC, C.TO_FLAC]
    tmp, _ = C.run_conversion(plan)
    try:
        assert C.verify_conversion(plan, tmp) == []
        out = probe(tmp)
        assert [(s.codec, s.channels) for s in out.audio_streams] == [("aac", 6), ("flac", 1)]
    finally:
        C.cleanup([tmp])


def test_hevc_gets_hvc1_tag(copy_of):
    source = copy_of("hdr")
    plan = C.plan_conversion(probe(source))
    tmp, _ = C.run_conversion(plan)
    try:
        video = probe(tmp).main_video
        assert video.raw.get("codec_tag_string") == "hvc1"
        assert video.hdr_format == "HDR10 (PQ)"
    finally:
        C.cleanup([tmp])


def test_conversion_cancel_cleans_up(copy_of):
    import threading
    source = copy_of("mkv_convert")
    plan = C.plan_conversion(probe(source))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(C.Cancelled):
        C.run_conversion(plan, cancel=cancel)
    assert not C.temp_output(plan).exists()


def test_verify_catches_wrong_output(copy_of, media):
    source = copy_of("mkv")
    plan = C.plan_conversion(probe(source))
    problems = C.verify_conversion(plan, media["mp4"])  # a different, longer file
    assert problems


def movie_seconds(path):
    """The MP4's own duration (mvhd), which is what Windows shows as Length."""
    import struct
    data = open(path, "rb").read()
    at = data.index(b"mvhd")
    if data[at + 4] == 0:
        scale, duration = struct.unpack(">II", data[at + 16:at + 24])
    else:
        scale, duration = struct.unpack(">IQ", data[at + 24:at + 36])
    return duration / scale


def _mkv_with_chapters(folder, name, chapters):
    from conftest import ff
    meta = folder / f"{name}.txt"
    meta.write_text(";FFMETADATA1\n" + "".join(
        f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={start}\nEND={end}\ntitle={title}\n" for start, end, title in chapters),
        encoding="utf-8")
    source = folder / f"{name}.mkv"
    ff("-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=2", "-f", "lavfi", "-i", "sine=d=2",
       "-i", str(meta), "-map", "0", "-map", "1", "-map_chapters", "2",
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(source))
    return source


def test_chapters_inside_the_video_are_copied_as_they_are(tmp_path):
    source = _mkv_with_chapters(tmp_path, "fine", [(0, 1000, "One"), (1000, 2000, "Two")])
    plan = C.plan_conversion(probe(source))
    assert plan.chapters is None and plan.chapter_note == ""
    assert "-map_chapters" in (args := C.build_command(plan, tmp_path / "x.mp4")) and \
        args[args.index("-map_chapters") + 1] == "0"


def test_chapters_past_the_end_are_trimmed(tmp_path):
    source = _mkv_with_chapters(tmp_path, "long", [(0, 1000, "One"), (1000, 4000, "Two \\= 2"),
                                                   (4000, 6000, "Three")])
    plan = C.plan_conversion(probe(source))
    assert [c.title for c in plan.chapters] == ["One", "Two = 2"]
    assert plan.chapters[-1].end < 2.2
    assert "1 chapter after the end of the video is left out" in plan.chapter_note
    tmp, _ = C.run_conversion(plan)
    try:
        assert C.verify_conversion(plan, tmp) == []
        out = probe(tmp)
        assert [c.title for c in out.chapters] == ["One", "Two = 2"]
        assert movie_seconds(tmp) < 2.2
    finally:
        C.cleanup([tmp])


def test_ffmetadata_escapes_special_characters():
    from runemark.media.probe import Chapter
    text = C.ffmetadata_chapters([Chapter(0.0, 1.5, "A=B; C#D\\E")])
    assert "title=A\\=B\\; C\\#D\\\\E" in text and "START=0" in text and "END=1500" in text


@pytest.mark.parametrize("name", ["mp4", "mkv", "vfr"])
def test_filmstrip_frames_are_real_frames(media, name):
    path = media[name]
    info = probe(path)
    times = packet_times(path, info.main_video.index, 0, 999)
    picks = frames.filmstrip_frames(info, 6)
    assert len(picks) == 6
    if name != "vfr":   # constant frame rate: every pick is exactly a frame's timestamp
        for t in picks:
            assert min(abs(t - p) for p in times) < 1e-6
        n = times.index(min(times, key=lambda p: abs(p - picks[2])))
        assert same(frames.grab_at_pts(path, info, picks[2]), nth_frame(path, n, info.main_video.index))
