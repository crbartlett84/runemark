"""0.9.1 fixes: the length shown while a video is still saving, K and the speed readout, the first click after
typing a smart-filter value."""
from __future__ import annotations

import threading

from PySide6.QtCore import Qt

from conftest import ff
from test_v090 import _close, _window, needs_window, qapp, wait_until  # noqa: F401


# K and the speed readout: test_v090.test_shuttle_jkl (K gives 1×, and the readout's label for it).


def test_first_add_a_rule_click_after_typing_a_value_adds_a_row(qapp):
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QPushButton
    from PySide6.QtCore import QThreadPool
    from runemark.ui.smart_filter_dialog import SmartFilterDialog
    QThreadPool.globalInstance().waitForDone(60000)       # the windows of earlier tests: their pictures are done
    qapp.processEvents()
    d = SmartFilterDialog(None, {"tag": ["Lake", "Lakeside", "Forest"], "person": [], "studio": []}, [], [])
    d.show()
    try:
        assert wait_until(qapp, lambda: d.windowHandle() is not None and d.windowHandle().isExposed(), 5)
        row = d.rows[0]
        row.value.setFocus()
        row.value.setText("La")                 # typed, as far as the suggestions go (no key events: earlier
        completer = row.value.completer()       # tests' windows still watch the keyboard)
        completer.setCompletionPrefix("La")
        completer.complete()
        wait_until(qapp, lambda: False, 0.1)
        assert QApplication.activePopupWidget() is not None          # the suggestions are open
        add = next(b for b in d.findChildren(QPushButton) if b.text() == "Add a rule")
        QTest.mouseClick(d.windowHandle(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         add.mapTo(d, add.rect().center()))
        assert wait_until(qapp, lambda: len(d.rows) == 2, 5)          # the first click counts
        assert QApplication.activePopupWidget() is None and d.rows[0].value.text() == "La"
    finally:
        d.close()


@needs_window
def test_a_video_still_saving_shows_its_own_length(tmp_path, media, qapp):
    from runemark.media.probe import fmt_duration, probe
    win, db, (a,) = _window(tmp_path, media, ("a.mp4",))
    try:
        b = tmp_path / "b.mp4"                                    # a shorter video, opened next
        ff("-i", str(a), "-t", "2", "-c", "copy", str(b))
        assert round(probe(a).duration) != round(probe(b).duration)
        gate = threading.Event()
        real = win._queue_mp4_save

        def slow_save(doc, after=None, on_failed=None):
            job = real(doc, after, on_failed)
            work = job.work
            job.work = lambda j: (gate.wait(20), work(j))[1]
            return job
        win._queue_mp4_save = slow_save
        win.open_paths([a, b])
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == a)
        win._tags_edited(["Lake"])
        win.save_and_next()
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == b
                          and win.player.duration_label.text() == "/ " + fmt_duration(probe(b).duration), 20)
        win.open_file(a)                                          # still saving: read-only, with a's own length
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == a and win.doc.read_only)
        wait_until(qapp, lambda: False, 1.0)                      # let the player's own signals arrive
        assert win.player.duration_label.text() == "/ " + fmt_duration(win.doc.info.duration)
        assert win.player.time_label.text() == "0:00:00.000"
        gate.set()
        assert wait_until(qapp, lambda: win.saves.idle() and not win.doc.read_only, 30)
    finally:
        _close(win, db, qapp)

