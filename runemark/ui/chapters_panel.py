"""The Chapters tab: mark chapters on the paused frame, name them by tag or freely."""
from __future__ import annotations

from PySide6.QtCore import QStringListModel, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCompleter, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QStyledItemDelegate, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..media.chapters import ChapterMark, add_mark, renumber
from ..media.probe import fmt_duration
from .widgets import Banner, text_button

COL_TIME, COL_NAME = 0, 1


class _NameDelegate(QStyledItemDelegate):
    """Edits a chapter name with suggestions from the tag database."""

    def __init__(self, model: QStringListModel, parent=None):
        super().__init__(parent)
        self.model = model

    def createEditor(self, parent, option, index):  # noqa: N802
        editor = QLineEdit(parent)
        completer = QCompleter(self.model, editor)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        editor.setCompleter(completer)
        editor.setPlaceholderText("A tag, or any name")

        def picked(text: str):
            # One Enter (or click) on a suggestion finishes the rename, not just fills the box
            editor.setText(text)
            self.commitData.emit(editor)
            self.closeEditor.emit(editor)

        completer.activated.connect(picked)
        return editor


class ChaptersPanel(QWidget):
    addRequested = Signal()                 # "Add chapter here": the window supplies the frame time
    seekRequested = Signal(float)
    changed = Signal()                      # the chapter list was edited
    nameTagged = Signal(str)                # a chapter was named: tag the file with that name

    def __init__(self, parent=None):
        super().__init__(parent)
        self.marks: list[ChapterMark] = []
        self._filling = False
        self._enabled = False
        self.names = QStringListModel(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 10, 8, 10)
        layout.setSpacing(10)
        self.banner = Banner()
        self.banner.hide()
        layout.addWidget(self.banner)
        intro = QLabel("Pause on the frame where a chapter starts and click <b>Add chapter</b> (or press M). "
                       "New chapters are numbered automatically. Double-click a name to change it: pick a tag "
                       "(the file gets that tag too) or type any name.")
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        buttons = QHBoxLayout()
        self.add_button = text_button("Add chapter", "plus", primary=True)
        self.add_button.setToolTip("Start a chapter at the frame on screen (M)")
        self.add_button.clicked.connect(self.addRequested)
        self.rename_button = text_button("Rename", "edit")
        self.rename_button.clicked.connect(self._rename)
        self.number_button = text_button("Number", "revert")
        self.number_button.setToolTip("Go back to an automatic “Chapter N” name")
        self.number_button.clicked.connect(self._make_auto)
        self.delete_button = text_button("Delete", "trash")
        self.delete_button.clicked.connect(self._delete)
        for b in (self.add_button, self.rename_button, self.number_button, self.delete_button):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            buttons.addWidget(b)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Starts at", "Name"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.horizontalHeader().setSectionResizeMode(COL_TIME, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        self.table.setItemDelegateForColumn(COL_NAME, _NameDelegate(self.names, self.table))
        self.table.itemChanged.connect(self._item_changed)
        self.table.cellClicked.connect(self._clicked)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.table, 1)

        self.note = QLabel("")
        self.note.setObjectName("Faint")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.set_enabled(False)

    # ---------------------------------------------------------------- setup
    def set_tag_names(self, names: list[str]):
        self.names.setStringList(names)

    def set_enabled(self, enabled: bool, container: str | None = None):
        self._enabled = enabled
        self.add_button.setEnabled(enabled)
        self.table.setEnabled(enabled)
        if container == "mkv":
            self.banner.show_message("info", "These chapters go into the MP4 when this MKV is converted.")
        elif enabled:
            self.banner.show_message("info", "Saving changed chapters writes the whole file again (nothing is "
                                             "re-encoded), so it takes about as long as copying the file.")
        else:
            self.banner.hide()
        self._update_buttons()

    def set_marks(self, marks: list[ChapterMark]):
        self.marks = [ChapterMark(m.start, m.name, m.auto) for m in marks]
        self._fill()

    # ---------------------------------------------------------------- editing
    def add_at(self, pts: float):
        self.marks, mark = add_mark(self.marks, pts)
        self._fill(select=mark)
        self.changed.emit()

    def _selected(self) -> list[ChapterMark]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.marks[r] for r in rows if 0 <= r < len(self.marks)]

    def _rename(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if len(rows) == 1:
            self.table.editItem(self.table.item(rows[0], COL_NAME))

    def _make_auto(self):
        for mark in self._selected():
            mark.auto = True
        self.marks = renumber(self.marks)
        self._fill()
        self.changed.emit()

    def _delete(self):
        chosen = self._selected()
        if not chosen:
            return
        self.marks = renumber([m for m in self.marks if all(m is not c for c in chosen)])
        self._fill()
        self.changed.emit()

    def _item_changed(self, item: QTableWidgetItem):
        if self._filling or item.column() != COL_NAME:
            return
        row = item.row()
        if not 0 <= row < len(self.marks):
            return
        text = " ".join(item.text().split()).replace(";", ",")
        mark = self.marks[row]
        if not text:
            mark.auto = True
        else:
            match = next((n for n in self.names.stringList() if n.casefold() == text.casefold()), None)
            mark.name, mark.auto = (match or text), False
        self.marks = renumber(self.marks)
        self._fill(select=mark)
        self.changed.emit()
        if not mark.auto:
            self.nameTagged.emit(mark.name)

    def _clicked(self, row: int, _column: int):
        if 0 <= row < len(self.marks):
            self.seekRequested.emit(self.marks[row].start)

    # ---------------------------------------------------------------- view
    def _fill(self, select: ChapterMark | None = None):
        self._filling = True
        self.table.setRowCount(len(self.marks))
        for row, mark in enumerate(self.marks):
            when = QTableWidgetItem(fmt_duration(mark.start, millis=True))
            when.setFlags(when.flags() & ~Qt.ItemFlag.ItemIsEditable)
            when.setToolTip("Click to show this frame")
            name = QTableWidgetItem(mark.name)
            name.setToolTip("Numbered automatically" if mark.auto else "Double-click to rename")
            if mark.auto:
                font = name.font()
                font.setItalic(True)
                name.setFont(font)
            self.table.setItem(row, COL_TIME, when)
            self.table.setItem(row, COL_NAME, name)
            if select is not None and mark is select:
                self.table.selectRow(row)
        self._filling = False
        count = len(self.marks)
        self.note.setText(f"{count} chapter{'s' if count != 1 else ''}. Each runs until the next one starts."
                          if count else "No chapters yet.")
        self._update_buttons()

    def _update_buttons(self):
        chosen = self._selected() if self._enabled else []
        self.rename_button.setEnabled(len(chosen) == 1)
        self.number_button.setEnabled(any(not m.auto for m in chosen))
        self.delete_button.setEnabled(bool(chosen))
