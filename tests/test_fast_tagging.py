"""Round 3 (v0.3.0): faster tagging. Save and next, copy/paste tags, several tags at once, Library keys,
and the Library remembering its search."""
import shutil
import sys
import time

import pytest

from runemark import library as L
from runemark.settings import Settings
from runemark.tagdb import TagDB
from runemark.tagsync import read_state


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


# --- several tags typed at once ------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Lake, Summer", ["Lake", "Summer"]),
    ("Lake;  Summer ;Aubree", ["Lake", "Summer", "Aubree"]),
    ("lake, LAKE, Lake", ["Lake"]),                     # duplicates dropped, known spelling used
    ("Halifax, NS", ["Halifax, NS"]),                   # a known tag that contains a comma stays whole
    ("Halifax, NS, Lake", ["Halifax", "NS", "Lake"]),   # otherwise commas separate
    ("  ", []),
])
def test_split_tag_input(text, expected):
    assert L.split_tag_input(text, ["Lake", "Halifax, NS"]) == expected


# --- Library: remembered search and keys -----------------------------------------------------------------

def test_library_remembers_search_and_filter(tmp_path, qapp):
    from runemark.ui.library_window import LibraryWindow
    db = TagDB(tmp_path / "tags.db")
    settings = Settings.load()
    try:
        w = LibraryWindow(db, settings, host=None)
        w._set_search(L.Search(["Lake"], "holiday", L.FILTER_MKV))
        w.close()
        assert settings.library_last_search == {"tags": ["Lake"], "text": "holiday", "filter": L.FILTER_MKV}
        again = LibraryWindow(db, settings, host=None)
        assert again.search.tags == ["Lake"] and again.text.text() == "holiday"
        assert again.filter.currentData() == L.FILTER_MKV
        again.close()
    finally:
        db.close()


def test_library_number_keys_rate_and_t_adds_tags(tmp_path, media, qapp, monkeypatch):
    from PySide6.QtCore import Qt
    from runemark.ui.library_window import LibraryWindow
    folder = tmp_path / "v"
    folder.mkdir()
    shutil.copy2(media["mp4"], folder / "a.mp4")
    db = TagDB(tmp_path / "tags.db")
    L.scan(db, [str(folder)])
    settings = Settings.load()
    try:
        w = LibraryWindow(db, settings, host=None)
        changes, adds = [], []
        monkeypatch.setattr(w, "_run_bulk", changes.append)
        monkeypatch.setattr(w, "_bulk_add", lambda: adds.append(True))
        from PySide6.QtGui import QShortcut
        w.table.selectRow(0)
        # Offscreen Qt doesn't route key presses to shortcuts (no active window), so fire the bound shortcuts;
        # the real key presses are checked in the Windows click-through.
        keys = {sc.key().toString(): sc for sc in w.table.findChildren(QShortcut)}
        assert {"0", "1", "2", "3", "4", "5", "T", "Return", "Enter"} <= set(keys)
        assert all(sc.context() == Qt.ShortcutContext.WidgetShortcut for sc in keys.values())  # not while typing
        keys["4"].activated.emit()
        keys["0"].activated.emit()
        keys["T"].activated.emit()
        assert [c.rating for c in changes] == [4, 0]
        assert adds == [True]
        w.close()
    finally:
        db.close()


# --- main window: Save and next, copy and paste ------------------------------------------------------------
# The main window builds a video player, which needs a working Qt Multimedia backend: present on Windows (the
# build machine), missing on headless Linux, where creating it crashes. So these run on Windows only.
needs_player = pytest.mark.skipif(sys.platform != "win32", reason="needs Qt Multimedia playback (Windows build)")

@needs_player
def test_save_and_next_then_paste_tags(tmp_path, media, qapp):
    from runemark.ui.main_window import MainWindow
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    shutil.copy2(media["mp4"], a)
    shutil.copy2(media["mp4"], b)
    db = TagDB(tmp_path / "tags.db")
    db.add_tag("Lake", db.add_category("Places").id)
    settings = Settings.load()
    win = MainWindow(db, settings)
    try:
        win.show()
        win.open_paths([a, b])
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == a)

        win._tags_edited(["Lake"])
        assert win.doc.dirty()
        win.save_and_next()
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == b and not win._busy), \
            "Save and next should save a.mp4 and then open b.mp4"
        assert read_state(a).keywords == ["Lake"]
        assert win.doc.tags == []

        win.paste_tags()                                  # the last saved file's tags
        assert win.doc.tags == ["Lake"] and win.doc.dirty()

        win.doc.tags = ["Other"]
        win.copy_tags()
        assert win._tag_clipboard == ["Other"]
        from PySide6.QtWidgets import QApplication
        assert QApplication.clipboard().text() == "Other"
    finally:
        if win.doc is not None:
            win.doc.discard()                             # no "unsaved changes" prompt on close
        win.close()
        db.close()


@needs_player
def test_save_and_next_without_changes_just_moves(tmp_path, media, qapp):
    from runemark.ui.main_window import MainWindow
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    shutil.copy2(media["mp4"], a)
    shutil.copy2(media["mp4"], b)
    before = a.stat().st_mtime_ns
    db = TagDB(tmp_path / "tags.db")
    win = MainWindow(db, Settings.load())
    try:
        win.show()
        win.open_paths([a, b])
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == a)
        win.save_and_next()
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == b)
        assert a.stat().st_mtime_ns == before             # nothing written
    finally:
        win.close()
        db.close()


def test_bulk_prompt_keeps_tag_case():
    change = L.BulkChange(add=["Lake", "Summer", "NS"])
    assert L.sentence_case(change.describe()) == "Add Lake, Summer, NS"
    assert L.sentence_case("") == ""


def test_library_keeps_selection_after_refresh(tmp_path, qapp):
    """Rate a few files, then press T: the same files should still be selected after the first change."""
    from runemark.ui.library_window import LibraryWindow
    db = TagDB(tmp_path / "tags.db")
    try:
        for i in range(4):
            db.index_file(tmp_path / f"c{i}.mp4", tags=[], container="mp4", title=f"C{i}")
        w = LibraryWindow(db, Settings.load(), host=None)
        w.table.selectRow(1)
        w.table.selectionModel().select(w.model.index(2, 0), w.table.selectionModel().SelectionFlag.Select
                                        | w.table.selectionModel().SelectionFlag.Rows)
        before = [p.name for p in w.selected_paths()]
        assert before == ["c1.mp4", "c2.mp4"]
        w.refresh()
        assert [p.name for p in w.selected_paths()] == before
        w.close()
    finally:
        db.close()
