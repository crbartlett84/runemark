"""Conversion plan, progress, settings and self-test dialogs."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from .. import datastore, explorer
from ..media import convert as C
from ..media.probe import fmt_size
from ..paths import IS_WINDOWS, app_data_dir, default_backup_dir, is_portable, log_dir
from ..tagdb import TagDB, TagDBError
from ..settings import Settings
from . import theme
from .widgets import Banner, text_button


class ConvertDialog(QDialog):
    """Shows what happens to every track before an MKV becomes an MP4."""

    def __init__(self, plan: C.ConversionPlan, parent=None):
        super().__init__(parent)
        self.plan = plan
        self.setWindowTitle("Convert to MP4 and save")
        self.resize(820, 520)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        banner = Banner()
        if plan.possible:
            banner.show_message(
                "info", "Saving converts this MKV to MP4 so Windows can show its tags and thumbnail. "
                        "<b>Video is copied as is</b>, so the picture doesn't change. "
                        "Check what happens to each track below.")
        else:
            banner.show_message("error", plan.blockers[0])
        layout.addWidget(banner)

        out_row = QHBoxLayout()
        out_label = QLabel("New file")
        out_label.setObjectName("SectionLabel")
        self.output = QLineEdit(str(plan.output))
        browse = text_button("Change…", "folder")
        browse.clicked.connect(self._browse)
        out_row.addWidget(out_label)
        out_row.addWidget(self.output, 1)
        out_row.addWidget(browse)
        layout.addLayout(out_row)

        self.table = QTableWidget(len(plan.tracks), 3)
        self.table.setHorizontalHeaderLabels(["Track", "What happens", "Why"])
        self.table.verticalHeader().hide()
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        kinds = {"video": "Video", "audio": "Audio", "subtitle": "Subtitles", "attachment": "Attachment",
                 "data": "Data"}
        self._combos: list[QComboBox | None] = []
        for row, track in enumerate(plan.tracks):
            kind = "Cover image" if track.stream.is_attached_pic else kinds.get(track.kind, track.kind.title())
            item = QTableWidgetItem(f"{kind}: {track.describe()}")
            self.table.setItem(row, 0, item)
            if len(track.choices) > 1:
                combo = QComboBox()
                for choice in track.choices:
                    combo.addItem(C.ACTION_LABELS[choice], choice)
                combo.setCurrentIndex(track.choices.index(track.action))
                combo.currentIndexChanged.connect(self._update_summary)
                self.table.setCellWidget(row, 1, combo)
                self._combos.append(combo)
            else:
                self.table.setItem(row, 1, QTableWidgetItem(C.ACTION_LABELS[track.action]))
                self._combos.append(None)
            note = QTableWidgetItem(track.note)
            note.setForeground(_color(theme.current().muted))
            self.table.setItem(row, 2, note)
        self.table.resizeRowsToContents()
        layout.addWidget(self.table, 1)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setObjectName("Muted")
        layout.addWidget(self.summary)

        buttons = QDialogButtonBox()
        self.ok = buttons.addButton("Convert and save", QDialogButtonBox.ButtonRole.AcceptRole)
        self.ok.setObjectName("Primary")
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        self.ok.setEnabled(plan.possible)
        layout.addWidget(buttons)
        self._update_summary()

    def _browse(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save MP4 as", self.output.text(), "MP4 video (*.mp4)")
        if path:
            if not path.lower().endswith(".mp4"):
                path += ".mp4"
            self.output.setText(path)
            self._update_summary()

    def _apply(self):
        for track, combo in zip(self.plan.tracks, self._combos):
            if combo is not None:
                track.action = combo.currentData()
        self.plan.output = Path(self.output.text())

    def _update_summary(self, *_):
        self._apply()
        kept = len(self.plan.kept())
        sidecars = C.sidecar_paths(self.plan) if self.plan.possible else {}
        lines = [f"{kept} track(s) go into the MP4."]
        if sidecars:
            names = ", ".join(p.name for p in sidecars.values())
            lines.append(f"Saved beside it: {names}.")
        if self.plan.chapter_note:
            lines.append(self.plan.chapter_note)
        lines.append("Afterwards Runemark checks the MP4 (length and tracks), writes your tags and thumbnail, "
                     "then sends the original MKV to the Recycle Bin.")
        try:
            free = shutil.disk_usage(self.plan.output.parent).free
            need = self.plan.info.size
            if free < need + 64 * 1024 * 1024:
                lines.append(f"<span style='color:{theme.current().danger}'>Not enough free space: needs about "
                             f"{fmt_size(need)}, {fmt_size(free)} free.</span>")
                self.ok.setEnabled(False)
            elif self.plan.possible:
                self.ok.setEnabled(True)
        except OSError:
            pass
        self.summary.setText(" ".join(lines))

    def _accept(self):
        self._apply()
        out = self.plan.output
        if out.resolve() == self.plan.source.resolve():
            QMessageBox.warning(self, "Convert", "Choose a different name for the MP4.")
            return
        if out.exists():
            answer = QMessageBox.question(self, "Replace file?", f"{out.name} already exists. Replace it?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        if not out.parent.exists():
            QMessageBox.warning(self, "Convert", "That folder doesn't exist.")
            return
        self.accept()


class ProgressDialog(QDialog):
    cancelRequested = Signal()

    def __init__(self, title: str, cancellable: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.resize(440, 140)
        layout = QVBoxLayout(self)
        self.label = QLabel("Working…")
        self.label.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        layout.addWidget(self.label)
        layout.addWidget(self.bar)
        if cancellable:
            row = QHBoxLayout()
            row.addStretch(1)
            self.cancel = QPushButton("Cancel")
            self.cancel.clicked.connect(self._cancel)
            row.addWidget(self.cancel)
            layout.addLayout(row)

    def _cancel(self):
        self.label.setText("Cancelling…")
        self.cancel.setEnabled(False)
        self.cancelRequested.emit()

    def update_progress(self, percent: int, message: str):
        if message:
            self.label.setText(message)
        if percent < 0:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 100)
            self.bar.setValue(percent)

    def reject(self):  # Esc does nothing while work is running
        pass

    def finish(self):
        super().accept()


class SettingsDialog(QDialog):
    databaseRestored = Signal()

    def __init__(self, settings: Settings, parent=None, db: TagDB | None = None):
        super().__init__(parent)
        self.settings = settings
        self.db = db
        self.setWindowTitle("Settings")
        self.resize(640, 720)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setVerticalSpacing(10)

        self.theme = QComboBox()
        for label, value in (("Match Windows", "system"), ("Dark", "dark"), ("Light", "light")):
            self.theme.addItem(label, value)
        self.theme.setCurrentIndex(max(0, self.theme.findData(settings.theme)))
        form.addRow("Appearance", self.theme)

        self.max_edge = QComboBox()
        for label, value in (("640 px", 640), ("960 px", 960), ("1280 px (recommended)", 1280),
                             ("1920 px", 1920), ("Full frame size", 0)):
            self.max_edge.addItem(label, value)
        self.max_edge.setCurrentIndex(max(0, self.max_edge.findData(settings.thumbnail_max_edge)))
        form.addRow("Thumbnail size (longest side)", self.max_edge)

        self.quality = QSpinBox()
        self.quality.setRange(60, 100)
        self.quality.setValue(settings.jpeg_quality)
        form.addRow("Thumbnail JPEG quality", self.quality)

        self.film = QSpinBox()
        self.film.setRange(0, 30)
        self.film.setValue(settings.filmstrip_count)
        self.film.setSpecialValueText("Off")
        form.addRow("Filmstrip frames", self.film)

        # 0.9.2: the four nudge sizes (the buttons under the player and Alt / Ctrl+Shift / Shift / Ctrl + arrows)
        from .player import NUDGE_MODIFIERS, clean_nudge_steps
        steps = clean_nudge_steps(getattr(settings, "nudge_steps", None))
        nudge_row = QHBoxLayout()
        self.nudge_spins: list[QDoubleSpinBox] = []
        for value, modifier in zip(steps, NUDGE_MODIFIERS):
            spin = QDoubleSpinBox()
            spin.setRange(0.05, 600.0)
            spin.setDecimals(2)
            spin.setSingleStep(0.5)
            spin.setSuffix(" s")
            spin.setValue(value)
            spin.setToolTip(f"{modifier}+← →")
            nudge_row.addWidget(spin)
            self.nudge_spins.append(spin)
        nudge_row.addStretch(1)
        form.addRow("Nudge steps", nudge_row)
        nudge_note = QLabel("The buttons under the player, and Alt, Ctrl+Shift, Shift and Ctrl with ← →, in that "
                            "order. ← → alone (and , .) still step one frame.")
        nudge_note.setObjectName("Faint")
        nudge_note.setWordWrap(True)
        form.addRow("", nudge_note)

        self.preserve = QCheckBox("Keep each file's “Date modified” when saving")
        self.preserve.setChecked(settings.preserve_modified_date)
        note = QLabel("Off by default: Plex, Jellyfin and Explorer's thumbnail cache notice changes by that date.")
        note.setObjectName("Faint")
        note.setWordWrap(True)
        form.addRow("", self.preserve)
        form.addRow("", note)

        self.read_nfo = QCheckBox("Fill in details from a .nfo beside the video")
        self.read_nfo.setChecked(settings.read_nfo)
        nfo_note = QLabel("When a video Runemark hasn't saved yet opens, empty title, year, studio, description and "
                          "people are filled in from Movie.nfo beside Movie.mp4 (the format Kodi, Jellyfin, Radarr "
                          "and Sonarr write). Nothing is saved until you save.")
        nfo_note.setObjectName("Faint")
        nfo_note.setWordWrap(True)
        form.addRow("", self.read_nfo)
        form.addRow("", nfo_note)
        self.nfo_people = QComboBox()              # 0.11.1
        for label, value in (("Add them (new names go into People too)", "add"),
                             ("Only people already in People", "known"),
                             ("Don't take people from it", "off")):
            self.nfo_people.addItem(label, value)
        self.nfo_people.setCurrentIndex(max(0, self.nfo_people.findData(getattr(settings, "nfo_people", "add"))))
        self.nfo_people.setToolTip("A .nfo lists everyone in the video. \"Only people already in People\" puts the "
                                   "ones you know on the video and creates nobody new, so a plugin that knows who "
                                   "to leave out (Lore leaves out male performers) adds the new ones.")
        self.read_nfo.toggled.connect(self.nfo_people.setEnabled)
        self.nfo_people.setEnabled(self.read_nfo.isChecked())
        form.addRow("People in the .nfo", self.nfo_people)

        # 0.12.0: the local copy of videos on network drives
        self.video_cache = QCheckBox("Copy videos on network drives to this computer (faster scrubbing)")
        self.video_cache.setChecked(getattr(settings, "video_cache", True))
        self.video_cache.setToolTip("The open video and the next ones in the list are copied in the background; the "
                                    "player switches to the copy when it's ready. Saves still go to the original.")
        form.addRow("", self.video_cache)
        cache_row = QHBoxLayout()
        self.video_cache_gb = QSpinBox()
        self.video_cache_gb.setRange(2, 500)
        self.video_cache_gb.setSuffix(" GB")
        self.video_cache_gb.setValue(int(getattr(settings, "video_cache_gb", 20)))
        self.video_cache_ahead = QSpinBox()
        self.video_cache_ahead.setRange(0, 10)
        self.video_cache_ahead.setValue(int(getattr(settings, "video_cache_ahead", 3)))
        self.video_cache_ahead.setToolTip("How many of the next videos in the list are copied ahead")
        self.clear_video_cache = QPushButton("Empty it")
        self.clear_video_cache.setAutoDefault(False)
        self.clear_video_cache.clicked.connect(self._clear_video_cache)
        cache_row.addWidget(self.video_cache_gb)
        cache_row.addWidget(QLabel("next videos copied ahead:"))
        cache_row.addWidget(self.video_cache_ahead)
        cache_row.addWidget(self.clear_video_cache)
        cache_row.addStretch(1)
        form.addRow("Local copy, at most", cache_row)
        for w in (self.video_cache_gb, self.video_cache_ahead):
            self.video_cache.toggled.connect(w.setEnabled)
            w.setEnabled(self.video_cache.isChecked())

        self.scan_on_start = QCheckBox("Look for new videos in the Library folders when Runemark starts")
        self.scan_on_start.setChecked(settings.scan_on_start)
        self.check_updates = QCheckBox("Check for a new version of Runemark once a day")
        self.check_updates.setChecked(settings.check_updates)
        form.addRow("", self.scan_on_start)
        form.addRow("", self.check_updates)
        self.find_cuts = QCheckBox("Look for likely cuts in each video a few seconds after it opens")
        self.find_cuts.setToolTip("Otherwise Chapters → Find cuts looks when you ask. Either way it runs in the "
                                  "background and is remembered for each video.")
        self.find_cuts.setChecked(getattr(settings, "find_cuts_on_open", False))
        self.cut_marks = QCheckBox("Show the cuts as faint dots under the timeline")
        self.cut_marks.setChecked(getattr(settings, "show_cut_marks", True))
        form.addRow("", self.find_cuts)
        form.addRow("", self.cut_marks)

        self.writer = QComboBox()
        self.writer.addItem("Windows (falls back to Runemark's own writer)", "auto")
        self.writer.addItem("Runemark's own writer only", "builtin")
        self.writer.setCurrentIndex(max(0, self.writer.findData(settings.explorer_writer)))
        form.addRow("Explorer Tags writer", self.writer)
        layout.addLayout(form)

        layout.addSpacing(6)
        if IS_WINDOWS:
            row = QHBoxLayout()
            self.menu_button = QPushButton()
            self.menu_button.clicked.connect(self._toggle_menu)
            self._update_menu_button()
            row.addWidget(QLabel("Explorer right-click menu"))
            row.addStretch(1)
            row.addWidget(self.menu_button)
            layout.addLayout(row)

        layout.addSpacing(8)
        heading = QLabel("YOUR TAG DATABASE")
        heading.setObjectName("CardTitle")
        layout.addWidget(heading)
        where = QLabel(datastore.describe_location())
        where.setObjectName("Muted")
        where.setWordWrap(True)
        where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(where)

        actions = QHBoxLayout()
        backup_now = QPushButton("Back up now…")
        backup_now.clicked.connect(self._backup_now)
        restore = QPushButton("Restore from a backup…")
        restore.clicked.connect(self._restore)
        data = QPushButton("Open data folder")
        data.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(app_data_dir()))))
        logs = QPushButton("Open log folder")
        logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir()))))
        for button in (backup_now, restore, data, logs):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        for button in (backup_now, restore):
            button.setEnabled(db is not None)

        auto = QFormLayout()
        folder_row = QHBoxLayout()
        self.backup_folder = QLineEdit(settings.backup_folder)
        self.backup_folder.setPlaceholderText(f"Default: {default_backup_dir()}")
        browse = QPushButton("Change…")
        browse.clicked.connect(self._choose_backup_folder)
        folder_row.addWidget(self.backup_folder, 1)
        folder_row.addWidget(browse)
        auto.addRow("Daily backups go to", folder_row)
        self.keep = QSpinBox()
        self.keep.setRange(1, 365)
        self.keep.setValue(settings.backup_keep)
        auto.addRow("Keep this many", self.keep)
        layout.addLayout(auto)
        tip = QLabel("By default backups go to Documents\\Backups\\Runemark: when OneDrive backs up your Documents, "
                     "that keeps a copy off this PC. Backups are complete, closed files, so syncing them is safe.")
        tip.setObjectName("Faint")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        self.portable = QCheckBox("Portable: keep the database and settings in a “Runemark data” folder beside "
                                  "Runemark.exe")
        self.portable.setChecked(is_portable())
        layout.addWidget(self.portable)
        portable_note = QLabel("Useful on a USB drive or a synced folder. When you update Runemark by replacing its "
                               "folder, keep “Runemark data” and Runemark-portable.txt. Takes effect after a restart.")
        portable_note.setObjectName("Faint")
        portable_note.setWordWrap(True)
        layout.addWidget(portable_note)
        self.portable.setEnabled(db is not None)
        layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("Primary")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _update_menu_button(self):
        installed = explorer.context_menu_installed()
        self.menu_button.setText("Remove “Tag with Runemark”" if installed else "Add “Tag with Runemark”")

    def _toggle_menu(self):
        try:
            if explorer.context_menu_installed():
                explorer.remove_context_menu()
            else:
                explorer.install_context_menu()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Explorer menu", str(exc))
        self._update_menu_button()

    # ---------------------------------------------------------------- database
    def _choose_backup_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Daily backups go to",
                                                  self.backup_folder.text() or str(default_backup_dir()))
        if folder:
            self.backup_folder.setText(str(Path(folder)))

    def _backup_now(self):
        import datetime as _dt
        start = Path(self.backup_folder.text() or default_backup_dir()) / f"Runemark tags {_dt.date.today():%Y-%m-%d}.db"
        name, _ = QFileDialog.getSaveFileName(self, "Back up the tag database", str(start),
                                              "Runemark tag database (*.db)")
        if not name:
            return
        try:
            target = datastore.backup_now(self.db, name)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Back up", f"Couldn't write the backup:\n\n{exc}")
            return
        QMessageBox.information(self, "Back up", f"Saved a complete copy of your tag database to:\n{target}")

    def _restore(self):
        name, _ = QFileDialog.getOpenFileName(self, "Restore the tag database",
                                              self.backup_folder.text() or str(default_backup_dir()),
                                              "Runemark tag database (*.db)")
        if not name:
            return
        try:
            counts = TagDB.check_backup(name)
        except TagDBError as exc:
            QMessageBox.warning(self, "Restore", str(exc))
            return
        answer = QMessageBox.question(
            self, "Restore", f"Replace your tag database with this backup?\n\n{Path(name).name}: "
            f"{counts['tags']} tags in {counts['categories']} categories, {counts['files']} files.\n\n"
            "Your current database is backed up first, so this can be undone.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            safety = datastore.safety_backup(self.db)
            self.db.restore_from(name)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Restore", f"The restore didn't work, and nothing was changed:\n\n{exc}")
            return
        self.databaseRestored.emit()
        QMessageBox.information(self, "Restore", f"Restored. The database you had before is saved as:\n{safety}")

    def _apply_portable(self) -> bool:
        wanted = self.portable.isChecked()
        if self.db is None or wanted == is_portable():
            return True
        try:
            target = datastore.make_portable(self.db) if wanted else datastore.make_standard(self.db)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Portable", f"Couldn't move your data:\n\n{exc}")
            return False
        QMessageBox.information(self, "Restart Runemark",
                                f"Your tag database and settings were copied to:\n{target}\n\n"
                                "Close Runemark and open it again to start using them.")
        return True

    def _clear_video_cache(self):
        """0.12.0: empty the local copies (not the one playing)."""
        from ..media.cache import VideoCache
        cache = getattr(self.parent(), "video_cache", None) if self.parent() is not None else None
        freed = (cache or VideoCache()).clear()
        QMessageBox.information(self, "Local copies", f"Freed {fmt_size(freed)}." if freed else
                                "The local copy folder is already empty.")

    def _save(self):
        s = self.settings
        s.backup_folder = self.backup_folder.text().strip()
        s.backup_keep = self.keep.value()
        s.theme = self.theme.currentData()
        s.thumbnail_max_edge = self.max_edge.currentData()
        s.jpeg_quality = self.quality.value()
        s.filmstrip_count = self.film.value()
        from .player import clean_nudge_steps
        s.nudge_steps = list(clean_nudge_steps([spin.value() for spin in self.nudge_spins]))
        s.preserve_modified_date = self.preserve.isChecked()
        s.read_nfo = self.read_nfo.isChecked()
        s.nfo_people = self.nfo_people.currentData() or "add"
        s.video_cache = self.video_cache.isChecked()
        s.video_cache_gb = self.video_cache_gb.value()
        s.video_cache_ahead = self.video_cache_ahead.value()
        s.scan_on_start = self.scan_on_start.isChecked()
        s.check_updates = self.check_updates.isChecked()
        s.find_cuts_on_open = self.find_cuts.isChecked()
        s.show_cut_marks = self.cut_marks.isChecked()
        s.explorer_writer = self.writer.currentData()
        s.save()
        if not self._apply_portable():
            return
        self.accept()


class TextReportDialog(QDialog):
    def __init__(self, title: str, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(640, 460)
        layout = QVBoxLayout(self)
        self.view = QPlainTextEdit(text)
        self.view.setReadOnly(True)
        layout.addWidget(self.view, 1)
        row = QHBoxLayout()
        copy = text_button("Copy report", "copy")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.view.toPlainText()))
        row.addWidget(copy)
        row.addStretch(1)
        close = text_button("Close", primary=True)
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)

    def set_text(self, text: str):
        self.view.setPlainText(text)


def _color(value: str):
    from PySide6.QtGui import QColor
    return QColor(value)


__all__ = ["ConvertDialog", "ProgressDialog", "SettingsDialog", "TextReportDialog", "os"]
