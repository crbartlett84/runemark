"""The library window: every video in your folders, searchable by tag, changeable in bulk."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QStringListModel, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QCompleter, QDialog, QFileDialog, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton,
                               QTableView, QToolButton, QVBoxLayout)

from .. import library as L
from ..tagdb import FileRecord, TagDB
from . import theme, workers
from .dialogs import ProgressDialog, TextReportDialog
from .library_model import COLUMNS, PATH_ROLE, LibraryModel, stars  # noqa: F401 (stars: kept for importers)
from .widgets import Chip, ChipArea, text_button


class LibraryWindow(QDialog):
    openRequested = Signal(list)       # paths to open in the editor

    def __init__(self, db: TagDB, settings, host, parent=None):
        super().__init__(parent)
        self.db = db
        self.settings = settings
        self.host = host                # the main window: release_for(paths, then) and files_changed(paths)
        self.search = L.Search()
        self.records: list[FileRecord] = []
        self._scan_job = None
        self._closing = False        # set on close: late callbacks from a background scan must not touch the window
        self.setWindowTitle("Library — Runemark")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.resize(1180, 720)
        if settings.library_geometry:
            self.restoreGeometry(QByteArray.fromBase64(settings.library_geometry.encode()))

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # ---- search row
        row = QHBoxLayout()
        self.text = QLineEdit()
        self.text.setPlaceholderText("Search file names and titles")
        self.text.setClearButtonEnabled(True)
        self.text.textChanged.connect(self._text_changed)
        self.tag_input = QLineEdit()
        self.tag_input.setPlaceholderText("Must have tag… (Enter)")
        self.tag_input.returnPressed.connect(self._add_search_tag)
        self.tag_model = QStringListModel(self)
        completer = QCompleter(self.tag_model, self.tag_input)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.activated.connect(lambda _text: self._add_search_tag())
        self.tag_input.setCompleter(completer)
        self.filter = QComboBox()
        for value, label in L.FILTERS:
            self.filter.addItem(label, value)
        self.filter.currentIndexChanged.connect(self._filter_changed)
        row.addWidget(self.text, 2)
        row.addWidget(self.tag_input, 2)
        row.addWidget(self.filter, 1)
        layout.addLayout(row)

        chips_row = QHBoxLayout()
        self.chips = ChipArea()
        self.chips.set_placeholder("Showing every video. Add tags above to narrow it down (all of them must match).")
        chips_row.addWidget(self.chips, 1)
        self.saved = QComboBox()
        self.saved.setMinimumWidth(180)
        self.saved.activated.connect(self._load_saved)
        self.save_search_button = QToolButton()
        self.save_search_button.setText("Save search…")
        self.save_search_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.save_search_button.clicked.connect(self._save_search)
        menu = QMenu(self.save_search_button)
        menu.addAction("Delete this saved search", self._delete_saved)
        self.save_search_button.setMenu(menu)
        chips_row.addWidget(QLabel("Saved searches"))
        chips_row.addWidget(self.saved)
        chips_row.addWidget(self.save_search_button)
        layout.addLayout(chips_row)

        # ---- table
        self.model = LibraryModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for col, width in enumerate((260, 180, 56, 92, 260, 84, 72)):
            self.table.setColumnWidth(col, width)
        self.table.doubleClicked.connect(lambda *_: self._open())
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._selection_changed())
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        # typing in the search box waits for a short pause, so a fast typist doesn't trigger a search per key
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._apply)
        layout.addWidget(self.table, 1)

        self.empty = QLabel("")
        self.empty.setObjectName("Muted")
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)

        # ---- actions
        bottom = QHBoxLayout()
        self.folders_button = QToolButton()
        self.folders_button.setText("Folders")
        self.folders_button.setIcon(theme.icon("folder", size=16))
        self.folders_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.folders_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.folders_menu = QMenu(self.folders_button)
        self.folders_menu.aboutToShow.connect(self._fill_folders_menu)
        self.folders_button.setMenu(self.folders_menu)
        self.scan_button = text_button("Scan for changes", "sync")
        self.scan_button.clicked.connect(self._scan_clicked)
        self.count = QLabel("")
        self.count.setObjectName("Faint")
        bottom.addWidget(self.folders_button)
        bottom.addWidget(self.scan_button)
        bottom.addWidget(self.count, 1)
        self.open_button = text_button("Open", "open", primary=True)
        self.open_button.clicked.connect(self._open)
        self.add_tag_button = text_button("Add tag…", "plus")
        self.add_tag_button.clicked.connect(self._bulk_add)
        self.remove_tag_button = text_button("Remove tag…", "close")
        self.remove_tag_button.clicked.connect(self._bulk_remove)
        self.rating_button = text_button("Rate…", "star")
        self.rating_button.clicked.connect(self._bulk_rating)
        more = QToolButton()
        more.setText("More")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more_menu = QMenu(more)
        more_menu.addAction("Export for Jellyfin / Kodi (.nfo)…", self._export_nfo)
        more_menu.addAction("Show in File Explorer", self._reveal)
        more_menu.addSeparator()
        more_menu.addAction("Undo last bulk change…", self._undo_bulk)
        more_menu.addSeparator()
        more_menu.addAction("Read every file again", lambda: self.scan(rescan_all=True))
        more_menu.addAction("Remove selected from the library (files aren't touched)", self._forget)
        more.setMenu(more_menu)
        for b in (self.open_button, self.add_tag_button, self.remove_tag_button, self.rating_button, more):
            bottom.addWidget(b)
        layout.addLayout(bottom)
        self._fill_saved()
        if settings.library_last_search:
            self._set_search(L.Search.from_dict(settings.library_last_search))
        self._add_shortcuts()
        self.refresh()

    def _add_shortcuts(self):
        """Keys that act on the selected rows while the list has focus (not while typing in the search boxes)."""
        def key(sequence, slot):
            sc = QShortcut(QKeySequence(sequence), self.table)
            sc.setContext(Qt.ShortcutContext.WidgetShortcut)
            sc.activated.connect(slot)
            return sc
        for n in range(6):
            key(str(n), lambda n=n: self._rate_selected(n))
        key(Qt.Key.Key_Return, lambda: self._open())
        key(Qt.Key.Key_Enter, lambda: self._open())
        key("T", lambda: self._bulk_add())
        self.table.setToolTip("Enter opens · 0-5 rates · T adds tags (to the selected files)")

    def _rate_selected(self, stars_: int):
        if self.selected_paths():
            self._run_bulk(L.BulkChange(rating=stars_))

    # ---------------------------------------------------------------- data
    def refresh(self):
        self.tag_model.setStringList([t.name for t in self.db.tags()])
        self.records = self.db.library()
        self._apply()

    def _apply(self):
        self._search_timer.stop()
        shown = [r for r in self.records if L.matches(r, self.search)]
        self.model.faint = _color(theme.current().faint)
        self.model.set_records(shown)
        total = len(self.records)
        self.count.setText(f"{len(shown)} of {total} video{'s' if total != 1 else ''}")
        if not total:
            self.empty.setText("Your library is empty. Use Folders → Add a folder… to bring in a folder of videos. "
                               "Files you save in Runemark appear here too.")
            self.empty.show()
        elif not shown:
            self.empty.setText("Nothing matches this search.")
            self.empty.show()
        else:
            self.empty.hide()
        self._selection_changed()

    def selected_paths(self) -> list[Path]:
        rows = sorted(i.row() for i in self.table.selectionModel().selectedRows())
        return [Path(self.model.record(r).path) for r in rows if r < self.model.rowCount()]

    def _selection_changed(self):
        n = len(self.selected_paths())
        for b in (self.open_button, self.add_tag_button, self.remove_tag_button, self.rating_button):
            b.setEnabled(n > 0)

    # ---------------------------------------------------------------- search
    def _text_changed(self, text: str):
        self.search.text = text
        self._search_timer.start()          # restarts on every key; _apply runs after the pause

    def _filter_changed(self, *_):
        self.search.filter = self.filter.currentData()
        self._apply()

    def _add_search_tag(self):
        name = " ".join(self.tag_input.text().split())
        self.tag_input.clear()
        if name and name.casefold() not in {t.casefold() for t in self.search.tags}:
            self.search.tags.append(self.db.canonical(name))
        self._show_search_tags()
        self._apply()

    def _remove_search_tag(self, name: str):
        self.search.tags = [t for t in self.search.tags if t.casefold() != name.casefold()]
        self._show_search_tags()
        self._apply()

    def _show_search_tags(self):
        self.chips.flow.clear()
        for name in self.search.tags:
            chip = Chip(name, parent=self.chips)
            chip.removeClicked.connect(self._remove_search_tag)
            self.chips.flow.addWidget(chip)
        self.chips.refresh()

    def _set_search(self, search: L.Search):
        self.search = L.Search(list(search.tags), search.text, search.filter)
        self.text.blockSignals(True)
        self.text.setText(search.text)
        self.text.blockSignals(False)
        self.filter.blockSignals(True)
        self.filter.setCurrentIndex(max(0, self.filter.findData(search.filter)))
        self.filter.blockSignals(False)
        self._show_search_tags()
        self._apply()

    def _fill_saved(self):
        self.saved.clear()
        self.saved.addItem("—", None)
        for name in sorted(self.settings.saved_searches, key=str.casefold):
            self.saved.addItem(name, name)

    def _load_saved(self, index: int):
        name = self.saved.itemData(index)
        if name and name in self.settings.saved_searches:
            self._set_search(L.Search.from_dict(self.settings.saved_searches[name]))

    def _save_search(self):
        current = self.saved.currentData() or (" + ".join(self.search.tags) if self.search.tags else "")
        name, ok = QInputDialog.getText(self, "Save search", "Name (also used as the collection name when "
                                        "exporting for Jellyfin / Kodi)", text=current)
        name = " ".join(name.split())
        if ok and name:
            self.settings.saved_searches[name] = self.search.to_dict()
            self.settings.save()
            self._fill_saved()
            self.saved.setCurrentIndex(self.saved.findData(name))

    def _delete_saved(self):
        name = self.saved.currentData()
        if name and name in self.settings.saved_searches:
            del self.settings.saved_searches[name]
            self.settings.save()
            self._fill_saved()

    # ---------------------------------------------------------------- folders and scanning
    def _fill_folders_menu(self):
        self.folders_menu.clear()
        self.folders_menu.addAction(theme.icon("folder_plus", size=16), "Add a folder…", self._add_folder)
        if self.settings.library_folders:
            self.folders_menu.addSeparator()
            for folder in self.settings.library_folders:
                sub = self.folders_menu.addMenu(folder)
                sub.addAction("Remove from the library (files aren't touched)",
                              lambda f=folder: self._remove_folder(f))

    def _add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Add a folder of videos", self.settings.last_folder or "")
        if not folder:
            return
        folder = str(Path(folder))
        if folder.casefold() not in {f.casefold() for f in self.settings.library_folders}:
            self.settings.library_folders.append(folder)
            self.settings.save()
        self.scan()

    def _remove_folder(self, folder: str):
        answer = QMessageBox.question(self, "Remove folder", f"Stop including {folder} in the library?\n\n"
                                      "The videos stay where they are, with their tags.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.settings.library_folders = [f for f in self.settings.library_folders if f != folder]
        self.settings.save()
        self.db.forget_folder(folder)
        self.refresh()

    def _scan_clicked(self):
        if self._scan_job is not None:
            self.stop_scan()
        else:
            self.scan()

    def stop_scan(self):
        """Ask a running scan to stop after the file it's reading. What it has read so far is kept."""
        if self._scan_job is not None:
            self._scan_job.cancel.set()
            self.scan_button.setEnabled(False)
            self.count.setText("Stopping the scan…")

    def _scan_finished_ui(self):
        self._scan_job = None
        self.scan_button.setText("Scan for changes")
        self.scan_button.setEnabled(True)

    def scan(self, rescan_all: bool = False):
        if self._scan_job is not None:
            return
        folders = list(self.settings.library_folders)
        self.scan_button.setText("Stop scan")
        self.count.setText("Scanning…")

        def done(result: L.ScanResult):
            if self._closing:
                return
            self._scan_finished_ui()
            self.refresh()
            note = f"{result.found} videos · {result.read} read"
            if result.missing:
                note += f" · {result.missing} missing"
            if result.stopped:
                note += " · stopped early: scan again to finish"
            self.count.setText(self.count.text() + f"   (scan: {note})")
            if result.problems:
                TextReportDialog("Some files couldn't be read", "\n".join(result.problems), self).show()

        def failed(exc, _tb):
            if self._closing:
                return
            self._scan_finished_ui()
            QMessageBox.warning(self, "Library", f"The scan stopped: {exc}")

        def progress(_percent, message):
            if not self._closing and self._scan_job is not None and not self._scan_job.cancel.is_set():
                self.count.setText(message)

        def work(job):
            db = self.db_for_thread()
            try:
                return L.scan(db, folders, progress=job.report, cancel=job.cancel, rescan_all=rescan_all)
            finally:
                db.close()

        self._scan_job = workers.start(work, on_done=done, on_error=failed, on_progress=progress)

    def db_for_thread(self) -> TagDB:
        """SQLite connections belong to one thread: background work opens its own."""
        return TagDB(self.db.path)

    # ---------------------------------------------------------------- actions
    def _open(self):
        paths = [p for p in self.selected_paths() if p.exists()]
        if paths:
            self.openRequested.emit(paths)

    def _reveal(self):
        paths = self.selected_paths()
        if paths:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths[0].parent)))

    def _forget(self):
        paths = self.selected_paths()
        if not paths:
            return
        answer = QMessageBox.question(self, "Remove from library", f"Remove {len(paths)} file(s) from the library?\n\n"
                                      "Only Runemark's list changes. The files and their tags aren't touched, and "
                                      "files inside your library folders come back on the next scan.")
        if answer == QMessageBox.StandardButton.Yes:
            for p in paths:
                self.db.forget_file(p)
            self.refresh()

    def _bulk_add(self):
        if not self.selected_paths():
            return
        names = [t.name for t in self.db.tags()]
        text, ok = QInputDialog.getItem(self, "Add tags", f"Tags to add to {len(self.selected_paths())} file(s), "
                                        "separated by commas:", names, 0, True)
        tags = L.split_tag_input(text or "", names)
        if ok and tags:
            self._run_bulk(L.BulkChange(add=[self.db.canonical(t) for t in tags]))

    def _bulk_remove(self):
        paths = {str(p).casefold() for p in self.selected_paths()}
        present = sorted({t for r in self.records if r.path.casefold() in paths for t in r.tags}, key=str.casefold)
        if not present:
            QMessageBox.information(self, "Remove tag", "The selected files have no tags.")
            return
        name, ok = QInputDialog.getItem(self, "Remove tag", "Tag to remove:", present, 0, False)
        if ok and name:
            self._run_bulk(L.BulkChange(remove=[name]))

    def _bulk_rating(self):
        options = ["Not rated", "1 star", "2 stars", "3 stars", "4 stars", "5 stars"]
        choice, ok = QInputDialog.getItem(self, "Rate", f"Rating for {len(self.selected_paths())} file(s):",
                                          options, 0, False)
        if ok:
            self._run_bulk(L.BulkChange(rating=options.index(choice)))

    def _undo_bulk(self):
        entries = self.db.last_batch()
        if not entries:
            QMessageBox.information(self, "Undo last bulk change", "There's no bulk change to undo. Runemark "
                                    "remembers them from version 0.3.0 on.")
            return
        first = entries[0]
        when = first.saved_at.replace("T", " ")[:16]
        later = [e for e in entries if self.db.changed_since(e)]
        text = (f"Undo “{first.label}” from {when} on {len(entries)} file(s)?\n\n"
                "Each file's tags, genre and details go back to how they were just before it. "
                "Thumbnails and chapters aren't changed.")
        if later:
            text += (f"\n\n{len(later)} of them were changed again since, so they'll be left as they are "
                     "(open one and use Undo last save to step back).")
        if QMessageBox.question(self, "Undo last bulk change", text) != QMessageBox.StandardButton.Yes:
            return
        paths = [Path(e.path) for e in entries]
        self.host.release_for(paths, lambda: self._start_undo(entries, paths))

    def _start_undo(self, entries, paths: list[Path]):
        dialog = ProgressDialog("Undoing", cancellable=True, parent=self)
        mode, preserve = self.settings.explorer_writer, self.settings.preserve_modified_date

        def work(job):
            db = self.db_for_thread()
            try:
                return L.undo_batch(db, entries, explorer_mode=mode, preserve_mtime=preserve,
                                    progress=job.report, cancel=job.cancel)
            finally:
                db.close()

        def done(problems):
            dialog.finish()
            if self._closing:
                return
            self.refresh()
            self.host.files_changed(paths)
            undone = len(entries) - len(problems)
            self.count.setText(self.count.text() + f"   (undid {undone} of {len(entries)})")
            if problems:
                TextReportDialog("Some files weren't changed back",
                                 "\n".join(f"{p.name}: {why}" for p, why in problems), self).exec()

        def failed(exc, _tb):
            dialog.finish()
            QMessageBox.critical(self, "Undo", str(exc))

        job = workers.start(work, on_done=done, on_error=failed, on_progress=dialog.update_progress)
        dialog.cancelRequested.connect(job.cancel.set)
        dialog.show()

    def _run_bulk(self, change: L.BulkChange):
        paths = self.selected_paths()
        if not paths:
            return
        mkv = [p for p in paths if p.suffix.lower() == ".mkv"]
        if len(paths) > 1:
            answer = QMessageBox.question(self, "Change files", f"{change.describe().capitalize()} on "
                                          f"{len(paths)} file(s)?" + (f"\n\n{len(mkv)} MKV file(s) will be "
                                          "skipped: open and save them to convert them to MP4 first." if mkv else ""))
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.host.release_for(paths, lambda: self._start_bulk(paths, change))

    def _start_bulk(self, paths: list[Path], change: L.BulkChange):
        dialog = ProgressDialog("Updating files", cancellable=True, parent=self)
        mode, preserve = self.settings.explorer_writer, self.settings.preserve_modified_date

        def work(job):
            db = self.db_for_thread()
            try:
                return L.apply_bulk(db, paths, change, explorer_mode=mode, preserve_mtime=preserve,
                                    progress=job.report, cancel=job.cancel)
            finally:
                db.close()

        def done(problems):
            dialog.finish()
            self.refresh()
            self.host.files_changed(paths)
            if problems:
                TextReportDialog("Some files weren't changed",
                                 "\n".join(f"{p.name}: {why}" for p, why in problems), self).exec()

        def failed(exc, _tb):
            dialog.finish()
            self.refresh()
            self.host.files_changed(paths)
            QMessageBox.critical(self, "Change files", str(exc))

        job = workers.start(work, on_done=done, on_error=failed, on_progress=dialog.update_progress)
        dialog.cancelRequested.connect(job.cancel.set)
        dialog.show()

    def _export_nfo(self):
        paths = {str(p).casefold() for p in self.selected_paths()}
        chosen = [r for r in self.records if r.path.casefold() in paths and not r.missing]
        if not chosen:
            QMessageBox.information(self, "Export", "Select the videos to export first.")
            return
        collection, ok = QInputDialog.getText(
            self, "Export for Jellyfin / Kodi",
            f"Writes a .nfo file beside each of the {len(chosen)} video(s) with the title, year, description, "
            "rating, genres and tags.\n\nCollection name (optional):", text=self.saved.currentData() or "")
        if not ok:
            return
        existing = [r for r in chosen if Path(r.path).with_suffix(".nfo").exists()]
        overwrite = False
        if existing:
            answer = QMessageBox.question(self, "Export", f"{len(existing)} video(s) already have a .nfo file. "
                                          "Replace them?\n\nNo keeps those files and only writes the missing ones.",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                          | QMessageBox.StandardButton.Cancel)
            if answer == QMessageBox.StandardButton.Cancel:
                return
            overwrite = answer == QMessageBox.StandardButton.Yes
        written, problems = 0, []
        for rec in chosen:
            try:
                if L.write_nfo(rec, " ".join(collection.split()), overwrite):
                    written += 1
            except OSError as exc:
                problems.append(f"{Path(rec.path).name}: {exc}")
        QMessageBox.information(self, "Export", f"Wrote {written} .nfo file(s)."
                                + ("\n\n" + "\n".join(problems) if problems else ""))

    def closeEvent(self, event):  # noqa: N802
        self._closing = True
        self.settings.library_last_search = self.search.to_dict()
        if self._scan_job is not None:
            self._scan_job.cancel.set()   # it finishes the current file and stops; its callbacks see _closing
        self.settings.library_geometry = bytes(self.saveGeometry().toBase64()).decode()
        self.settings.save()
        super().closeEvent(event)


def _color(value: str):
    from PySide6.QtGui import QColor
    return QColor(value)


__all__ = ["LibraryWindow", "QPushButton"]
