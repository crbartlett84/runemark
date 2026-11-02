"""Library → More → Health check: what has drifted, with fixes."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from .. import health as H
from ..tagdb import TagDB
from . import workers
from .dialogs import ProgressDialog, TextReportDialog


class HealthDialog(QDialog):
    openRequested = Signal(list)
    changed = Signal()

    def __init__(self, db: TagDB, records: list, settings, host=None, parent=None):
        super().__init__(parent)
        self.db, self.records, self.settings, self.host = db, list(records), settings, host
        self.issues: list[H.Issue] = []
        self.setWindowTitle("Health check — Runemark")
        self.resize(1000, 600)
        layout = QVBoxLayout(self)
        intro = QLabel("Videos that have drifted or were left half done. Checking only reads the files. Select rows "
                       "and use Fix to repair them; fixes are saved like any change and can be undone with Library → "
                       "More → Undo last bulk change. Double-click a row to open the video.")
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Video", "Problem", "Details", "Fix"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemDoubleClicked.connect(self._open)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.summary = QLabel("")
        self.summary.setObjectName("Faint")
        row.addWidget(self.summary, 1)
        self.recheck = QPushButton("Check again")
        self.recheck.clicked.connect(self.run_check)
        self.fix_selected = QPushButton("Fix selected")
        self.fix_selected.clicked.connect(lambda: self._fix(selected_only=True))
        self.fix_all = QPushButton("Fix everything fixable")
        self.fix_all.clicked.connect(lambda: self._fix(selected_only=False))
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        for b in (self.recheck, self.fix_selected, self.fix_all, close):
            row.addWidget(b)
        layout.addLayout(row)

    def run_check(self):
        dialog = ProgressDialog("Checking the library", cancellable=True, parent=self)
        db_path, records = self.db.path, self.records

        def work(job):
            db = TagDB(db_path)
            try:
                return H.check(db, records, progress=job.report, cancel=job.cancel)
            finally:
                db.close()

        def done(issues):
            dialog.finish()
            self.issues = issues
            self._fill()

        def failed(exc, _tb):
            dialog.finish()
            QMessageBox.warning(self, "Health check", str(exc))

        job = workers.start(work, on_done=done, on_error=failed, on_progress=dialog.update_progress)
        dialog.cancelRequested.connect(job.cancel.set)
        dialog.show()

    def _fill(self):
        self.table.setRowCount(len(self.issues))
        for r, issue in enumerate(self.issues):
            cells = [Path(issue.path).name, H.KIND_TEXT[issue.kind], issue.detail, H.FIX_TEXT.get(issue.kind, "—")]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(issue.path if c == 0 else text)
                item.setData(Qt.ItemDataRole.UserRole, r)
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()
        fixable = sum(1 for i in self.issues if i.fixable)
        self.summary.setText(f"{len(self.issues)} thing(s) found in {len(self.records)} video(s); {fixable} can be "
                             "fixed here" if self.issues else f"All {len(self.records)} video(s) look healthy.")
        self.fix_all.setEnabled(fixable > 0)
        self.fix_selected.setEnabled(fixable > 0)

    def _open(self, item):
        issue = self.issues[item.data(Qt.ItemDataRole.UserRole)]
        if Path(issue.path).exists():
            self.openRequested.emit([Path(issue.path)])

    def _fix(self, selected_only: bool):
        if selected_only:
            rows = sorted({i.row() for i in self.table.selectedIndexes()})
            chosen = [self.issues[r] for r in rows]
        else:
            chosen = list(self.issues)
        chosen = [i for i in chosen if i.fixable]
        if not chosen:
            QMessageBox.information(self, "Health check", "Nothing selected can be fixed here.")
            return
        paths = sorted({Path(i.path) for i in chosen if i.kind != H.MISSING})
        if QMessageBox.question(self, "Health check", f"Fix {len(chosen)} thing(s) in {len(paths)} video(s)?") \
                != QMessageBox.StandardButton.Yes:
            return
        if self.host is not None:
            self.host.release_for(paths, lambda: self._start_fix(chosen, paths))
        else:
            self._start_fix(chosen, paths)

    def _start_fix(self, chosen, paths):
        dialog = ProgressDialog("Fixing", cancellable=True, parent=self)
        db_path = self.db.path
        mode, preserve = self.settings.explorer_writer, self.settings.preserve_modified_date

        def work(job):
            db = TagDB(db_path)
            try:
                return H.fix(db, chosen, explorer_mode=mode, preserve_mtime=preserve, progress=job.report,
                             cancel=job.cancel)
            finally:
                db.close()

        def done(problems):
            dialog.finish()
            self.changed.emit()
            if self.host is not None:
                self.host.files_changed(paths)
            if problems:
                TextReportDialog("Some things weren't fixed",
                                 "\n".join(f"{p.name}: {why}" for p, why in problems), self).exec()
            fixed = {(i.path, i.kind) for i in chosen}
            self.issues = [i for i in self.issues if (i.path, i.kind) not in fixed]
            self._fill()

        def failed(exc, _tb):
            dialog.finish()
            QMessageBox.warning(self, "Health check", str(exc))

        job = workers.start(work, on_done=done, on_error=failed, on_progress=dialog.update_progress)
        dialog.cancelRequested.connect(job.cancel.set)
        dialog.show()
