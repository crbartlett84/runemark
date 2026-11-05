"""0.8.3: the player keys (Space, arrows, M, E) keep working after clicking in a list."""
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
import pytest
from PySide6.QtWidgets import QAbstractItemView, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QSpinBox


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_lists_hand_over_keys_but_text_boxes_keep_them(qapp):
    from runemark.ui.main_window import takes_typing
    lst = QListWidget()
    item = QListWidgetItem("Chapter 1")
    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
    lst.addItem(item)
    assert not takes_typing(lst)                       # a selected list: keys go to the player
    assert not takes_typing(None)
    assert takes_typing(QLineEdit()) and takes_typing(QPlainTextEdit()) and takes_typing(QSpinBox())
    lst.show()
    lst.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
    lst.editItem(item)                                 # an editor open in the list keeps them
    assert lst.state() == QAbstractItemView.State.EditingState and takes_typing(lst)
    lst.close()


class _Player:
    def __init__(self):
        self.calls = []

    def toggle_play(self):
        self.calls.append("play")

    def step(self, n):
        self.calls.append(("step", n))

    def nudge(self, n):
        self.calls.append(("nudge", n))

    def seek_seconds(self, s):
        self.calls.append(("seek", s))


def test_window_takes_player_keys_from_its_lists(qapp):
    from runemark.ui.main_window import MainWindow

    class Win:                                         # just enough of MainWindow for its key routing
        _player_key = MainWindow._player_key

        def __init__(self):
            self.doc = object()
            self.player = _Player()
            self.added, self.ended = [], 0

        def add_chapter(self, key=None):
            self.added.append(key)

        def end_chapter(self):
            self.ended += 1

    win = Win()
    lst = QListWidget()
    lst.show()
    lst.setFocus()
    qapp.processEvents()

    def press(key, mods=Qt.KeyboardModifier.NoModifier):
        return win._player_key(QKeyEvent(QEvent.Type.KeyPress, key, mods))

    from PySide6.QtWidgets import QApplication
    if QApplication.focusWidget() is lst:              # offscreen platforms may not give focus
        assert press(Qt.Key.Key_Space) and press(Qt.Key.Key_M) and press(Qt.Key.Key_E)
        assert press(Qt.Key.Key_Right) and press(Qt.Key.Key_Left, Qt.KeyboardModifier.ShiftModifier)
        assert win.player.calls == ["play", ("step", 1), ("nudge", -5)]
        assert win.added == [None] and win.ended == 1
        assert not press(Qt.Key.Key_A)                 # other keys stay with the list
    win.doc = None
    assert not press(Qt.Key.Key_Space)                 # no video open: nothing to do
    lst.close()
