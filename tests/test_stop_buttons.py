"""Round 2 (v0.3.0): long jobs can be stopped, and closing the Library mid-scan is safe."""
import shutil
import sys
import threading

import pytest

from runemark import library as L
from runemark.settings import Settings
from runemark.tagdb import TagDB


def _folder_of_videos(tmp_path, media, n=6):
    folder = tmp_path / "videos"
    folder.mkdir()
    for i in range(n):
        shutil.copy2(media["mp4"], folder / f"clip{i}.mp4")
    return folder


def test_scan_stops_when_asked_and_keeps_what_it_read(tmp_path, media):
    folder = _folder_of_videos(tmp_path, media)
    db = TagDB(tmp_path / "tags.db")
    try:
        cancel = threading.Event()
        seen = []

        def progress(_pct, message):
            if message.startswith("Reading"):
                seen.append(message)
                if len(seen) == 1:        # progress is reported every few files, so stop at the first
                    cancel.set()          # the user presses Stop partway through

        result = L.scan(db, [str(folder)], progress=progress, cancel=cancel)
        assert result.stopped
        assert 0 < result.read < result.found == 6
        assert len(db.library()) == result.read        # what was read is kept
        again = L.scan(db, [str(folder)])               # scanning again finishes the job
        assert not again.stopped and again.read == 6 - result.read
    finally:
        db.close()


def test_scan_cancelled_before_reading(tmp_path, media):
    folder = _folder_of_videos(tmp_path, media, n=2)
    db = TagDB(tmp_path / "tags.db")
    try:
        cancel = threading.Event()
        cancel.set()
        result = L.scan(db, [str(folder)], cancel=cancel)
        assert result.stopped and result.read == 0
    finally:
        db.close()


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_closing_library_mid_scan_is_safe(tmp_path, media, qapp, monkeypatch):
    """The main window opens the Library with WA_DeleteOnClose. Closing it during a scan used to leave
    the scan's callbacks calling into a deleted window."""
    from PySide6.QtCore import QThreadPool, Qt
    from runemark.ui.library_window import LibraryWindow

    folder = _folder_of_videos(tmp_path, media, n=8)
    db = TagDB(tmp_path / "tags.db")
    settings = Settings.load()
    settings.library_folders = [str(folder)]
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: errors.append(exc))
    try:
        window = LibraryWindow(db, settings, host=None)
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        window.show()
        window.scan()
        assert window.scan_button.text() == "Stop scan"
        job = window._scan_job
        window.close()
        assert job.cancel.is_set()                      # closing stops the scan
        QThreadPool.globalInstance().waitForDone(30_000)
        for _ in range(20):
            qapp.processEvents()
        assert errors == []
    finally:
        db.close()


def test_stop_button_stops_the_scan(tmp_path, media, qapp):
    from PySide6.QtCore import QThreadPool
    from runemark.ui.library_window import LibraryWindow

    folder = _folder_of_videos(tmp_path, media, n=8)
    db = TagDB(tmp_path / "tags.db")
    settings = Settings.load()
    settings.library_folders = [str(folder)]
    try:
        window = LibraryWindow(db, settings, host=None)
        window.scan()
        window.scan_button.click()                      # now labelled "Stop scan"
        assert window._scan_job.cancel.is_set()
        QThreadPool.globalInstance().waitForDone(30_000)
        for _ in range(20):
            qapp.processEvents()
        assert window._scan_job is None
        assert window.scan_button.text() == "Scan for changes" and window.scan_button.isEnabled()
        window.close()
    finally:
        db.close()
