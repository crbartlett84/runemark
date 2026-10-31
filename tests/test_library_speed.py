"""Round 5 (v0.3.0): the Library's table model, debounced search, and the one-pass, batched scan."""
import shutil
import time

import pytest
from PySide6.QtCore import Qt

from runemark import library as L
from runemark.settings import Settings
from runemark.tagdb import FileRecord, TagDB


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def rec(name, title="", year=None, rating=0, tags=(), duration=None):
    return FileRecord(f"/v/{name}", list(tags), "mp4", title, year, rating, False, duration, [], False, None)


def test_model_sorts_with_blanks_last(qapp):
    from runemark.ui.library_model import LibraryModel
    m = LibraryModel()
    m.set_records([rec("b.mp4", year=2001), rec("a.mp4"), rec("c.mp4", year=1999)])
    m.sort(2, Qt.SortOrder.AscendingOrder)                 # Year
    assert [r.path[-5:] for r in m.rows] == ["c.mp4", "b.mp4", "a.mp4"]
    m.set_records([rec("x.mp4"), rec("y.mp4", year=1990)])  # a new search keeps the chosen sort
    assert [r.path[-5:] for r in m.rows] == ["y.mp4", "x.mp4"]
    assert m.data(m.index(0, 2)) == "1990" and m.data(m.index(1, 2)) == ""
    assert m.data(m.index(0, 0), Qt.ItemDataRole.ToolTipRole) == "/v/y.mp4"
    m.set_records([rec("u.mp4"), rec("r3.mp4", rating=3), rec("r5.mp4", rating=5)])
    m.sort(3, Qt.SortOrder.DescendingOrder)                # highest rated first: unrated stay at the bottom
    assert [r.path[-6:] for r in m.rows] == ["r5.mp4", "r3.mp4", "/u.mp4"]


def test_search_waits_for_a_pause_and_selection_follows_sort(tmp_path, qapp):
    from runemark.ui.library_window import LibraryWindow
    db = TagDB(tmp_path / "t.db")
    try:
        for i in range(30):
            db.index_file(tmp_path / f"clip {i:02d}.mp4", tags=["Lake"] if i % 3 == 0 else [], container="mp4",
                          title=f"Clip {i}", rating=i % 6)
        w = LibraryWindow(db, Settings.load(), host=None)
        assert w.model.rowCount() == 30
        w.text.setText("clip 1")
        assert w.model.rowCount() == 30                    # not yet: the search runs after a short pause
        end = time.time() + 2
        while time.time() < end and w.model.rowCount() == 30:
            qapp.processEvents()
            time.sleep(0.01)
        assert w.model.rowCount() == 11                    # clip 10-19, plus "clip 01" whose title is "Clip 1"
        w.table.sortByColumn(3, Qt.SortOrder.DescendingOrder)  # by rating
        w.table.selectRow(0)
        top = w.model.record(0)
        assert w.selected_paths()[0].name == top.path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        assert top.rating == 5
        w.close()
    finally:
        db.close()


def test_scan_reads_each_file_once_and_commits_in_batches(tmp_path, media, monkeypatch):
    folder = tmp_path / "v"
    folder.mkdir()
    for i in range(5):
        shutil.copy2(media["mp4_cover"], folder / f"c{i}.mp4")
    from mutagen import mp4 as mutagen_mp4
    from runemark.media import xtra
    opens = {"mp4": 0, "xtra": 0}
    real_mp4, real_payload = mutagen_mp4.MP4, xtra.read_payload

    class CountingMP4(real_mp4):
        def __init__(self, *a, **k):
            opens["mp4"] += 1
            super().__init__(*a, **k)

    def counting_payload(path):
        opens["xtra"] += 1
        return real_payload(path)
    monkeypatch.setattr(mutagen_mp4, "MP4", CountingMP4)
    monkeypatch.setattr(xtra, "read_payload", counting_payload)
    monkeypatch.setattr(L, "SCAN_COMMIT_EVERY", 2)
    db = TagDB(tmp_path / "t.db")
    try:
        result = L.scan(db, [str(folder)])
        assert result.read == 5
        assert opens == {"mp4": 5, "xtra": 5}              # one parse + one Xtra read per file
        records = db.library()
        assert len(records) == 5 and all(r.has_cover and r.duration for r in records)
        assert L.scan(db, [str(folder)]).read == 0          # unchanged files skipped via the one-query lookup
    finally:
        db.close()
