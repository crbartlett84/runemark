"""The Chapters tab: mark chapters on the paused frame, name them by tag or freely."""
from __future__ import annotations

from PySide6.QtCore import QStringListModel, Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCompleter, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMenu, QStyledItemDelegate, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..media.chapters import ChapterMark, add_mark, copy_marks, end_at, renumber
from ..media.probe import fmt_duration
from .widgets import Banner, text_button

COL_TIME, COL_NAME, COL_END = 0, 1, 2
CHAPTER_KEYS = [str(n) for n in range(1, 10)]


def _completer(model: QStringListModel, parent) -> QCompleter:
    """Suggestions in the model's own order (likeliest first), matching anywhere in the name."""
    completer = QCompleter(model, parent)
    completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    completer.setFilterMode(Qt.MatchFlag.MatchContains)
    completer.setModelSorting(QCompleter.ModelSorting.UnsortedModel)
    completer.setMaxVisibleItems(12)
    return completer


class _NameDelegate(QStyledItemDelegate):
    """Edits a chapter name with suggestions from the tag database."""

    def __init__(self, model: QStringListModel, parent=None):
        super().__init__(parent)
        self.model = model

    def createEditor(self, parent, option, index):  # noqa: N802
        editor = QLineEdit(parent)
        completer = _completer(self.model, editor)
        editor.setCompleter(completer)
        editor.setPlaceholderText("A tag, or any name")

        def picked(text: str):
            # One Enter (or click) on a suggestion finishes the rename, not just fills the box
            editor.setText(text)
            self.commitData.emit(editor)
            self.closeEditor.emit(editor)

        completer.activated.connect(picked)

        def show_likely():
            # the likeliest names (used before with this video's people or studio) straight away, before typing
            if not editor.text() or index.data() and str(index.data()).startswith("Chapter "):
                editor.selectAll()
                completer.setCompletionPrefix("")
                completer.complete()
        QTimer.singleShot(0, show_likely)
        return editor


class ChapterKeysDialog(QDialog):
    """Which name each number key (1-9) stands for: chapter keys, or (with other texts) tag keys."""

    CHAPTER_INTRO = ("Pause on a frame and press a number key to add a chapter there with that name, in one key. "
                     "Pick a tag (the file gets that tag too) or type any name. Leave a key empty to not use it.")

    def __init__(self, keys: dict, names: QStringListModel, parent=None, *, title: str = "Chapter keys",
                 prefix: str = "Key ", intro: str = CHAPTER_INTRO):
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        intro = QLabel(intro)
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.edits: dict[str, QLineEdit] = {}
        for key in CHAPTER_KEYS:
            edit = QLineEdit(keys.get(key, ""))
            edit.setPlaceholderText("(not used)")
            edit.setCompleter(_completer(names, edit))
            form.addRow(f"{prefix}{key}", edit)
            self.edits[key] = edit
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def keys(self) -> dict:
        out = {}
        for key, edit in self.edits.items():
            name = " ".join(edit.text().split()).replace(";", ",")
            if name:
                out[key] = name
        return out


