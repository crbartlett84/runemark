"""Library → More → Save space…: which videos would shrink by re-encoding to HEVC, by how much, and doing it."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from .. import spacesaver as S
from ..media.probe import fmt_bitrate, fmt_duration
from ..tagdb import TagDB
from . import workers
from .dialogs import ProgressDialog, TextReportDialog


class SpaceSaverDialog(QDialog):
    changed = Signal()
    openRequested = Signal(list)

    def __init__(self, db: TagDB, records: list, host=None, parent=None):
        super().__init__(parent)
        self.db, self.records, self.host = db, list(records), host
        self.candidates: list[S.Candidate] = []
        self.setWindowTitle("Save space — Runemark")
        self.resize(1000, 600)
        layout = QVBoxLayout(self)
        intro = QLabel("Older videos (H.264, MPEG-4…) take about twice the space of HEVC at the same picture quality, "
                       "and HEVC plays directly on most TVs. Re-encoding keeps everything Runemark knows about each "
                       "video (tags, people, details, thumbnail, chapters) and the same file name; the original goes "
                       "to the Recycle Bin, and a copy that doesn't check out is thrown away with the original left "
                       "alone. Sizes after are estimates. Nothing changes until you press Re-encode.")
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Video", "Now", "Quality", "Size", "About after", "Saves", "Note"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(lambda *_: self._totals())
        self.table.itemDoubleClicked.connect(self._open)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.summary = QLabel("")
        self.summary.setObjectName("Faint")
        row.addWidget(self.summary, 1)
        self.go = QPushButton("Re-encode")
        self.go.clicked.connect(self._run)
        self.go.setEnabled(False)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(self.go)
        row.addWidget(close)
        layout.addLayout(row)

    # ---------------------------------------------------------------- looking
    def assess(self):
        dialog = ProgressDialog("Looking at the videos", cancellable=True, parent=self)
        records = [r for r in self.records if not r.missing]

        def work(job):
            out = []
            for n, rec in enumerate(records):
                if job.cancel.is_set():
                    break
                job.report(int(n * 100 / max(1, len(records))), Path(rec.path).name)
                try:
                    out.append(S.assess(rec.path))
                except Exception as exc:  # noqa: BLE001 (one unreadable file doesn't stop the rest)
                    c = S.Candidate(Path(rec.path), "?", 0, 0.0, 0, 0, 0, reason=f"can't be read: {exc}")
                    out.append(c)
            encoder = ""
            try:
                encoder = S.LABELS.get(S.choose_encoder()[0], "")
            except Exception as exc:  # noqa: BLE001
                encoder = f"no HEVC encoder: {exc}"
            return out, encoder

        def done(result):
            dialog.finish()
            self.candidates, self.encoder = result
            self._fill()

        def failed(exc, _tb):
            dialog.finish()
            QMessageBox.warning(self, "Save space", str(exc))

        job = workers.start(work, on_done=done, on_error=failed, on_progress=dialog.update_progress)
        dialog.cancelRequested.connect(job.cancel.set)
        dialog.show()

    def _fill(self):
        self.candidates.sort(key=lambda c: (not c.worth_it, -c.saving))
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.candidates))
        for r, c in enumerate(self.candidates):
            name = QTableWidgetItem(c.path.name)
            name.setToolTip(str(c.path))
            name.setData(Qt.ItemDataRole.UserRole, r)
            if c.worth_it:
                name.setFlags(name.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                name.setCheckState(Qt.CheckState.Checked)
            cells = [name, QTableWidgetItem(c.codec.upper()),
                     QTableWidgetItem(f"{c.height}p · {fmt_bitrate(c.bitrate)} · {fmt_duration(c.duration)}"
                                      if c.height else ""),
                     QTableWidgetItem(S.fmt_bytes(c.size)),
                     QTableWidgetItem(S.fmt_bytes(c.estimate) if c.worth_it else ""),
                     QTableWidgetItem(S.fmt_bytes(c.saving) if c.worth_it else ""),
                     QTableWidgetItem(c.reason or "")]
            for col, item in enumerate(cells):
                self.table.setItem(r, col, item)
        self.table.blockSignals(False)
        self.table.resizeColumnsToContents()
        self._totals()

    def _chosen(self) -> list[S.Candidate]:
        out = []
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                out.append(self.candidates[item.data(Qt.ItemDataRole.UserRole)])
        return out

    def _totals(self):
        chosen = self._chosen()
        worth = sum(1 for c in self.candidates if c.worth_it)
        if not self.candidates:
            self.summary.setText("")
        elif not worth:
            self.summary.setText(f"None of these {len(self.candidates)} video(s) would get much smaller.")
        else:
            self.summary.setText(
                f"{len(chosen)} of {worth} worth re-encoding ticked: about {S.fmt_bytes(sum(c.saving for c in chosen))}"
                f" saved (of {S.fmt_bytes(sum(c.size for c in chosen))}). Encoder: {getattr(self, 'encoder', '')}.")
        self.go.setEnabled(bool(chosen))
        self.go.setText(f"Re-encode {len(chosen)}" if chosen else "Re-encode")

    def _open(self, item):
        c = self.candidates[self.table.item(item.row(), 0).data(Qt.ItemDataRole.UserRole)]
        if c.path.exists():
            self.openRequested.emit([c.path])

    # ---------------------------------------------------------------- doing it
    def _run(self):
        chosen = self._chosen()
        if not chosen:
            return
        hours = sum(c.duration for c in chosen) / 3600
        if QMessageBox.question(
                self, "Save space",
                f"Re-encode {len(chosen)} video(s) to HEVC ({hours:.1f} hours of video)? It can take a while, and you "
                "can stop between videos. Each original goes to the Recycle Bin once its new copy checks out.") \
                != QMessageBox.StandardButton.Yes:
            return
        paths = [c.path for c in chosen]
        if self.host is not None:
            self.host.release_for(paths, lambda: self._start(chosen))
        else:
            self._start(chosen)

    def _start(self, chosen: list[S.Candidate]):
        dialog = ProgressDialog("Re-encoding to save space", cancellable=True, parent=self)
        db_path = self.db.path

        def work(job):
            db = TagDB(db_path)
            saved, problems, done_n = 0, [], 0
            try:
                for n, c in enumerate(chosen):
                    if job.cancel.is_set():
                        break
                    label = f"{n + 1} of {len(chosen)}: {c.path.name}"

                    def report(percent, message, _label=label):
                        job.report(percent if percent >= 0 else 99, f"{_label} — {message}")
                    before = c.size
                    try:
                        S.shrink(db, c, progress=report, cancel=job.cancel)
                        saved += max(0, before - c.path.stat().st_size)
                        done_n += 1
                    except Exception as exc:  # noqa: BLE001 (one video's problem doesn't stop the rest)
                        if job.cancel.is_set():
                            break
                        problems.append(f"{c.path.name}: {exc}")
            finally:
                db.close()
            return done_n, saved, problems

        def finished(result):
            dialog.finish()
            done_n, saved, problems = result
            self.changed.emit()
            text = f"Re-encoded {done_n} video(s), {S.fmt_bytes(saved)} saved."
            if problems:
                TextReportDialog(text + " Some were left alone:", "\n".join(problems), self).show()
            else:
                QMessageBox.information(self, "Save space", text)
            self.assess()

        def failed(exc, _tb):
            dialog.finish()
            QMessageBox.warning(self, "Save space", str(exc))

        job = workers.start(work, on_done=finished, on_error=failed, on_progress=dialog.update_progress)
        dialog.cancelRequested.connect(job.cancel.set)
        dialog.show()
