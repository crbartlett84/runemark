"""The command palette (Ctrl+K): type to find any action, tag, person or video, and Enter to use it."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout

MAX_SHOWN = 60


@dataclass
class Entry:
    kind: str                  # "Action", "Tag", "Person", "Video"
    text: str
    detail: str
    run: Callable[[], None]
    keywords: str = ""

    def haystack(self) -> str:
        return f"{self.text} {self.keywords}".casefold()


def rank(entries: list[Entry], query: str) -> list[Entry]:
    """Entries matching every word of the query; names starting with it first, then actions, tags, people, videos."""
    words = query.casefold().split()
    if not words:
        return [e for e in entries if e.kind == "Action"]
    order = {"Action": 0, "Tag": 1, "Person": 2, "Video": 3}
    hits = [e for e in entries if all(w in e.haystack() for w in words)]
    q = query.strip().casefold()
    return sorted(hits, key=lambda e: (not e.text.casefold().startswith(q), order.get(e.kind, 9), e.text.casefold()))


class CommandPalette(QDialog):
    def __init__(self, entries: list[Entry], parent=None):
        super().__init__(parent)
        self.entries = entries
        self.setWindowTitle("Go to")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.resize(620, 440)
        layout = QVBoxLayout(self)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Type an action, tag, person or video…")
        self.input.textChanged.connect(self._fill)
        self.input.installEventFilter(self)
        layout.addWidget(self.input)
        self.list = QListWidget()
        self.list.itemActivated.connect(self._run)
        layout.addWidget(self.list, 1)
        hint = QLabel("Enter runs it · ↑↓ choose · Esc closes. Tags and people go on the open video.")
        hint.setObjectName("Faint")
        layout.addWidget(hint)
        self._fill("")

    def _fill(self, text: str):
        self.list.clear()
        for entry in rank(self.entries, text)[:MAX_SHOWN]:
            item = QListWidgetItem(f"{entry.text}    ·  {entry.kind}" + (f" — {entry.detail}" if entry.detail else ""))
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)

    def eventFilter(self, obj, event):  # noqa: N802
        from PySide6.QtCore import QEvent
        if obj is self.input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                row = self.list.currentRow() + (1 if key == Qt.Key.Key_Down else -1)
                self.list.setCurrentRow(max(0, min(self.list.count() - 1, row)))
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._run(self.list.currentItem())
                return True
        return super().eventFilter(obj, event)

    def _run(self, item):
        if item is None:
            return
        entry: Entry = item.data(Qt.ItemDataRole.UserRole)
        self.accept()
        entry.run()
