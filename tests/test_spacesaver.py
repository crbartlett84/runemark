"""0.8.0: the space saver re-encodes to HEVC and keeps everything Runemark knows about the video."""
from __future__ import annotations

import pytest
from test_duplicates import ff
from test_upgrade import SCENE_LINK, with_chapters

from runemark import spacesaver as S
from runemark.media.mp4meta import VideoMeta, read_mp4_tags
from runemark.media.probe import probe
from runemark.tagdb import TagDB
from runemark.tagsync import read_state, save_mp4


@pytest.fixture
def big_h264(tmp_path):
    src = tmp_path / "src.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=duration=12:size=1280x720:rate=25", "-f", "lavfi", "-i",
       "sine=frequency=440:duration=12", "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "12M", "-pix_fmt",
       "yuv420p", "-c:a", "aac", "-shortest", str(src))
    video = with_chapters(src, tmp_path / "Mine.mp4", [(0, "Intro"), (6, "Kissing")], "1280:720")
    # with_chapters re-encodes small; make it big again so there's something to save
    big = tmp_path / "Big.mp4"
    ff("-i", str(video), "-map", "0", "-map_chapters", "0", "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "12M",
       "-c:a", "copy", str(big))
    save_mp4(big, keywords=["Beach"], genre=[], meta=VideoMeta("My video", 2021, "Notes", 4, "Studio", [SCENE_LINK],
                                                               "2021-02-03"))
    return big


def test_assess_says_what_would_be_saved(big_h264, monkeypatch):
    c = S.assess(big_h264)
    assert c.codec == "h264" and c.height == 720 and c.estimate < c.size
    monkeypatch.setattr(S, "MIN_SAVING_BYTES", 0)
    assert S.assess(big_h264).worth_it
    monkeypatch.setattr(S, "MIN_SAVING_BYTES", 10 ** 12)
    assert "small" in S.assess(big_h264).reason


def test_shrink_puts_an_hevc_copy_in_place_with_everything(big_h264, tmp_path, monkeypatch):
    monkeypatch.setattr(S, "MIN_SAVING_BYTES", 0)
    db = TagDB(tmp_path / "t.db")
    try:
        c = S.assess(big_h264)
        result = S.shrink(db, c, old_to=tmp_path / "old")
        assert not result.problems and result.final == big_h264
        info = probe(big_h264)
        assert info.main_video.codec == "hevc" and big_h264.stat().st_size < c.size
        assert [ch.title for ch in info.chapters] == ["Intro", "Kissing"]
        tags = read_mp4_tags(big_h264)
        assert tags.keywords == ["Beach"] and tags.title == "My video" and tags.date == "2021-02-03"
        assert read_state(big_h264).meta.description == "Notes"
        assert db.history(big_h264)[0].label == "Re-encoded to save space"
        assert (tmp_path / "old" / "Big.mp4").exists()
        assert not list(tmp_path.glob(".*runemark-hevc*"))
    finally:
        db.close()


def test_hevc_is_left_alone(tmp_path):
    v = tmp_path / "h.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=duration=2:size=320x240:rate=25", "-c:v", "libx265", "-preset", "ultrafast",
       "-tag:v", "hvc1", str(v))
    assert S.assess(v).reason == "already HEVC"


def test_dialog_lists_what_is_worth_it_and_totals(big_h264, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt
    app = QApplication.instance() or QApplication([])
    from runemark.ui.spacesaver_dialog import SpaceSaverDialog
    monkeypatch.setattr(S, "MIN_SAVING_BYTES", 0)
    db = TagDB(tmp_path / "t.db")
    try:
        d = SpaceSaverDialog(db, [])
        d.encoder = "the processor (slow, best quality)"
        small = S.Candidate(tmp_path / "x.mp4", "hevc", 720, 10.0, 1000, 1000, 1000, reason="already HEVC")
        d.candidates = [small, S.assess(big_h264)]
        d._fill()
        assert d.table.item(0, 0).text() == "Big.mp4" and d.table.item(0, 0).checkState() == Qt.CheckState.Checked
        assert d.table.item(1, 6).text() == "already HEVC"
        assert len(d._chosen()) == 1 and "1 of 1 worth" in d.summary.text() and d.go.text() == "Re-encode 1"
        d.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        assert not d.go.isEnabled()
        app.processEvents()
    finally:
        db.close()
