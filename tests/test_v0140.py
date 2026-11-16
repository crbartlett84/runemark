"""0.14.0: suggested chapters (a pasted list of times, or a plugin), added only when the user adds them."""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from runemark.media.timestamps import parse_timestamps


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_parse_common_lists():
    text = """Chapters:
0:00 Intro
4:32 - The interview
[12:05] Behind the scenes
1:02:10 Credits
no time on this line
"""
    assert parse_timestamps(text) == [(0.0, "Intro"), (272.0, "The interview"), (725.0, "Behind the scenes"),
                                      (3730.0, "Credits")]


def test_parse_odd_shapes():
    assert parse_timestamps("1. 00:30 First\n2) 01:15.500 Second") == [(30.0, "First"), (75.5, "Second")]
    assert parse_timestamps("Interview 4:32") == [(272.0, "Interview")]
    assert parse_timestamps("0:00 - 4:31 Intro") == [(0.0, "Intro")]            # a range: its start
    assert parse_timestamps("4:32") == [(272.0, "")]
    assert parse_timestamps("5:00 A; B") == [(300.0, "A, B")]                    # ; separates tags in files
    assert parse_timestamps("0:75 nope\n99:99 nope") == []
    assert parse_timestamps("2:00 kept\n9:00 past the end", duration=300) == [(120.0, "kept")]
    assert parse_timestamps("1:00 a\n1:00 b\n0:10 first") == [(10.0, "first"), (60.0, "a")]
    assert parse_timestamps("") == []


def _panel(qapp):
    from runemark.ui.chapters_panel import ChaptersPanel
    panel = ChaptersPanel()
    panel.set_enabled(True, "mp4")
    panel.duration = 600.0
    return panel


def test_suggestions_show_and_add(qapp):
    panel = _panel(qapp)
    panel.set_tag_names(["Interview"])
    named, changed = [], []
    panel.nameTagged.connect(named.append)
    panel.changed.connect(lambda: changed.append(1))
    panel.add_at(100.0)
    changed.clear()
    n = panel.suggest([{"start": 30, "name": "interview", "end": 60}, {"start": 100.4, "name": "dupe"},
                       {"start": 200, "name": ""}, {"start": 900, "name": "past the end"}, {"name": "no time"}], "Test")
    assert n == 2 and [s["start"] for s in panel.suggestions] == [30.0, 200.0]   # near a chapter / past the end: out
    assert not panel.sug_box.isHidden() and not changed and len(panel.marks) == 1  # nothing added yet

    panel.accept_suggestions(panel.suggestions[:1])
    assert [(m.start, m.name, m.end) for m in panel.marks][0] == (30.0, "Interview", 60.0)   # the tag's spelling
    assert named == ["Interview"] and changed and len(panel.suggestions) == 1

    panel.accept_suggestions(list(panel.suggestions))
    assert [m.start for m in panel.marks] == [30.0, 100.0, 200.0] and panel.marks[2].auto
    assert panel.suggestions == [] and panel.sug_box.isHidden()


def test_same_source_replaces_and_end_never_overlaps(qapp):
    panel = _panel(qapp)
    panel.suggest([{"start": 10, "name": "A"}], "Plugin")
    panel.suggest([{"start": 20, "name": "B", "end": 500}, {"start": 40, "name": "C"}], "Plugin")
    panel.suggest([{"start": 50, "name": "D"}], "Pasted list")
    assert [(s["start"], s["source"]) for s in panel.suggestions] == [(20.0, "Plugin"), (40.0, "Plugin"),
                                                                       (50.0, "Pasted list")]
    panel.accept_suggestions(list(panel.suggestions))
    assert panel.marks[0].end is None                 # its end (500) is past the next chapter: runs on as usual
    panel.clear_suggestions()
    assert panel.suggestions == []


def test_add_at_this_frame_takes_the_name(qapp):
    panel = _panel(qapp)
    panel.suggest([{"start": 30, "name": "Interview", "end": 80}, {"start": 90, "name": "Later"}], "Test")
    panel.addRequested.connect(lambda: panel.add_at(31.25))      # the window supplies the frame on screen
    panel.sug_table.selectRow(0)
    panel._add_here()
    assert [(m.start, m.name, m.end) for m in panel.marks] == [(31.25, "Interview", 80.0)]
    assert [s["name"] for s in panel.suggestions] == ["Later"] and panel._pending is None
    panel.add_at(200.0)                                           # an ordinary add afterwards is numbered
    assert panel.marks[-1].auto


def test_dismiss_and_disabled(qapp):
    panel = _panel(qapp)
    panel.suggest([{"start": 30, "name": "A"}, {"start": 60, "name": "B"}], "Test")
    panel.sug_table.selectRow(1)
    panel._dismiss()
    assert [s["name"] for s in panel.suggestions] == ["A"]
    panel.set_enabled(False)
    panel.accept_suggestions(list(panel.suggestions))
    assert panel.marks == []                                      # nothing is added to a video that can't be edited


needs_player = pytest.mark.skipif(sys.platform != "win32" and not os.environ.get("RUNEMARK_TRY_PLAYER"),
                                  reason="needs Qt Multimedia playback")


@needs_player
def test_plugin_suggestions_in_the_window(tmp_path, media, qapp):
    """A plugin's suggestions reach the Chapters tab for the open video only, and adding one edits the document."""
    import shutil
    import time
    from runemark.settings import Settings
    from runemark.tagdb import TagDB
    from runemark.ui.main_window import MainWindow

    def wait_until(condition, timeout=60.0):
        end = time.time() + timeout
        while time.time() < end:
            qapp.processEvents()
            if condition():
                return True
            time.sleep(0.02)
        return False

    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    shutil.copy2(media["mp4"], a)
    shutil.copy2(media["mp4"], b)
    db = TagDB(tmp_path / "tags.db")
    win = MainWindow(db, Settings.load())
    try:
        win.show()
        win.open_paths([a])
        assert wait_until(lambda: win.doc is not None and win.doc.path == a)
        assert win.suggest_chapters([{"start": 1.0, "name": "Lake"}], "Test", path=b) == 0    # a late answer
        assert win.suggest_chapters([{"start": 1.0, "name": "Lake"}, {"start": 2.0, "name": ""}], "Test", path=a) == 2
        assert win.doc.chapters == [] or [m.start for m in win.doc.chapters] == []           # nothing added yet
        win.chapters.accept_suggestions(win.chapters.suggestions[:1])
        assert [m.name for m in win.doc.chapters] == ["Lake"] and "Lake" in win.doc.tags
        win._confirm_leave = lambda then: True            # no "Unsaved changes" box on a test desktop
        win.open_paths([b])
        assert wait_until(lambda: win.doc is not None and win.doc.path == b)
        assert win.chapters.suggestions == []                                                # they went with the video
    finally:
        if win.doc is not None:
            win.doc.discard()
        win.close()
        db.close()
