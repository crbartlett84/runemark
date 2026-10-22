"""Right-hand panels: file details (with title, year, description and rating) and thumbnail."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QGuiApplication, QIntValidator, QPixmap
from PySide6.QtWidgets import (QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                               QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from ..media.mp4meta import VideoMeta
from ..media.probe import Section, sections_as_text
from . import theme
from .player import _strip_label
from .widgets import Card, ImageBox, text_button


class StarRating(QWidget):
    """Five clickable stars. Clicking the current rating again clears it."""
    changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 0
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        self.buttons = []
        for n in range(1, 6):
            button = QToolButton()
            button.setObjectName("Icon")
            button.setAutoRaise(True)
            button.setIconSize(QSize(20, 20))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(f"{n} star{'s' if n > 1 else ''} (click again to clear)")
            button.clicked.connect(lambda _=False, v=n: self._clicked(v))
            row.addWidget(button)
            self.buttons.append(button)
        self.label = QLabel("")
        self.label.setObjectName("Faint")
        row.addSpacing(6)
        row.addWidget(self.label)
        row.addStretch(1)
        self._paint()

    def value(self) -> int:
        return self._value

    def set_value(self, value: int):
        self._value = max(0, min(5, int(value or 0)))
        self._paint()

    def _clicked(self, n: int):
        self.set_value(0 if self._value == n else n)
        self.changed.emit(self._value)

    def _paint(self):
        pal = theme.current()
        for n, button in enumerate(self.buttons, start=1):
            filled = n <= self._value
            button.setIcon(theme.icon("star_fill" if filled else "star", pal.warning if filled else pal.faint, 20))
        self.label.setText("Not rated" if not self._value else f"{self._value} of 5")


class MetaEditor(Card):
    """Title, year, description and star rating."""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__("About this video", parent)
        form = QFormLayout()
        form.setVerticalSpacing(8)
        form.setHorizontalSpacing(12)
        self.title = QLineEdit()
        self.title.setPlaceholderText("Shown as Title in Explorer, Plex and Jellyfin")
        self.year = QLineEdit()
        self.year.setValidator(QIntValidator(1800, 2199, self.year))
        self.year.setMaximumWidth(80)
        self.year.setPlaceholderText("Year")
        self.rating = StarRating()
        self.description = QPlainTextEdit()
        self.description.setPlaceholderText("A short description (Explorer shows it as Comments)")
        self.description.setFixedHeight(84)
        form.addRow("Title", self.title)
        form.addRow("Year", self.year)
        form.addRow("Rating", self.rating)
        form.addRow("Description", self.description)
        self.body.addLayout(form)
        self.note = QLabel("")
        self.note.setObjectName("Faint")
        self.note.setWordWrap(True)
        self.body.addWidget(self.note)
        self._filling = False
        for widget in (self.title, self.year):
            widget.textChanged.connect(self._edited)
        self.description.textChanged.connect(self._edited)
        self.rating.changed.connect(self._edited)
        self.set_meta(None)

    def set_meta(self, meta: VideoMeta | None, container: str | None = None):
        self._filling = True
        meta = meta or VideoMeta()
        self.title.setText(meta.title)
        self.year.setText(str(meta.year) if meta.year else "")
        self.description.setPlainText(meta.description)
        self.rating.set_value(meta.rating)
        enabled = meta is not None and container is not None
        for widget in (self.title, self.year, self.description, self.rating):
            widget.setEnabled(enabled)
        self.note.setText("Saved into the MP4 when this MKV is converted." if container == "mkv" else "")
        self.note.setVisible(container == "mkv")
        self._filling = False

    def value(self) -> VideoMeta:
        text = self.year.text().strip()
        year = int(text) if text.isdigit() and len(text) == 4 else None
        return VideoMeta(" ".join(self.title.text().split()), year, self.description.toPlainText().strip(),
                         self.rating.value())

    def _edited(self, *_):
        if not self._filling:
            self.changed.emit()


class DetailsPanel(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        outer = QWidget()
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(2, 10, 8, 10)
        outer_layout.setSpacing(10)
        self.meta = MetaEditor()
        outer_layout.addWidget(self.meta)
        self._inner = QWidget()
        self._layout = QVBoxLayout(self._inner)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(10)
        outer_layout.addWidget(self._inner, 1)
        self.setWidget(outer)
        self._sections: list[Section] = []
        self.copy_button = text_button("Copy details", "copy")
        self.copy_button.clicked.connect(self._copy)
        self.show_sections([])

    def _clear(self):
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
            elif item.layout():
                while item.layout().count():
                    sub = item.layout().takeAt(0)
                    if sub.widget():
                        sub.widget().setParent(None)

    def show_sections(self, sections: list[Section]):
        self._sections = sections
        self._clear()
        if not sections:
            label = QLabel("Details about the open video appear here: length, resolution, "
                           "frame rate, audio and subtitle tracks, chapters.")
            label.setObjectName("Muted")
            label.setWordWrap(True)
            self._layout.addWidget(label)
            self._layout.addStretch(1)
            return
        for section in sections:
            card = Card(section.title)
            grid = QGridLayout()
            grid.setHorizontalSpacing(14)
            grid.setVerticalSpacing(6)
            grid.setColumnStretch(1, 1)
            for row, (label, value) in enumerate(section.rows):
                key = QLabel(label)
                key.setObjectName("SectionLabel")
                key.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
                val = QLabel(value)
                val.setObjectName("ValueLabel")
                val.setWordWrap(True)
                val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                grid.addWidget(key, row, 0)
                grid.addWidget(val, row, 1)
            card.body.addLayout(grid)
            self._layout.addWidget(card)
        bar = QHBoxLayout()
        bar.addStretch(1)
        bar.addWidget(self.copy_button)
        self._layout.addLayout(bar)
        self._layout.addStretch(1)

    def _copy(self):
        QGuiApplication.clipboard().setText(sections_as_text(self._sections))


class _SuggestionButton(QToolButton):
    doubleClicked = Signal()

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        self.doubleClicked.emit()
        super().mouseDoubleClickEvent(event)


class ThumbnailPanel(QScrollArea):
    captureRequested = Signal()
    chooseFileRequested = Signal()
    removeRequested = Signal()
    revertRequested = Signal()
    suggestRequested = Signal()
    suggestionPicked = Signal(float)       # show this frame in the player
    suggestionUse = Signal(float)          # make this frame the thumbnail

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(2, 10, 8, 10)
        layout.setSpacing(12)

        self.intro = QLabel()
        self.intro.setWordWrap(True)
        self.intro.setObjectName("Muted")
        layout.addWidget(self.intro)

        boxes = QHBoxLayout()
        boxes.setSpacing(10)
        current = Card("In the file now")
        self.current_box = ImageBox("No thumbnail yet. Explorer picks a frame itself.")
        current.body.addWidget(self.current_box)
        pending = Card("After you save")
        self.pending_box = ImageBox("Unchanged")
        pending.body.addWidget(self.pending_box)
        boxes.addWidget(current, 1)
        boxes.addWidget(pending, 1)
        layout.addLayout(boxes)
        self.pending_note = QLabel("")
        self.pending_note.setObjectName("Muted")
        self.pending_note.setWordWrap(True)
        layout.addWidget(self.pending_note)

        buttons = QHBoxLayout()
        self.capture_button = text_button("Use current frame", "camera", primary=True)
        self.capture_button.clicked.connect(self.captureRequested)
        self.file_button = text_button("Choose image…", "image")
        self.file_button.clicked.connect(self.chooseFileRequested)
        buttons.addWidget(self.capture_button)
        buttons.addWidget(self.file_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        buttons2 = QHBoxLayout()
        self.remove_button = text_button("Remove thumbnail", "trash")
        self.remove_button.clicked.connect(self.removeRequested)
        self.revert_button = text_button("Undo change", "revert")
        self.revert_button.clicked.connect(self.revertRequested)
        buttons2.addWidget(self.remove_button)
        buttons2.addWidget(self.revert_button)
        buttons2.addStretch(1)
        layout.addLayout(buttons2)

        suggest = Card("Suggested frames")
        head = QHBoxLayout()
        self.suggest_status = QLabel("Runemark can look through the video for sharp, well-lit frames.")
        self.suggest_status.setObjectName("Muted")
        self.suggest_status.setWordWrap(True)
        self.suggest_button = text_button("Find good frames", "sparkle")
        self.suggest_button.clicked.connect(self.suggestRequested)
        head.addWidget(self.suggest_status, 1)
        head.addWidget(self.suggest_button, 0, Qt.AlignmentFlag.AlignTop)
        suggest.body.addLayout(head)
        self.suggest_grid = QGridLayout()
        self.suggest_grid.setSpacing(8)
        suggest.body.addLayout(self.suggest_grid)
        hint = QLabel("Click a frame to show it in the player; double-click to make it the thumbnail.")
        hint.setObjectName("Faint")
        hint.setWordWrap(True)
        suggest.body.addWidget(hint)
        self.suggest_hint = hint
        hint.hide()
        layout.addWidget(suggest)
        self._suggest_buttons: list[QToolButton] = []
        layout.addStretch(1)
        self.setWidget(inner)
        self.set_container(None)
        self.set_state(None, None, "")

    def set_container(self, container: str | None):
        if container == "mkv":
            self.intro.setText("The thumbnail is stored as the file's cover image. This MKV will be converted to "
                               "MP4 when you save, and the new MP4 gets the thumbnail you choose here.")
        else:
            self.intro.setText("The thumbnail is stored inside the file as its cover image. Windows Explorer "
                               "shows it as the file's thumbnail.")
        enabled = container is not None
        for button in (self.capture_button, self.file_button, self.remove_button, self.suggest_button):
            button.setEnabled(enabled)

    def set_state(self, current: QPixmap | None, pending: QPixmap | None, note: str, *,
                  changed: bool = False, removing: bool = False):
        self.current_box.set_pixmap(current)
        if removing:
            self.pending_box.set_pixmap(None)
            self.pending_box.set_placeholder("No thumbnail (it will be removed)")
        elif changed:
            self.pending_box.set_pixmap(pending)
        else:
            self.pending_box.set_pixmap(None)
            self.pending_box.set_placeholder("Unchanged")
        self.pending_note.setText(note)
        self.revert_button.setEnabled(changed or removing)
        self.remove_button.setEnabled(current is not None and not removing)
        pal = theme.current()
        self.pending_note.setStyleSheet(f"color: {pal.accent};" if (changed or removing) else "")

    # ---------------------------------------------------------------- suggestions
    def clear_suggestions(self, text: str = "Runemark can look through the video for sharp, well-lit frames."):
        for button in self._suggest_buttons:
            button.setParent(None)
            button.deleteLater()
        self._suggest_buttons = []
        self.suggest_status.setText(text)
        self.suggest_button.setText("Find good frames")
        self.suggest_hint.hide()

    def set_suggest_busy(self, text: str):
        self.suggest_status.setText(text)
        self.suggest_button.setEnabled(False)

    def set_suggestions(self, items: list[tuple[float, QPixmap]], start_time: float = 0.0,
                        duration: float = 0.0):
        self.clear_suggestions()
        self.suggest_button.setEnabled(True)
        self.suggest_button.setText("Look again")
        if not items:
            self.suggest_status.setText("No clear frames found. Step through the video and use the current frame.")
            return
        self.suggest_status.setText(f"{len(items)} frames worth a look:")
        width = 150
        for n, (pts, pixmap) in enumerate(items):
            button = _SuggestionButton()
            button.setObjectName("FrameThumb")
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setIcon(pixmap)
            button.setIconSize(QSize(width, int(width * 9 / 16)))
            button.setText(_strip_label(max(0.0, pts - min(0.0, start_time)), duration))
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip("Click to show this frame. Double-click to make it the thumbnail.")
            button.clicked.connect(lambda _=False, t=pts: self.suggestionPicked.emit(t))
            button.doubleClicked.connect(lambda t=pts: self.suggestionUse.emit(t))
            self.suggest_grid.addWidget(button, n // 2, n % 2)
            self._suggest_buttons.append(button)
        self.suggest_hint.show()