class ChaptersPanel(QWidget):
    addRequested = Signal()                 # "Add chapter here": the window supplies the frame time
    seekRequested = Signal(float)
    changed = Signal()                      # the chapter list was edited
    nameTagged = Signal(str)                # a chapter was named: tag the file with that name
    keysChanged = Signal(dict)              # the chapter keys were set up: {"1": "name", ...}
    endRequested = Signal()                 # "End here": the window supplies the frame time (E)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.marks: list[ChapterMark] = []
        self._filling = False
        self._enabled = False
        self.names = QStringListModel(self)
        self.chapter_keys: dict = {}
        self.thumbs: dict[float, object] = {}       # round(start, 3) -> QIcon of the chapter's first frame

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 10, 8, 10)
        layout.setSpacing(10)
        self.banner = Banner()
        self.banner.hide()
        layout.addWidget(self.banner)
        intro = QLabel("Pause on the frame where a chapter starts and click <b>Add chapter</b> (or press M), or "
                       "press a number key to add a named one. New chapters are numbered automatically. "
                       "Double-click a name to change it: pick a tag (the file gets that tag too) or type any name. "
                       "<b>End here</b> (E) marks where the chapter you're in stops, when filler follows it; the "
                       "end is kept with the video's details, not added as a chapter.")
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
        self.end_button = text_button("End here", "pause")
        self.end_button.setToolTip("End the chapter you're in at the frame on screen (E). Moments stop here; the "
                                   "timeline and chapter list don't change.")
        self.end_button.clicked.connect(self.endRequested)
        self.clear_end_button = text_button("Clear end", "revert")
        self.clear_end_button.setToolTip("The chapter runs until the next one starts again")
        self.clear_end_button.clicked.connect(self._clear_end)
        self.keys_button = text_button("Keys…", "edit")
        self.keys_button.setToolTip("Choose the chapter name each number key (1-9) adds")
        self.keys_button.clicked.connect(self.edit_keys)
        for b in (self.add_button, self.end_button, self.rename_button, self.number_button, self.clear_end_button,
                  self.delete_button):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            buttons.addWidget(b)
        buttons.addStretch(1)
        self.keys_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        buttons.addWidget(self.keys_button)
        layout.addLayout(buttons)
        self.keys_line = QLabel("")
        self.keys_line.setObjectName("Faint")
        self.keys_line.setWordWrap(True)
        layout.addWidget(self.keys_line)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Starts at", "Name", "Ends at"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.horizontalHeader().setSectionResizeMode(COL_TIME, QHeaderView.ResizeMode.ResizeToContents)
        from PySide6.QtCore import QSize
        self.table.setIconSize(QSize(96, 54))
        self.table.verticalHeader().setDefaultSectionSize(60)
        self.table.horizontalHeader().setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(COL_END, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setItemDelegateForColumn(COL_NAME, _NameDelegate(self.names, self.table))
        self.table.itemChanged.connect(self._item_changed)
        self.table.cellClicked.connect(self._clicked)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.table, 1)

        self.note = QLabel("")
        self.note.setObjectName("Faint")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.set_enabled(False)

    # ---------------------------------------------------------------- setup
    def set_tag_names(self, names: list[str]):
        """Names offered when naming a chapter, likeliest first."""
        self.names.setStringList(names)

    def set_chapter_keys(self, keys: dict):
        self.chapter_keys = dict(keys or {})
        used = [f"{k} {self.chapter_keys[k]}" for k in CHAPTER_KEYS if self.chapter_keys.get(k)]
        self.keys_line.setText("Keys: " + " · ".join(used) if used else
                               "Tip: set up number keys that add a named chapter in one press (Keys…).")

    def edit_keys(self):
        dialog = ChapterKeysDialog(self.chapter_keys, self.names, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.set_chapter_keys(dialog.keys())
            self.keysChanged.emit(dict(self.chapter_keys))

    def _context_menu(self, pos):
        row = self.table.rowAt(pos.y())
        if not 0 <= row < len(self.marks) or self.marks[row].auto:
            return
        name = self.marks[row].name
        menu = QMenu(self)
        sub = menu.addMenu(f"Put “{name}” on a key")
        for key in CHAPTER_KEYS:
            now = self.chapter_keys.get(key)
            act = sub.addAction(f"Key {key}" + (f"  (now {now})" if now else ""))
            act.triggered.connect(lambda _=False, k=key: self._set_key(k, name))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _set_key(self, key: str, name: str):
        keys = {k: v for k, v in self.chapter_keys.items() if v.casefold() != name.casefold()}
        keys[key] = name
        self.set_chapter_keys(keys)
        self.keysChanged.emit(dict(self.chapter_keys))

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

    def missing_thumbs(self) -> list[float]:
        """Chapter starts that don't have a picture yet."""
        return [m.start for m in self.marks if round(m.start, 3) not in self.thumbs]

    def set_thumb(self, start: float, pixmap) -> None:
        from PySide6.QtGui import QIcon
        self.thumbs[round(start, 3)] = QIcon(pixmap)
        for row, mark in enumerate(self.marks):
            if abs(mark.start - start) < 0.0005 and self.table.item(row, COL_TIME) is not None:
                self.table.item(row, COL_TIME).setIcon(self.thumbs[round(start, 3)])

    def clear_thumbs(self):
        self.thumbs = {}

    def set_marks(self, marks: list[ChapterMark]):
        self.marks = copy_marks(marks)
        self._fill()

    def end_at(self, pts: float) -> ChapterMark | None:
        """End the chapter playing at pts there. None when no chapter has started yet."""
        mark = end_at(self.marks, pts)
        if mark is not None:
            self._fill(select=mark)
            self.changed.emit()
        return mark

    def _clear_end(self):
        chosen = [m for m in self._selected() if m.end is not None]
        for m in chosen:
            m.end = None
        if chosen:
            self._fill()
            self.changed.emit()

    # ---------------------------------------------------------------- editing
    def add_at(self, pts: float, name: str | None = None):
        """Start a chapter at pts, numbered, or named `name` (which also tags the file)."""
        self.marks, mark = add_mark(self.marks, pts)
        if name:
            match = next((n for n in self.names.stringList() if n.casefold() == name.casefold()), None)
            mark.name, mark.auto = (match or name), False
            self.marks = renumber(self.marks)
        self._fill(select=mark)
        self.changed.emit()
        if name:
            self.nameTagged.emit(mark.name)
        return mark

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

    def _clicked(self, row: int, column: int):
        if 0 <= row < len(self.marks):
            mark = self.marks[row]
            self.seekRequested.emit(mark.end if column == COL_END and mark.end is not None else mark.start)

    # ---------------------------------------------------------------- view
    def _fill(self, select: ChapterMark | None = None):
        self._filling = True
        self.table.setRowCount(len(self.marks))
        for row, mark in enumerate(self.marks):
            when = QTableWidgetItem(fmt_duration(mark.start, millis=True))
            icon = self.thumbs.get(round(mark.start, 3))
            if icon is not None:
                when.setIcon(icon)
            when.setFlags(when.flags() & ~Qt.ItemFlag.ItemIsEditable)
            when.setToolTip("Click to show this frame")
            name = QTableWidgetItem(mark.name)
            name.setToolTip("Numbered automatically" if mark.auto else "Double-click to rename")
            if mark.auto:
                font = name.font()
                font.setItalic(True)
                name.setFont(font)
            ends = QTableWidgetItem(fmt_duration(mark.end, millis=True) if mark.end is not None else "—")
            ends.setFlags(ends.flags() & ~Qt.ItemFlag.ItemIsEditable)
            ends.setToolTip("Click to show this frame" if mark.end is not None else
                            "Runs until the next chapter. End here (E) ends it sooner.")
            self.table.setItem(row, COL_TIME, when)
            self.table.setItem(row, COL_NAME, name)
            self.table.setItem(row, COL_END, ends)
            if select is not None and mark is select:
                self.table.selectRow(row)
        self._filling = False
        count = len(self.marks)
        ended = sum(1 for m in self.marks if m.end is not None)
        self.note.setText((f"{count} chapter{'s' if count != 1 else ''}. Each runs until the next one starts"
                           + (f", or its end ({ended} ended early)." if ended else "."))
                          if count else "No chapters yet.")
        self._update_buttons()

    def _update_buttons(self):
        chosen = self._selected() if self._enabled else []
        self.rename_button.setEnabled(len(chosen) == 1)
        self.number_button.setEnabled(any(not m.auto for m in chosen))
        self.delete_button.setEnabled(bool(chosen))
        self.clear_end_button.setEnabled(any(m.end is not None for m in chosen))
        self.end_button.setEnabled(self._enabled and bool(self.marks))
