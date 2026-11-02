"""The Activity window: what Runemark changed, when, newest first. Double-click a file to open it."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout)

from ..tagdb import TagDB


def group(rows: list[dict]) -> list[dict]:
    """Consecutive rows of one change together: the same batch (a bulk change, a rename), or the same label
    within the same minute. Rows come newest first."""
    out: list[dict] = []
    for r in rows:
        key = ("batch", r["batch"]) if r["batch"] else ("label", r["label"], r["at"][:16])
        if out and out[-1]["key"] == key:
            out[-1]["paths"].append(r["path"])
        else:
            out.append({"key": key, "at": r["at"], "label": r["label"], "batch": r["batch"], "paths": [r["path"]]})
    return out


class ActivityDialog(QDialog):
    openRequested = Signal(list)

    def __init__(self, db: TagDB, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Activity — Runemark")
        self.resize(900, 560)
        layout = QVBoxLayout(self)
        intro = QLabel("What Runemark changed, newest first: saves, bulk changes, undos, renames and duplicates sent "
                       "to the Recycle Bin. Double-click a file to open it.")
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search files and changes")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._fill)
        layout.addWidget(self.search)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["When", "What", "File"])
        self.tree.setColumnWidth(0, 150)
        self.tree.setColumnWidth(1, 340)
        self.tree.itemDoubleClicked.connect(self._open)
        layout.addWidget(self.tree, 1)
        bottom = QHBoxLayout()
        self.count = QLabel("")
        self.count.setObjectName("Faint")
        bottom.addWidget(self.count, 1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        layout.addLayout(bottom)
        self.groups = group(db.activity())
        self._fill()

    def _fill(self, *_):
        needle = self.search.text().strip().casefold()
        self.tree.clear()
        shown = 0
        for g in self.groups:
            paths = [p for p in g["paths"] if not needle or needle in p.casefold() or needle in g["label"].casefold()]
            if not paths:
                continue
            shown += 1
            when = g["at"].replace("T", " ")[:16]
            if len(paths) == 1:
                item = QTreeWidgetItem([when, g["label"], Path(paths[0]).name])
                item.setData(0, Qt.ItemDataRole.UserRole, paths[0])
                item.setToolTip(2, paths[0])
            else:
                item = QTreeWidgetItem([when, g["label"], f"{len(paths)} files"])
                for p in paths:
                    child = QTreeWidgetItem(["", "", Path(p).name])
                    child.setData(0, Qt.ItemDataRole.UserRole, p)
                    child.setToolTip(2, p)
                    item.addChild(child)
            self.tree.addTopLevelItem(item)
        self.count.setText(f"{shown} change(s)")

    def _open(self, item, _col):
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if path and Path(path).exists():
            self.openRequested.emit([Path(path)])
