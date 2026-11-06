"""0.10.2: a save that a save handler writes somewhere else says where in the status bar, and details filled in by
themselves (from a .nfo) aren't shown as unsaved changes on their own, matching what leaving the video asks about."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from test_v0100 import _close, _window, needs_window, no_handlers, qapp, wait_until  # noqa: F401

NFO = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<movie><title>Placeholder title</title><premiered>2019-07-14</premiered><studio>Studio A</studio>
<actor><name>Person One</name></actor></movie>
"""


def test_where_a_handler_saves_comes_from_its_progress_line():
    from runemark.ui.save_queue import save_place
    assert save_place("Saving on the server…", "a.mp4") == "on the server"
    assert save_place("Saving on the server: Writing the chapters", "a.mp4") == "on the server"
    assert save_place("Saving a.mp4…", "a.mp4") == ""             # Runemark's own line (a conversion)
    assert save_place("Saving locally…", "a.mp4") == ""
    assert save_place("Making a working copy… 42%", "a.mp4") == ""
    assert save_place("", "a.mp4") == "" and save_place(None) == ""


def test_the_status_bar_says_a_save_is_written_elsewhere(qapp):
    from runemark.ui.save_queue import SaveJob, SaveQueue
    q = SaveQueue()
    job = SaveJob(Path("a.mp4"), lambda w: None)
    q._running = job
    job.percent, job.message = -1, "Sending the changes to the server…"
    assert q.summary() == "Saving a.mp4… Sending the changes to the server…"
    job.message = "Saving on the server…"
    assert q.summary() == "Saving a.mp4 on the server…"
    job.percent, job.message = 40, "Saving on the server: Writing the chapters"
    assert q.summary() == "Saving a.mp4 on the server… 40%"
    job.percent, job.message = 55, ""                             # a bare percentage: still labelled
    assert q.summary() == "Saving a.mp4 on the server… 55%"
    here = SaveJob(Path("b.mp4"), lambda w: None)                  # saved here: as before
    q._running = here
    here.percent, here.message = 42, "Making a working copy… 42%"
    assert q.summary() == "Saving b.mp4… 42%"
    here.percent, here.message = -1, "Saving b.mp4…"
    assert q.summary() == "Saving b.mp4…"
    assert job.clone().where == ""                                # Retry starts again from nothing
    q._running = None


@needs_window
def test_details_filled_in_from_the_nfo_alone_arent_shown_as_unsaved(tmp_path, media, qapp, monkeypatch):
    monkeypatch.setattr(QMessageBox, "exec", lambda self: (_ for _ in ()).throw(AssertionError("asked")))
    win, db, (a, b) = _window(tmp_path, media)
    try:
        a.with_suffix(".nfo").write_text(NFO, encoding="utf-8")
        win.open_paths([a, b])
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == a)
        doc = win.doc
        assert doc.meta.title == "Placeholder title" and doc.dirty() and not doc.has_own_changes()
        assert win.dirty_dot.isHidden() and "•" not in win.windowTitle()
        assert win.save_button.isEnabled()                         # still saved with the next save
        details = win.plugin_details()
        assert details["unsaved"] is True and details["own_changes"] is False
        win._tags_edited(["Lake"])                                 # an edit of his own: shown, and asked about
        assert not win.dirty_dot.isHidden() and "•" in win.windowTitle()
        assert win.plugin_details()["own_changes"] is True
        win.doc.tags = list(win.doc.saved_tags)
        win._refresh_ui()
        win.open_file(b)                                           # the fill-in alone: switches without asking
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == b)
    finally:
        _close(win, db, qapp)
