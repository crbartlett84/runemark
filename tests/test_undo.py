"""Round 4 (v0.3.0): undo. Each save remembers the file's tags, genre and details from just before it
(last 10 per file); a file steps back one save at a time, and a bulk change can be undone as a whole."""
import shutil
import sqlite3
import sys
import time

import pytest

from runemark import library as L
from runemark.media.mp4meta import VideoMeta
from runemark.settings import Settings
from runemark.tagdb import HISTORY_KEEP, TagDB
from runemark.tagsync import read_state, save_mp4


@pytest.fixture
def db(tmp_path):
    d = TagDB(tmp_path / "tags.db")
    yield d
    d.close()


def snap(db, path, **kw):
    fields = dict(keywords=[], genre=[], title="", year=None, description="", rating=0)
    fields.update(kw)
    db.record_history(path, **fields)


# --- the history table ------------------------------------------------------------------------------------

def test_history_newest_first_and_capped(db, tmp_path):
    f = tmp_path / "a.mp4"
    for n in range(HISTORY_KEEP + 5):
        snap(db, f, keywords=[f"v{n}"])
    h = db.history(f)
    assert len(h) == HISTORY_KEEP
    assert h[0].keywords == [f"v{HISTORY_KEEP + 4}"] and h[-1].keywords == ["v5"]


def test_history_follows_a_conversion(db, tmp_path):
    mkv, mp4 = tmp_path / "a.mkv", tmp_path / "a.mp4"
    snap(db, mkv, keywords=["old"])
    db.move_file(mkv, mp4)
    assert db.history(mkv) == [] and db.history(mp4)[0].keywords == ["old"]


def test_last_batch_and_changed_since(db, tmp_path):
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    snap(db, a, keywords=["single"])                     # an editor save: not a batch
    assert db.last_batch() == []
    db.record_history(a, keywords=["x"], genre=[], label="Library: add Lake", batch="B1")
    db.record_history(b, keywords=["y"], genre=[], label="Library: add Lake", batch="B1")
    batch = db.last_batch()
    assert [e.path for e in batch] == [str(a), str(b)] and batch[0].label == "Library: add Lake"
    assert not db.changed_since(batch[0])
    snap(db, a, keywords=["later edit"])
    assert db.changed_since(batch[0]) and not db.changed_since(batch[1])


def test_old_database_gains_history(tmp_path):
    """A v0.2.0 database (no file_history table) is upgraded when opened, keeping its data."""
    path = tmp_path / "old.db"
    d = TagDB(path)
    d.add_tag("Lake", d.add_category("Places").id)
    d.close()
    con = sqlite3.connect(path)
    con.execute("DROP TABLE file_history")
    con.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    con.commit()
    con.close()
    d = TagDB(path)
    try:
        assert [t.name for t in d.tags()] == ["Lake"]
        snap(d, tmp_path / "a.mp4", keywords=["Lake"])
        assert len(d.history(tmp_path / "a.mp4")) == 1
    finally:
        d.close()


# --- reverting files --------------------------------------------------------------------------------------

def test_revert_steps_back_one_save_at_a_time(db, copy_of):
    path = copy_of("mp4")
    for tags, rating in ((["One"], 1), (["One", "Two"], 2), (["Three"], 5)):
        state = read_state(path)
        save_mp4(path, keywords=tags, genre=[], meta=VideoMeta("T", 2020, "D", rating))
        db.record_history(path, **L.history_fields(state), label="Saved in the editor")
    assert read_state(path).keywords == ["Three"]

    L.revert_file(db, db.history(path)[0])
    s = read_state(path)
    assert (s.keywords, s.meta.rating) == (["One", "Two"], 2)
    L.revert_file(db, db.history(path)[0])
    s = read_state(path)
    assert (s.keywords, s.meta.rating) == (["One"], 1)
    L.revert_file(db, db.history(path)[0])
    s = read_state(path)
    assert (s.keywords, s.meta.title, s.meta.rating) == ([], "", 0)    # back to the untouched file
    assert db.history(path) == []


def test_undo_bulk_change(tmp_path, media, db):
    folder = tmp_path / "v"
    folder.mkdir()
    files = []
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        shutil.copy2(media["mp4"], folder / name)
        files.append(folder / name)
    save_mp4(files[0], keywords=["Keep"], genre=[], meta=VideoMeta("", None, "", 3))
    db.add_tag("Lake", db.add_category("Places").id)

    assert L.apply_bulk(db, files, L.BulkChange(add=["Lake"], rating=5)) == []
    assert read_state(files[0]).keywords == ["Keep", "Lake"]
    batch = db.last_batch()
    assert len(batch) == 3 and batch[0].label == "Library: add Lake; set the rating to 5 stars"

    # one file is edited again after the bulk change: undo must not throw that away
    state = read_state(files[2])
    save_mp4(files[2], keywords=["Lake", "Mine"], genre=[])
    db.record_history(files[2], **L.history_fields(state), label="Saved in the editor")

    problems = L.undo_batch(db, db.last_batch())
    assert [(p.name, "changed again" in why) for p, why in problems] == [("c.mp4", True)]
    a, b, c = (read_state(f) for f in files)
    assert (a.keywords, a.meta.rating) == (["Keep"], 3)
    assert (b.keywords, b.meta.rating) == ([], 0)
    assert c.keywords == ["Lake", "Mine"]                 # left alone


def test_undo_bulk_reports_missing_files(tmp_path, media, db):
    f = tmp_path / "a.mp4"
    shutil.copy2(media["mp4"], f)
    L.apply_bulk(db, [f], L.BulkChange(add=["X"]))
    f.unlink()
    problems = L.undo_batch(db, db.last_batch())
    assert len(problems) == 1 and "isn't there" in problems[0][1]


# --- the editor's Undo last save (needs the video player: Windows build only) ------------------------------
needs_player = pytest.mark.skipif(sys.platform != "win32", reason="needs Qt Multimedia playback (Windows build)")


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def wait_until(qapp, condition, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return False


@needs_player
def test_editor_save_then_undo(tmp_path, media, qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from runemark.ui.main_window import MainWindow
    a = tmp_path / "a.mp4"
    shutil.copy2(media["mp4"], a)
    db = TagDB(tmp_path / "tags.db")
    win = MainWindow(db, Settings.load())
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kw: QMessageBox.StandardButton.Yes)
    try:
        win.show()
        win.open_paths([a])
        assert wait_until(qapp, lambda: win.doc is not None)
        win._tags_edited(["Lake"])
        win.save()
        assert wait_until(qapp, lambda: not win._busy and read_state(a).keywords == ["Lake"])
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.tags == ["Lake"])
        win.undo_last_save()
        assert wait_until(qapp, lambda: not win._busy and read_state(a).keywords == [])
        assert db.history(a) == []
    finally:
        if win.doc is not None:
            win.doc.discard()
        win.close()
        db.close()
