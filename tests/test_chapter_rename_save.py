"""0.8.3: a chapter renamed to a tag name keeps that name after saving."""
import os
import shutil
import sys
import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLineEdit

from runemark.media.probe import probe
from runemark.settings import Settings
from runemark.tagdb import TagDB

needs_player = pytest.mark.skipif(sys.platform != "win32" and not os.environ.get("RUNEMARK_TRY_PLAYER"),
                                  reason="needs Qt Multimedia playback")


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def wait_until(qapp, condition, timeout=60.0):
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return False


def rename_in_table(qapp, panel, row, text, pick=False):
    from runemark.ui.chapters_panel import COL_NAME
    panel.table.editItem(panel.table.item(row, COL_NAME))
    for _ in range(3):
        qapp.processEvents()
    editor = panel.table.findChild(QLineEdit)
    if pick:                                          # type part of it, then take the suggestion
        editor.clear()
        QTest.keyClicks(editor, text)
        qapp.processEvents()
        popup = editor.completer().popup()
        QTest.keyClick(popup, Qt.Key.Key_Down)
        QTest.keyClick(popup, Qt.Key.Key_Return)
    else:
        editor.setText(text)
        QTest.keyClick(editor, Qt.Key.Key_Return)
    for _ in range(5):
        qapp.processEvents()


@needs_player
@pytest.mark.parametrize("pick", [False, True], ids=["typed", "suggestion"])
def test_renamed_chapter_survives_save(tmp_path, media, qapp, pick):
    from runemark.ui.main_window import MainWindow
    path = tmp_path / "a.mp4"
    shutil.copy2(media["mp4"], path)
    db = TagDB(tmp_path / "tags.db")
    db.add_tag("Lake", db.add_category("Places").id)
    win = MainWindow(db, Settings.load())
    try:
        win.show()
        win.open_paths([path])
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == path)
        win.chapters.add_at(1.0)                          # what M does, at a known time
        assert [m.name for m in win.doc.chapters] == ["Chapter 1", "Chapter 2"]
        rename_in_table(qapp, win.chapters, 1, "lak" if pick else "lake", pick)   # an existing tag
        assert [m.name for m in win.doc.chapters] == ["Chapter 1", "Lake"]
        assert "Lake" in win.doc.tags
        win.activateWindow()
        QTest.keyClick(win, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
        qapp.processEvents()
        if not win._busy:
            win.save()                                    # no keyboard focus on a test desktop
        assert wait_until(qapp, lambda: not win._busy and win.saves.idle() and win.doc is not None and not win.doc.dirty()
                          and win.doc.path == path and win.player.path is not None)
        assert [c.title for c in probe(path).chapters] == ["Chapter 1", "Lake"]
        assert [m.name for m in win.chapters.marks] == ["Chapter 1", "Lake"]
        assert [m.name for m in win.doc.chapters] == ["Chapter 1", "Lake"]
    finally:
        if win.doc is not None:
            win.doc.discard()
        win.close()
        db.close()


@needs_player
def test_rename_made_while_saving_is_kept(tmp_path, media, qapp):
    from runemark.ui.main_window import MainWindow
    path = tmp_path / "a.mp4"
    shutil.copy2(media["mp4"], path)
    db = TagDB(tmp_path / "tags.db")
    db.add_tag("Lake", db.add_category("Places").id)
    win = MainWindow(db, Settings.load())
    try:
        win.show()
        win.open_paths([path])
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == path)
        win.chapters.add_at(1.0)
        win.save()                                        # writes "Chapter 2"...
        rename_in_table(qapp, win.chapters, 1, "Lake")    # ...while the name is changed
        assert wait_until(qapp, lambda: not win._busy and win.saves.idle() and win.player.path is not None
                          and win.doc is not None and win.doc.state is not None
                          and [c.title for c in probe(path).chapters] == ["Chapter 1", "Chapter 2"]
                          and "kept" in win.status_text.text())
        assert [m.name for m in win.doc.chapters] == ["Chapter 1", "Lake"] and win.doc.dirty()
        assert [m.name for m in win.chapters.marks] == ["Chapter 1", "Lake"]
        win.save()
        assert wait_until(qapp, lambda: not win._busy and win.saves.idle() and win.doc is not None and not win.doc.dirty()
                          and win.player.path is not None)
        assert [c.title for c in probe(path).chapters] == ["Chapter 1", "Lake"]
    finally:
        if win.doc is not None:
            win.doc.discard()
        win.close()
        db.close()
