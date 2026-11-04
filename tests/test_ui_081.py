"""0.8.1: the timeline jumps where it's clicked; the Library keeps files from other folders out of the libraries."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from runemark.ui.player import JumpSlider


def _app():
    return QApplication.instance() or QApplication([])


def _mouse(kind, x, y=5):
    return QMouseEvent(kind, QPointF(x, y), QPointF(x, y), Qt.MouseButton.LeftButton,
                       Qt.MouseButton.LeftButton if kind != QMouseEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton,
                       Qt.KeyboardModifier.NoModifier)


def test_click_on_the_line_jumps_there_and_drags_on():
    _app()
    s = JumpSlider(Qt.Orientation.Horizontal)
    s.setRange(0, 100_000)
    s.resize(1000, 20)
    moved, pressed, released = [], [], []
    s.sliderMoved.connect(moved.append)
    s.sliderPressed.connect(lambda: pressed.append(1))
    s.sliderReleased.connect(lambda: released.append(1))
    s.mousePressEvent(_mouse(QMouseEvent.Type.MouseButtonPress, 750))
    assert pressed and moved, "a click on the line seeks"
    assert 70_000 <= s.value() <= 80_000
    s.mouseMoveEvent(_mouse(QMouseEvent.Type.MouseMove, 250))
    assert 20_000 <= s.value() <= 30_000
    s.mouseReleaseEvent(_mouse(QMouseEvent.Type.MouseButtonRelease, 250))
    assert released and not s.isSliderDown()


class _Settings:
    def __init__(self, libraries, shown=""):
        self.libraries, self.library_shown = libraries, shown

    def save(self):
        pass


class _Rec:
    def __init__(self, path):
        self.path = path


def test_library_views_keep_other_folders_apart(tmp_path):
    from runemark.ui.library_window import LibraryWindow
    lib_a, lib_b, dl = tmp_path / "A", tmp_path / "B", tmp_path / "Downloads"
    recs = [_Rec(str(lib_a / "1.mp4")), _Rec(str(lib_b / "2.mp4")), _Rec(str(dl / "3.mp4"))]
    st = _Settings([{"name": "Whisparr", "folders": [str(lib_a)], "skip": []},
                    {"name": "Mine", "folders": [str(lib_b)], "skip": []}])

    class W:  # just the view logic, without building the whole window
        SHOW_ALL, SHOW_OUTSIDE = LibraryWindow.SHOW_ALL, LibraryWindow.SHOW_OUTSIDE
        _shown_library = LibraryWindow._shown_library
        _in_any_library = LibraryWindow._in_any_library
        _in_view = LibraryWindow._in_view

        def __init__(self):
            self.settings = st

    w = W()
    names = lambda: sorted(os.path.basename(r.path) for r in w._in_view(recs))  # noqa: E731
    assert names() == ["1.mp4", "2.mp4"]           # All libraries: no Downloads
    st.library_shown = "Whisparr"
    assert names() == ["1.mp4"]
    st.library_shown = "Mine"
    assert names() == ["2.mp4"]
    st.library_shown = LibraryWindow.SHOW_OUTSIDE
    assert names() == ["3.mp4"]
