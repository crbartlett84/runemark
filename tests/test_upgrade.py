"""0.7.0: upgrading a curated video to a better copy keeps everything and moves chapters to the right moments."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from test_duplicates import OTHER, SCENES, ff, make

from runemark import duplicates as D
from runemark import upgrade as U
from runemark.media.chapters import ChapterMark
from runemark.media.mp4meta import VideoMeta, read_mp4_tags
from runemark.media.probe import probe
from runemark.tagdb import TagDB
from runemark.tagsync import read_state, save_mp4

SCENE_LINK = "https://stashdb.org/scenes/bffb88f3-726d-4ef8-ab81-703281c19c3f"


def with_chapters(src: Path, out: Path, chapters: list[tuple[float, str]], scale: str) -> Path:
    meta = out.with_suffix(".ffmeta")
    end = probe(src).duration
    lines = [";FFMETADATA1"]
    for n, (start, name) in enumerate(chapters):
        stop = chapters[n + 1][0] if n + 1 < len(chapters) else end
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(start * 1000)}", f"END={int(stop * 1000)}",
                  f"title={name}"]
    meta.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ff("-i", str(src), "-f", "ffmetadata", "-i", str(meta), "-map", "0:v", "-map_chapters", "1", "-vf",
       f"scale={scale}", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", str(out))
    meta.unlink()
    return out


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    d = tmp_path_factory.mktemp("upgrade")
    original = make(SCENES, d / "original.mp4")                      # 48 s, 320x180
    trimmed = d / "trimmed.mp4"                                      # starts 5.3 s in
    ff("-ss", "5.3", "-i", str(original), "-t", "36", "-c:v", "libx264", "-preset", "ultrafast", str(trimmed))
    return {"original": original, "trimmed": trimmed, "unrelated": make(OTHER, d / "unrelated.mp4")}


@pytest.fixture
def setup(sources, tmp_path):
    lib, feed = tmp_path / "Library" / "Studio", tmp_path / "Feed"
    lib.mkdir(parents=True)
    feed.mkdir()
    curated = with_chapters(sources["trimmed"], lib / "My scene.mp4",
                            [(0, "Intro"), (10, "Kissing"), (20, "Talking")], "212:120")
    save_mp4(curated, keywords=["Beach", "Sunset"], genre=[],
             meta=VideoMeta("My scene", 2020, "Hand-written description", 4, "Studio", [SCENE_LINK], "2020-05-01"))
    better = feed / "Studio.20.05.01.My.Scene.1080p.mp4"
    shutil.copy(sources["original"], better)
    db = TagDB(tmp_path / "t.db")
    yield db, curated, better, tmp_path
    db.close()


def test_upgrade_puts_the_better_copy_in_place_with_everything(setup):
    db, curated, better, tmp = setup
    plan = U.plan_upgrade(db, curated, better)
    assert plan.sharper and plan.final == curated
    assert [m.name for m in plan.marks] == ["Intro", "Kissing", "Talking"] and not plan.dropped
    # the better copy has 5.3 s more at the start, so the chapters move 5.3 s later (to the 0.5 s sampling grid)
    assert plan.marks[0].start == 0
    assert abs(plan.marks[1].start - 15.3) <= 0.6 and abs(plan.marks[2].start - 25.3) <= 0.6
    assert plan.shift < 0 and "later" in " ".join(plan.summary())
    result = U.do_upgrade(db, plan, old_to=tmp / "Feed" / "_Rejected")
    assert result.final == curated and not result.problems
    assert not better.exists()
    assert result.old_now == tmp / "Feed" / "_Rejected" / "My scene.mp4" and result.old_now.exists()
    info = probe(curated)
    assert info.main_video.width == 320                                   # the sharper picture
    assert [c.title for c in info.chapters] == ["Intro", "Kissing", "Talking"]
    assert abs(info.chapters[1].start - 15.3) <= 0.6
    tags = read_mp4_tags(curated)
    assert tags.keywords == ["Beach", "Sunset"] and tags.title == "My scene" and tags.date == "2020-05-01"
    assert SCENE_LINK in tags.links
    assert read_state(curated).meta.description == "Hand-written description"
    assert db.history(curated)[0].label == "Upgraded to a better copy"
    assert not list(curated.parent.glob(".*"))                            # no temporary files left


def test_an_unrelated_video_is_refused(setup, sources):
    db, curated, _better, tmp = setup
    other = tmp / "Feed" / "Other.mp4"
    shutil.copy(sources["unrelated"], other)
    with pytest.raises(ValueError, match="doesn't look like a copy"):
        U.plan_upgrade(db, curated, other)
    assert curated.exists() and other.exists()


def test_chapters_outside_the_better_copy_are_left_out():
    # A (curated) 0-60 s; B holds only A's 0-30 s (the second half was cut)
    match = D.Match([D.Run(0, 0, 60)], 2.0, 120, 60)
    marks = [ChapterMark(0, "Start"), ChapterMark(20, "Middle"), ChapterMark(50, "Late")]
    moved, dropped, shift = U.map_marks(marks, match, 30.0)
    assert [m.name for m in moved] == ["Start", "Middle"] and dropped == ["Late (0:50)"] and shift == 0
    # B starts 4 s later than A everywhere: chapters move 4 s earlier
    match = D.Match([D.Run(8, 0, 100)], 2.0, 120, 100)
    moved, dropped, shift = U.map_marks([ChapterMark(0, "S"), ChapterMark(30, "M")], match, 50.0)
    assert [m.start for m in moved] == [0.0, 26.0] and shift == 4.0


def test_mkv_curated_becomes_mp4(sources, tmp_path):
    curated = tmp_path / "Mine.mkv"
    ff("-i", str(sources["trimmed"]), "-vf", "scale=212:120", "-c:v", "libx264", "-preset", "ultrafast", str(curated))
    assert U.final_path(curated) == tmp_path / "Mine.mp4"
    assert subprocess.run(["ffprobe", "-v", "error", str(curated)], check=False).returncode == 0


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.mark.skipif(__import__("sys").platform != "win32" or bool(__import__("os").environ.get("GITHUB_ACTIONS")),
                    reason="needs Qt Multimedia playback on a real Windows desktop (crashes on GitHub's runners)")
def test_main_window_upgrade_for_a_plugin(setup, qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from test_fast_tagging import wait_until

    from runemark.settings import Settings
    from runemark.ui.main_window import MainWindow
    db, curated, better, tmp = setup
    asked = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: asked.append(self.text()) or QMessageBox.StandardButton.Yes)
    win = MainWindow(db, Settings.load())
    got = []
    try:
        win.show()
        win.upgrade_copy(curated, better, tmp / "old", lambda path, reason: got.append((path, reason)))
        assert wait_until(qapp, lambda: got, timeout=120)
        assert got == [(curated, "")] and "Put " in asked[0] and not better.exists()
        assert probe(curated).main_video.width == 320
    finally:
        win.close()
