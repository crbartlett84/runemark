"""File names in both directions: read details from names that follow a pattern, or rename (and sort into folders)
from details. A live preview shows exactly what will happen before anything changes."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from .. import names as N
from ..tagdb import TagDB
from . import theme, workers
from .dialogs import ProgressDialog, TextReportDialog

READ_PRESETS = ["{People} - {Title} ({Year})", "{Title} ({Year})", "{Title} - {People}", "{Year} - {Title}"]
RENAME_PRESETS = ["{Title} ({Year})", "{People} - {Title} ({Year})", "{Year}\\{Title}", "{People}\\{Title} ({Year})"]


def _button(text: str, slot) -> QPushButton:
    b = QPushButton(text)
    b.setAutoDefault(False)
    b.clicked.connect(slot)
    return b


class NamesDialog(QDialog):
    def __init__(self, db: TagDB, records: list, mode: str, settings=None, host=None, parent=None):
        super().__init__(parent)
        assert mode in ("read", "rename")
        self.db, self.records, self.mode, self.settings, self.host = db, list(records), mode, settings, host
        self.plans = []
        self.applied = False
        self.moved: list[tuple[str, str]] = []      # (old, new) of what was renamed
        self.setWindowTitle("Read details from file names" if mode == "read" else "Rename from details")
        self.resize(1100, 620)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Fill in title, year and people from file names that follow a pattern. Fields: "
            + ", ".join("{" + f + "}" for f in N.FIELDS if f != "Name") + ". People can be separated by commas, "
            "“&” or “and”." if mode == "read" else
            "Rename the videos from their details. Fields: " + ", ".join("{" + f + "}" for f in N.FIELDS)
            + ". Use \\ to sort into folders. Subtitles and .nfo files with the same name move too, and the "
            "rename can be undone (Library → More → Undo last rename).")
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        row = QHBoxLayout()
        row.addWidget(QLabel("Pattern"))
        self.pattern = QComboBox()
        self.pattern.setEditable(True)
        self.pattern.addItems(READ_PRESETS if mode == "read" else RENAME_PRESETS)
        last = getattr(settings, "name_patterns", {}).get(mode) if settings is not None else None
        if last:
            self.pattern.setCurrentText(last)
        self.pattern.currentTextChanged.connect(lambda *_: self._timer.start())
        row.addWidget(self.pattern, 1)
        for f in N.FIELDS:
            if mode == "read" and f == "Name":
                continue
            b = QPushButton("{" + f + "}")
            b.setAutoDefault(False)
            b.clicked.connect(lambda _=False, f=f: self._insert(f))
            row.addWidget(b)
        layout.addLayout(row)

        opts = QHBoxLayout()
        if mode == "read":
            self.overwrite = QCheckBox("Replace title, year and rating that are already filled in")
            self.create_people = QCheckBox("Add people who aren't in your People list yet")
            self.create_people.setChecked(True)
            opts.addWidget(self.overwrite)
            opts.addWidget(self.create_people)
            self.overwrite.toggled.connect(lambda *_: self._refresh())
        else:
            self.use_base = QCheckBox("Move into one folder:")
            self.base = QLineEdit()
            self.base.setPlaceholderText("(each video stays in its own folder)")
            self.base.setEnabled(False)
            self.use_base.toggled.connect(self.base.setEnabled)
            self.use_base.toggled.connect(lambda *_: self._timer.start())
            self.base.textChanged.connect(lambda *_: self._timer.start())
            browse = _button("Browse…", self._browse)
            opts.addWidget(self.use_base)
            opts.addWidget(self.base, 1)
            opts.addWidget(browse)
        opts.addStretch(1)
        layout.addLayout(opts)

        self.error = QLabel("")
        self.error.setStyleSheet(f"color: {theme.current().danger};")
        layout.addWidget(self.error)
        cols = ["File", "Title", "Year", "People", "Note"] if mode == "read" else ["File", "New name", "Note"]
        self.table = QTableWidget(0, len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        if mode == "rename":
            note = QLabel("If Sonarr, Radarr or Plex manage these folders, let them do the renaming, or rescan "
                          "them afterwards: renaming behind their back can make them lose track of files.")
            note.setObjectName("Faint")
            note.setWordWrap(True)
            layout.addWidget(note)
        bottom = QHBoxLayout()
        self.summary = QLabel("")
        self.summary.setObjectName("Faint")
        bottom.addWidget(self.summary, 1)
        self.apply_button = _button("Apply", self._apply)
        self.apply_button.setDefault(False)
        bottom.addWidget(self.apply_button)
        bottom.addWidget(_button("Close", self.reject))
        layout.addLayout(bottom)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._refresh)
        self._refresh()

    # ---------------------------------------------------------------- preview
    def _insert(self, field: str):
        edit = self.pattern.lineEdit()
        edit.insert("{" + field + "}")
        edit.setFocus()

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Move into folder", self.base.text() or "")
        if folder:
            self.base.setText(folder)
            self.use_base.setChecked(True)

    def _refresh(self):
        pattern = self.pattern.currentText().strip()
        self.table.setRowCount(0)
        self.plans = []
        try:
            if self.mode == "read":
                self.plans = N.plan_read([r.path for r in self.records], pattern)
            else:
                base = self.base.text().strip() if self.use_base.isChecked() and self.base.text().strip() else None
                self.plans = N.plan_renames(self.records, pattern, base=base)
            self.error.setText("")
        except N.PatternError as exc:
            self.error.setText(str(exc))
            self.apply_button.setEnabled(False)
            self.summary.setText("")
            return
        records = {r.path: r for r in self.records}
        for plan in self.plans:
            row = self.table.rowCount()
            self.table.insertRow(row)
            if self.mode == "read":
                r = records.get(plan.path)
                note = ""
                if not plan.matched:
                    note = "doesn't match the pattern"
                elif r is not None and not self.overwrite.isChecked() and r.title and plan.title and r.title != plan.title:
                    note = f"keeps its title “{r.title}”"
                if plan.people:
                    new = [p for p in plan.people if self.db.find_person(p) is None]
                    if new:
                        note = (note + "; " if note else "") + "new: " + ", ".join(new)
                cells = [Path(plan.path).name, plan.title or "", str(plan.year or ""), ", ".join(plan.people), note]
            else:
                cells = [Path(plan.old).name, str(Path(plan.new).relative_to(Path(plan.old).parent))
                         if Path(plan.new).is_relative_to(Path(plan.old).parent) else plan.new,
                         plan.problem or ("" if plan.changes else "already named like this")]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if col == len(cells) - 1 and text and (self.mode == "rename" and plan.problem or
                                                       self.mode == "read" and not plan.matched):
                    item.setForeground(Qt.GlobalColor.darkRed)
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()
        if self.mode == "read":
            n = sum(1 for p in self.plans if p.matched)
            self.summary.setText(f"{n} of {len(self.plans)} file name(s) match")
            self.apply_button.setText(f"Fill in {n} video(s)")
        else:
            n = sum(1 for p in self.plans if p.changes)
            flagged = sum(1 for p in self.plans if p.problem)
            self.summary.setText(f"{n} to rename" + (f", {flagged} skipped (see Note)" if flagged else ""))
            self.apply_button.setText(f"Rename {n} video(s)")
        self.apply_button.setEnabled(n > 0)

    # ---------------------------------------------------------------- applying
    def _apply(self):
        if self.settings is not None:
            patterns = dict(getattr(self.settings, "name_patterns", {}) or {})
            patterns[self.mode] = self.pattern.currentText().strip()
            self.settings.name_patterns = patterns
            if hasattr(self.settings, "save"):
                self.settings.save()
        plans = list(self.plans)
        paths = [Path(p.path if self.mode == "read" else p.old) for p in plans]
        db_path = self.db.path
        overwrite = self.mode == "read" and self.overwrite.isChecked()
        create = self.mode == "read" and self.create_people.isChecked()
        dialog = ProgressDialog("Updating videos" if self.mode == "read" else "Renaming", cancellable=True, parent=self)

        def work(job):
            db = TagDB(db_path)
            try:
                if self.mode == "read":
                    return N.apply_read(db, plans, overwrite=overwrite, create_people=create,
                                        progress=job.report, cancel=job.cancel)
                batch, problems = N.apply_renames(db, plans, progress=job.report, cancel=job.cancel)
                self.moved = db.renames(batch)
                return problems
            finally:
                db.close()

        def done(problems):
            dialog.finish()
            self.applied = True
            if problems:
                TextReportDialog("Some videos weren't changed",
                                 "\n".join(f"{Path(p).name}: {why}" for p, why in problems), self).exec()
            self.accept()

        def failed(exc, _tb):
            dialog.finish()
            QMessageBox.critical(self, self.windowTitle(), str(exc))

        def start():
            job = workers.start(work, on_done=done, on_error=failed, on_progress=dialog.update_progress)
            dialog.cancelRequested.connect(job.cancel.set)
            dialog.show()

        if self.host is not None:
            self.host.release_for(paths, start)
        else:
            start()
