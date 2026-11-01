"""Duplicates: groups of videos that are copies of each other (re-encoded, resized, trimmed or with scenes cut),
with the best-quality one suggested as the one to keep. Nothing is deleted without being asked, and deleted copies
go to the Recycle Bin (never deleted outright: where there's no Recycle Bin, the file is kept)."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
                               QPushButton, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from .. import duplicates as D
from .. import explorer
from ..media.probe import fmt_duration, fmt_size
from ..tagdb import TagDB
from . import workers
from .dialogs import ProgressDialog, TextReportDialog

ROLE_PATH = Qt.ItemDataRole.UserRole
ROLE_GROUP = Qt.ItemDataRole.UserRole + 1
COPY_COLUMNS = ["Video", "Compared with the one to keep", "Length", "Resolution", "Bitrate", "Size", "Tags",
                "Folder"]
SHARED_COLUMNS = ["Video", "Shares a section with", "Shared", "Where (in the first video)"]


def _button(text: str, slot) -> QPushButton:
    b = QPushButton(text)
    b.setAutoDefault(False)
    b.clicked.connect(slot)
    return b


class DuplicatesWindow(QDialog):
    openRequested = Signal(list)
    changed = Signal()                     # files were recycled or merged: the Library should refresh

    def __init__(self, db: TagDB, settings, host=None, parent=None):
        super().__init__(parent)
        self.db, self.settings, self.host = db, settings, host
        self.found: list[D.Found] = []
        self.groups: list[dict] = []          # {"copies": [Copy], "keep": path}
        self._job = None
        self.setWindowTitle("Duplicates")
        self.resize(1180, 640)
        layout = QVBoxLayout(self)
        intro = QLabel("Finds copies of the same video even when they were re-encoded, resized, given black bars, "
                       "trimmed or had scenes cut out. Each video is read once (about 3 minutes per hour of video "
                       "the first time); after that only new or changed videos are read.")
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        top = QHBoxLayout()
        self.find_button = _button("Find duplicates", self.find)
        self.shared_box = QCheckBox("Also list videos that only share a section")
        self.shared_box.setToolTip("Two different videos with the same intro, or two clips of the same moment")
        self.shared_box.toggled.connect(self._shared_toggled)
        self.status = QLabel("")
        self.status.setObjectName("Faint")
        top.addWidget(self.find_button)
        top.addWidget(self.shared_box)
        top.addWidget(self.status, 1)
        layout.addLayout(top)

        self.tabs = QTabWidget()
        self.copies = self._tree(COPY_COLUMNS)
        self.copies.itemChanged.connect(lambda *_: self._update_buttons())
        self.copies.currentItemChanged.connect(lambda *_: self._update_buttons())
        self.copies.itemDoubleClicked.connect(lambda item, _c: self._open([item.data(0, ROLE_PATH)]))
        self.shared = self._tree(SHARED_COLUMNS)
        self.tabs.addTab(self.copies, "Copies")
        self.tabs.addTab(self.shared, "Shares a section")
        self.tabs.setTabEnabled(1, False)
        layout.addWidget(self.tabs, 1)

        bottom = QHBoxLayout()
        self.keep_button = _button("Keep this one", self._keep_selected)
        self.tick_button = _button("Tick all but the one to keep", self._tick_others)
        self.merge_button = _button("Copy tags into the one to keep", self._merge)
        self.merge_button.setToolTip("Adds the other copies' tags and people to the kept one, and fills in its "
                                     "title, year, description and rating where it has none. Nothing is removed.")
        self.recycle_button = _button("Send ticked to the Recycle Bin…", self._recycle)
        self.open_button = _button("Open", lambda: self._open(self._selected_paths()))
        self.reveal_button = _button("Show in File Explorer", self._reveal)
        for b in (self.keep_button, self.tick_button, self.merge_button, self.recycle_button):
            bottom.addWidget(b)
        bottom.addStretch(1)
        bottom.addWidget(self.open_button)
        bottom.addWidget(self.reveal_button)
        bottom.addWidget(_button("Close", self.reject))
        layout.addLayout(bottom)
        self._update_buttons()

    @staticmethod
    def _tree(columns: list[str]) -> QTreeWidget:
        tree = QTreeWidget()
        tree.setColumnCount(len(columns))
        tree.setHeaderLabels(columns)
        tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        tree.setUniformRowHeights(True)
        tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        tree.header().setStretchLastSection(True)
        return tree

    # ---------------------------------------------------------------- finding
    def find(self):
        if self._job is not None:
            return
        dialog = ProgressDialog("Finding duplicates", cancellable=True, parent=self)
        include_shared = self.shared_box.isChecked()
        db_path = self.db.path

        def work(job):
            db = TagDB(db_path)                       # SQLite connections belong to one thread
            try:
                result = D.scan_library(db, include_shared=include_shared, progress=job.report, cancel=job.cancel)
                if result.stopped:
                    return result, []
                job.report(95, "Reading video details…")
                records = {r.path: r for r in db.library()}
                groups = []
                for members in D.groups(result.found):
                    copies = [D.describe_copy(p, records.get(p)) for p in members]
                    groups.append({"copies": copies, "keep": D.best_quality(copies).path})
                return result, groups
            finally:
                db.close()

        def done(out):
            self._job = None
            dialog.finish()
            result, groups = out
            self.found, self.groups = result.found, groups
            self._fill()
            bits = [f"{len(groups)} group(s) of copies"]
            if include_shared:
                bits.append(f"{sum(1 for f in self.found if f.match.kind == D.SHARED)} shared section(s)")
            if result.read:
                bits.append(f"{result.read} video(s) read")
            if result.stopped:
                bits = ["Stopped: what was read is kept, and the next search carries on from there"]
            self.status.setText(" · ".join(bits))
            if result.problems:
                TextReportDialog("Some videos couldn't be read", "\n".join(result.problems), self).exec()

        def failed(exc, _tb):
            self._job = None
            dialog.finish()
            QMessageBox.critical(self, "Duplicates", str(exc))

        self._job = workers.start(work, on_done=done, on_error=failed, on_progress=dialog.update_progress)
        dialog.cancelRequested.connect(self._job.cancel.set)
        dialog.show()

    def _shared_toggled(self, on: bool):
        self.tabs.setTabEnabled(1, on and self.shared.topLevelItemCount() > 0)
        if on and self.found and not any(f.match.kind == D.SHARED for f in self.found):
            self.status.setText("Search again to list shared sections")

    # ---------------------------------------------------------------- showing results
    def _fill(self):
        self.copies.blockSignals(True)
        self.copies.clear()
        bold = QFont(self.copies.font())
        bold.setWeight(QFont.Weight.DemiBold)
        for n, group in enumerate(self.groups):
            head = QTreeWidgetItem([f"{len(group['copies'])} copies", "", "", "", "", "", "", ""])
            head.setFont(0, bold)
            head.setData(0, ROLE_GROUP, n)
            head.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.copies.addTopLevelItem(head)
            keep = group["keep"]
            ordered = sorted(group["copies"], key=lambda c: c.path != keep)
            for c in ordered:
                is_keep = c.path == keep
                what = "Keep (best quality)" if is_keep else D.relation(keep, c.path, self.found)
                row = QTreeWidgetItem([
                    Path(c.path).name, what, fmt_duration(c.duration) if c.duration else "",
                    f"{c.width}×{c.height}" if c.pixels else "", f"{c.bit_rate // 1000:,} kb/s" if c.bit_rate else "",
                    fmt_size(c.size), str(c.tags + c.people) if (c.tags or c.people) else "", str(Path(c.path).parent)])
                row.setData(0, ROLE_PATH, c.path)
                row.setData(0, ROLE_GROUP, n)
                row.setToolTip(0, c.path)
                row.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
                row.setCheckState(0, Qt.CheckState.Unchecked)
                if is_keep:
                    row.setFont(0, bold)
                    row.setFont(1, bold)
                head.addChild(row)
        self.copies.expandAll()
        for col in range(len(COPY_COLUMNS) - 1):
            self.copies.resizeColumnToContents(col)
        self.copies.blockSignals(False)
        if not self.groups:
            empty = QTreeWidgetItem(["No copies found."])
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.copies.addTopLevelItem(empty)

        self.shared.clear()
        for f in self.found:
            if f.match.kind != D.SHARED:
                continue
            m = f.match
            shared_s = sum(r.length for r in m.runs) / m.fps
            where = ", ".join(f"{D._fmt(r.a_start / m.fps)}–{D._fmt((r.a_start + r.length) / m.fps)}" for r in m.runs)
            item = QTreeWidgetItem([Path(f.a).name, Path(f.b).name, D._fmt(shared_s), where])
            item.setData(0, ROLE_PATH, f.a)
            item.setToolTip(0, f.a)
            item.setToolTip(1, f.b)
            self.shared.addTopLevelItem(item)
        for col in range(len(SHARED_COLUMNS) - 1):
            self.shared.resizeColumnToContents(col)
        self.tabs.setTabText(1, f"Shares a section ({self.shared.topLevelItemCount()})")
        self.tabs.setTabEnabled(1, self.shared_box.isChecked() and self.shared.topLevelItemCount() > 0)
        self._update_buttons()

    def _rows(self, group: int | None = None):
        for i in range(self.copies.topLevelItemCount()):
            head = self.copies.topLevelItem(i)
            if head.data(0, ROLE_GROUP) is None or (group is not None and head.data(0, ROLE_GROUP) != group):
                continue
            for k in range(head.childCount()):
                yield head.child(k)

    def _ticked(self) -> list[str]:
        return [r.data(0, ROLE_PATH) for r in self._rows() if r.checkState(0) == Qt.CheckState.Checked]

    def _current_row(self):
        item = self.copies.currentItem()
        return item if item is not None and item.data(0, ROLE_PATH) else None

    def _selected_paths(self) -> list[str]:
        tree = self.copies if self.tabs.currentIndex() == 0 else self.shared
        return [i.data(0, ROLE_PATH) for i in tree.selectedItems() if i.data(0, ROLE_PATH)]

    def _update_buttons(self):
        row = self._current_row()
        self.keep_button.setEnabled(row is not None and row.data(0, ROLE_PATH) != self._keep_of(row))
        self.tick_button.setEnabled(row is not None)
        self.merge_button.setEnabled(row is not None)
        self.recycle_button.setEnabled(bool(self._ticked()))
        has = bool(self._selected_paths())
        self.open_button.setEnabled(has)
        self.reveal_button.setEnabled(has)

    def _keep_of(self, row) -> str | None:
        n = row.data(0, ROLE_GROUP) if row is not None else None
        return self.groups[n]["keep"] if n is not None and n < len(self.groups) else None

    # ---------------------------------------------------------------- actions
    def _keep_selected(self):
        row = self._current_row()
        if row is None:
            return
        n = row.data(0, ROLE_GROUP)
        self.groups[n]["keep"] = row.data(0, ROLE_PATH)
        self._fill()

    def _tick_others(self):
        row = self._current_row()
        if row is None:
            return
        n = row.data(0, ROLE_GROUP)
        keep = self.groups[n]["keep"]
        for r in self._rows(n):
            r.setCheckState(0, Qt.CheckState.Unchecked if r.data(0, ROLE_PATH) == keep else Qt.CheckState.Checked)

    def _merge(self):
        row = self._current_row()
        if row is None:
            return
        n = row.data(0, ROLE_GROUP)
        keep = self.groups[n]["keep"]
        others = [c.path for c in self.groups[n]["copies"] if c.path != keep]
        if not keep.lower().endswith((".mp4", ".m4v")):
            QMessageBox.information(self, "Copy tags", "The one to keep is an MKV. Open it and save it to convert "
                                    "it to MP4 first, or keep a different copy.")
            return
        answer = QMessageBox.question(self, "Copy tags", f"Add the tags and people from the {len(others)} other "
                                      f"cop{'ies' if len(others) != 1 else 'y'} to {Path(keep).name}, and fill in its "
                                      "title, year, description and rating where it has none?\n\nNothing is removed, "
                                      "and it can be undone (More → Undo last save… with the file open).")
        if answer != QMessageBox.StandardButton.Yes:
            return
        db_path, mode = self.db.path, self.settings.explorer_writer
        preserve = self.settings.preserve_modified_date

        def work(job):
            db = TagDB(db_path)
            try:
                return D.merge_into(db, keep, others, explorer_mode=mode, preserve_mtime=preserve)
            finally:
                db.close()

        def done(problems):
            if self.host is not None:
                self.host.files_changed([Path(keep)])
            self.changed.emit()
            self.status.setText(f"Copied tags into {Path(keep).name}")
            if problems:
                QMessageBox.warning(self, "Copy tags", "Saved, with warnings:\n\n• " + "\n• ".join(problems))

        def failed(exc, _tb):
            QMessageBox.critical(self, "Copy tags", str(exc))

        run = lambda: workers.start(work, on_done=done, on_error=failed)  # noqa: E731
        if self.host is not None:
            self.host.release_for([Path(keep)], run)
        else:
            run()

    def _recycle(self):
        paths = self._ticked()
        if not paths:
            return
        keeps = {g["keep"] for g in self.groups}
        whole = [n for n, g in enumerate(self.groups) if all(c.path in paths for c in g["copies"])]
        text = f"Send these {len(paths)} video(s) to the Recycle Bin?\n\n" + "\n".join(
            f"• {Path(p).name}" for p in paths[:15]) + (f"\n…and {len(paths) - 15} more" if len(paths) > 15 else "")
        if whole:
            text += "\n\nWarning: every copy in a group is ticked, so none of that video would be left."
        elif any(p in keeps for p in paths):
            text += "\n\nOne of them is the copy suggested to keep."
        text += ("\n\nVideos on drives without a Recycle Bin (network shares, USB drives) are kept, not deleted. "
                 "Consider Copy tags into the one to keep first.")
        if QMessageBox.question(self, "Recycle Bin", text) != QMessageBox.StandardButton.Yes:
            return
        if self.host is not None:
            self.host.release_for([Path(p) for p in paths], lambda: self._do_recycle(paths))
        else:
            self._do_recycle(paths)

    def _do_recycle(self, paths: list[str]):
        gone, kept = [], []
        for p in paths:
            try:
                explorer.recycle(p)
            except explorer.NoRecycleBin:
                kept.append(f"{Path(p).name}: no Recycle Bin on that drive, so it was kept")
                continue
            except Exception as exc:  # noqa: BLE001
                kept.append(f"{Path(p).name}: {exc}")
                continue
            gone.append(p)
            self.db.forget_file(p)
        for g in self.groups:
            g["copies"] = [c for c in g["copies"] if c.path not in gone]
            if g["keep"] in gone and g["copies"]:
                g["keep"] = D.best_quality(g["copies"]).path
        self.groups = [g for g in self.groups if len(g["copies"]) > 1]
        self.found = [f for f in self.found if f.a not in gone and f.b not in gone]
        self._fill()
        self.changed.emit()
        self.status.setText(f"Sent {len(gone)} video(s) to the Recycle Bin")
        if kept:
            TextReportDialog("Some videos were kept", "\n".join(kept), self).exec()

    def _open(self, paths):
        paths = [Path(p) for p in paths if p and Path(p).exists()]
        if paths:
            self.openRequested.emit(paths)

    def _reveal(self):
        paths = self._selected_paths()
        if paths:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(paths[0]).parent)))
