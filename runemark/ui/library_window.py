"""The library window: every video in your folders, searchable by tag, changeable in bulk."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import (QByteArray, QItemSelection, QItemSelectionModel, QSize, QStringListModel, Qt, QTimer,
                            Signal)
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QCompleter, QDialog, QFileDialog, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton,
                               QTableView, QToolButton, QVBoxLayout)

from .. import library as L
from ..tagdb import FileRecord, TagDB, file_key
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
        self.text.setPlaceholderText("Search file names, titles, studios and people")
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
        # 0.9.0: the list opens wide enough for its longest entry ("Inbox · 1234", "Still to do …"), and the box
        # itself is never squeezed below a readable width
        self.filter.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.filter.setMinimumContentsLength(16)
        self.library_pick = QComboBox()
        self.library_pick.setToolTip("Which library to show. Each library is folders you choose; files you saved "
                                     "from anywhere else (Downloads…) only show under Everything else.")
        self.library_pick.activated.connect(self._library_picked)
        self.library_pick.setMinimumContentsLength(14)
        row.addWidget(self.library_pick, 1)
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
        self.smart_button = QToolButton()
        self.smart_button.setText("Smart filter…")
        self.smart_button.setToolTip("A saved filter with rules (tags, people, studio, rating, year…), all or any of "
                                     "them. Pinned filters sit above the list with how many videos match.")
        self.smart_button.clicked.connect(lambda: self._edit_smart_filter(None))
        chips_row.addWidget(self.smart_button)
        chips_row.addWidget(QLabel("Saved searches"))
        chips_row.addWidget(self.saved)
        chips_row.addWidget(self.save_search_button)
        layout.addLayout(chips_row)

        # ---- pinned smart filters (0.9.0), with live counts
        from PySide6.QtWidgets import QWidget
        from .widgets import FlowLayout
        self.pins_row = QWidget()
        self.pins = FlowLayout(self.pins_row, spacing=6)
        self.pins_row.hide()
        layout.addWidget(self.pins_row)
        self.active_smart: str | None = None

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
        # ---- grid: the same rows as cover pictures, sharing the table's selection
        from PySide6.QtWidgets import QListView, QStackedWidget
        self.grid = QListView()
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setWordWrap(True)
        self.grid.setIconSize(QSize(224, 126))
        self.grid.setGridSize(QSize(244, 176))
        self.grid.setSpacing(6)
        self.grid.setModel(self.model)
        self.grid.setModelColumn(0)
        self.grid.setSelectionModel(self.table.selectionModel())
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.grid.doubleClicked.connect(lambda *_: self._open())
        self.views = QStackedWidget()
        self.views.addWidget(self.table)
        self.views.addWidget(self.grid)
        layout.addWidget(self.views, 1)
        self._cover_queue: list[str] = []
        self._cover_busy = False
        self.model.request_cover = self._want_cover
        self._plugin_wanted: dict[int, tuple] = {}      # id(column) -> (column, set of paths waiting for a value)
        self._plugin_timer = QTimer(self)
        self._plugin_timer.setSingleShot(True)
        self._plugin_timer.setInterval(200)
        self._plugin_timer.timeout.connect(self._fetch_plugin_values)
        self.model.request_plugin_values = self._want_plugin_value
        self.plugin_columns_changed()

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
        self.view_button = text_button("Grid", "library")
        self.view_button.setToolTip("Switch between the list and a grid of thumbnails")
        self.view_button.clicked.connect(lambda: self._set_view("list" if self.views.currentIndex() else "grid"))
        self.scan_button = text_button("Scan for changes", "sync")
        self.scan_button.clicked.connect(self._scan_clicked)
        self.count = QLabel("")
        self.count.setObjectName("Faint")
        bottom.addWidget(self.folders_button)
        bottom.addWidget(self.scan_button)
        bottom.addWidget(self.view_button)
        bottom.addWidget(self.count, 1)
        self.open_button = text_button("Open", "open", primary=True)
        self.open_button.clicked.connect(self._open)
        self.add_tag_button = text_button("Add tag…", "plus")
        self.add_tag_button.clicked.connect(self._bulk_add)
        self.remove_tag_button = text_button("Remove tag…", "close")
        self.remove_tag_button.clicked.connect(self._bulk_remove)
        self.rating_button = text_button("Rate…", "star")
        self.rating_button.clicked.connect(self._bulk_rating)
        self.details_button = text_button("Details…", "edit")
        self.details_button.setToolTip("Set the studio, year or people of the selected videos")
        self.details_button.clicked.connect(self._bulk_details)
        more = QToolButton()
        more.setText("More")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more_menu = QMenu(more)
        more_menu.addAction("Export for Jellyfin / Kodi (.nfo)…", self._export_nfo)
        more_menu.addAction("Show in File Explorer", self._reveal)
        more_menu.addAction("Mark as done (out of the inbox)", lambda: self._mark_done(True))
        more_menu.addAction("Put back in the inbox", lambda: self._mark_done(False))
        more_menu.addAction("Find duplicates…", self.find_duplicates)
        more_menu.addAction("Health check…", self.health_check)
        more_menu.addAction("Save space (re-encode to HEVC)…", self.save_space)
        more_menu.addAction("Stats…", self.show_stats)
        more_menu.addSeparator()
        more_menu.addAction("Read details from file names…", lambda: self.file_names("read"))
        more_menu.addAction("Read details from .nfo files…", lambda: self.file_names("nfo"))
        more_menu.addAction("Rename from details…", lambda: self.file_names("rename"))
        more_menu.addAction("Undo last rename…", self._undo_rename)
        more_menu.addSeparator()
        more_menu.addAction("Undo last bulk change…", self._undo_bulk)
        more_menu.addSeparator()
        more_menu.addAction("Read every file again", lambda: self.scan(rescan_all=True))
        self.plugin_menu = more_menu.addMenu("Plugins")
        more_menu.aboutToShow.connect(self._fill_plugin_menu)
        more_menu.addAction("Remove selected from the library (files aren't touched)", self._forget)
        more.setMenu(more_menu)
        for b in (self.open_button, self.add_tag_button, self.remove_tag_button, self.rating_button,
                  self.details_button, more):
            bottom.addWidget(b)
        layout.addLayout(bottom)
        self._fill_saved()
        if settings.library_last_search:
            self._set_search(L.Search.from_dict(settings.library_last_search))
        self._add_shortcuts()
        self._set_view(getattr(settings, "library_view", "list"), save=False)
        self.refresh()

    def _add_shortcuts(self):
        """Keys that act on the selected rows while the list has focus (not while typing in the search boxes)."""
        def key(sequence, slot):
            for view in (self.table, self.grid):          # the list and the grid both
                sc = QShortcut(QKeySequence(sequence), view)
                sc.setContext(Qt.ShortcutContext.WidgetShortcut)
                sc.activated.connect(slot)
        for n in range(6):
            key(str(n), lambda n=n: self._rate_selected(n))
        key(Qt.Key.Key_Return, lambda: self._open())
        key(Qt.Key.Key_Enter, lambda: self._open())
        key("T", lambda: self._bulk_add())
        key("D", lambda: self._mark_done(True))
        tip = "Enter opens · 0-5 rates · T adds tags · D marks done (to the selected files)"
        self.table.setToolTip(tip)
        self.grid.setToolTip(tip)

    def _set_view(self, view: str, save: bool = True):
        grid = view == "grid"
        self.views.setCurrentIndex(1 if grid else 0)
        self.model.want_covers = grid
        self.view_button.setText("List" if grid else "Grid")
        if grid:
            self.model.layoutChanged.emit()          # ask for the covers now on screen
        if save and hasattr(self.settings, "library_view"):
            self.settings.library_view = view
            self.settings.save()

    def _remember_size(self):
        """Maximised or not (0.9.0: it opens maximised until you make it smaller), and the size when it isn't."""
        self.settings.library_maximized = self.isMaximized()
        if not self.isMaximized():
            self.settings.library_geometry = bytes(self.saveGeometry().toBase64()).decode()

    def show_window(self):
        """Show it maximised (the default) or at the size it last had."""
        if getattr(self.settings, "library_maximized", True):
            self.showMaximized()
        else:
            self.showNormal()

    def _want_cover(self, path: str):
        if path not in self._cover_queue:
            self._cover_queue.append(path)
        if not self._cover_busy:
            QTimer.singleShot(30, self._load_covers)

    def _load_covers(self):
        if self._cover_busy or not self._cover_queue or self._closing:
            return
        batch, self._cover_queue = self._cover_queue[:24], self._cover_queue[24:]
        self._cover_busy = True
        from .. import covers

        def work(job):
            return [(p, covers.thumbnail(p)) for p in batch]

        def done(results):
            self._cover_busy = False
            if self._closing:
                return
            from PySide6.QtGui import QPixmap
            for path, data in results:
                pix = None
                if data:
                    pix = QPixmap()
                    pix.loadFromData(data)
                self.model.set_cover(path, pix)
            if self._cover_queue:
                QTimer.singleShot(0, self._load_covers)

        def failed(_exc, _tb):
            self._cover_busy = False

        workers.start(work, on_done=done, on_error=failed)

    def _rate_selected(self, stars_: int):
        if self.selected_paths():
            self._run_bulk(L.BulkChange(rating=stars_))

    # ---------------------------------------------------------------- data
    # ---------------------------------------------------------------- plugin columns (0.9.0)
    def plugin_columns_changed(self):
        host = getattr(self.host, "plugins", None)
        self._sync_plugin_filters()
        if self._plugin_filter() is not None:
            self._apply()                      # a plugin filter is picked: its answers may have changed
        columns = list(host.library_columns) if host is not None else []
        if [id(c) for c in columns] != [id(c) for c in self.model.plugin_columns]:
            self.model.set_plugin_columns(columns)
            for n in range(len(columns)):
                self.table.setColumnWidth(len(COLUMNS) + n, 110)
        else:
            self.model.plugin_values_changed()

    # ---------------------------------------------------------------- plugin filters (0.12.0)
    def _plugin_filters(self) -> list:
        host = getattr(self.host, "plugins", None)
        return list(getattr(host, "library_filters", []) or []) if host is not None else []

    def _plugin_filter(self):
        key = self.search.filter or ""
        if not key.startswith("plugin:"):
            return None
        return next((f for f in self._plugin_filters() if f.key == key), None)

    def _sync_plugin_filters(self):
        """The filter list: Runemark's own, then a line, then the plugins' (added once each)."""
        have = {self.filter.itemData(i) for i in range(self.filter.count())}
        new = [f for f in self._plugin_filters() if f.key not in have]
        if not new:
            return
        self.filter.blockSignals(True)
        if not any(str(d or "").startswith("plugin:") for d in have):
            self.filter.insertSeparator(self.filter.count())
        for f in new:
            self.filter.addItem(f.label, f.key)
        at = self.filter.findData(self.search.filter)
        if at >= 0:
            self.filter.setCurrentIndex(at)
        self.filter.blockSignals(False)

    def _prepare_plugin_filter(self):
        f = self._plugin_filter()
        if f is None or f.prepare is None:
            return
        paths = [str(r.path) for r in self._in_view(self.records)]
        try:
            f.prepare(paths)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("plugin filter %s couldn't prepare", f.label)

    def _plugin_filter_matches(self, f, path: str) -> bool:
        try:
            return f.test(path) is True
        except Exception:  # noqa: BLE001
            return False

    def _want_plugin_value(self, column, path: str):
        if column.fetch is None:
            return
        entry = self._plugin_wanted.setdefault(id(column), (column, set()))
        entry[1].add(path)
        if not self._plugin_timer.isActive():
            self._plugin_timer.start()

    def _fetch_plugin_values(self):
        wanted, self._plugin_wanted = self._plugin_wanted, {}
        for column, paths in wanted.values():
            batch = sorted(paths)
            for start in range(0, len(batch), 100):
                try:
                    column.fetch(batch[start:start + 100])
                except Exception:  # noqa: BLE001
                    import logging
                    logging.getLogger(__name__).exception("plugin column %s couldn't fetch", column.title)

    # ---------------------------------------------------------------- smart filters (0.9.0)
    def _smart_filters(self) -> list[L.SmartFilter]:
        out = []
        for item in getattr(self.settings, "smart_filters", []) or []:
            try:
                out.append(L.SmartFilter.from_dict(item))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def _save_smart_filters(self, filters: list[L.SmartFilter]):
        self.settings.smart_filters = [f.to_dict() for f in filters]
        self.settings.save()

    def _show_pins(self, in_view: list[FileRecord]):
        from PySide6.QtWidgets import QToolButton as _Button
        self.pins.clear()
        pinned = [f for f in self._smart_filters() if f.pinned]
        for sf in pinned:
            count = sum(1 for r in in_view if L.smart_matches(r, sf))
            button = _Button()
            button.setObjectName("SmartPin")
            button.setCheckable(True)
            button.setChecked(sf.name == self.active_smart)
            button.setText(f"{sf.name} · {count}")
            button.setToolTip(sf.describe() + "\n\nClick to show only these; click again to show everything. "
                              "Right-click to change, unpin or delete it.")
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(lambda _=False, n=sf.name: self._toggle_smart(n))
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(lambda _pos, n=sf.name, b=button: self._smart_menu(n, b))
            self.pins.addWidget(button)
        unpinned = [f for f in self._smart_filters() if not f.pinned]
        if unpinned:
            more = _Button()
            more.setText("More filters")
            more.setPopupMode(_Button.ToolButtonPopupMode.InstantPopup)
            menu = QMenu(more)
            for sf in unpinned:
                count = sum(1 for r in in_view if L.smart_matches(r, sf))
                act = menu.addAction(f"{sf.name} · {count}", lambda n=sf.name: self._toggle_smart(n))
                act.setCheckable(True)
                act.setChecked(sf.name == self.active_smart)
            more.setMenu(menu)
            self.pins.addWidget(more)
        self.pins_row.setVisible(bool(pinned or unpinned))

    def _toggle_smart(self, name: str):
        self.active_smart = None if self.active_smart == name else name
        self._apply()

    def _smart_menu(self, name: str, button):
        menu = QMenu(self)
        menu.addAction("Change…", lambda: self._edit_smart_filter(name))
        menu.addAction("Unpin (keep it under More filters)", lambda: self._pin_smart(name, False))
        menu.addAction("Delete", lambda: self._delete_smart(name))
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def _pin_smart(self, name: str, pinned: bool):
        filters = self._smart_filters()
        for f in filters:
            if f.name == name:
                f.pinned = pinned
        self._save_smart_filters(filters)
        self._apply()

    def _delete_smart(self, name: str):
        if QMessageBox.question(self, "Delete smart filter", f"Delete the smart filter “{name}”? Videos aren't "
                                "changed.") != QMessageBox.StandardButton.Yes:
            return
        self._save_smart_filters([f for f in self._smart_filters() if f.name != name])
        if self.active_smart == name:
            self.active_smart = None
        self._apply()

    def _edit_smart_filter(self, name: str | None):
        from .smart_filter_dialog import SmartFilterDialog
        filters = self._smart_filters()
        current = next((f for f in filters if f.name == name), None)
        names = {"tag": [t.name for t in self.db.tags()], "person": [p.name for p in self.db.people()],
                 "studio": self.db.studios()}
        dialog = SmartFilterDialog(current, names, self.records, [f.name for f in filters if f.name != name], self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        result = dialog.value()
        filters = [f for f in filters if f.name != name] + [result]
        self._save_smart_filters(filters)
        self.active_smart = result.name
        self._apply()

    def refresh(self):
        self.tag_model.setStringList([t.name for t in self.db.tags()])
        self.records = self.db.library()
        self._fill_library_pick()
        self._prepare_plugin_filter()
        self._apply()

    def _fill_plugin_menu(self):
        """Actions plugins added for the Library; each gets the selected videos."""
        self.plugin_menu.clear()
        host = getattr(self.host, "plugins", None)
        actions = list(host.library_actions) if host is not None else []
        for act in actions:
            self.plugin_menu.addAction(act.text, lambda a=act: self._run_library_plugin(a))
        self.plugin_menu.menuAction().setVisible(bool(actions))

    def _run_library_plugin(self, act):
        paths = [Path(p) for p in self.selected_paths()]
        try:
            act.callback(paths)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Plugin", f"{act.plugin} hit a problem:\n\n{exc}")

    # ---------------------------------------------------------------- libraries (0.7.0)
    # The picker (always shown, 0.8.1): each library by name, "All libraries" (only files inside one of them), and
    # "Everything else" for files saved from outside every library (a Downloads folder, a USB stick…), so those never
    # clutter a library. "New library…" sits at the bottom of the list.
    SHOW_ALL = ""            # inside any library
    SHOW_OUTSIDE = "*outside"
    NEW_LIBRARY = "*new"

    def _fit_popups(self):
        """Every drop-down list as wide as its longest entry, so nothing in it is cut off (0.9.0)."""
        for combo in (self.filter, self.library_pick, self.saved):
            view = combo.view()
            metrics = view.fontMetrics()
            widest = max((metrics.horizontalAdvance(combo.itemText(i)) for i in range(combo.count())), default=0)
            view.setMinimumWidth(widest + 48)

    def _fill_library_pick(self):
        self.library_pick.blockSignals(True)
        self.library_pick.clear()
        names = [lib["name"] for lib in self.settings.libraries]
        for name in names:
            self.library_pick.addItem(name, name)
        if len(names) != 1:
            self.library_pick.addItem("All libraries", self.SHOW_ALL)
        outside = sum(1 for r in getattr(self, "records", []) if not self._in_any_library(r.path))
        self.library_pick.addItem(f"Everything else (saved from other folders) · {outside}", self.SHOW_OUTSIDE)
        self.library_pick.insertSeparator(self.library_pick.count())
        self.library_pick.addItem("New library…", self.NEW_LIBRARY)
        shown = self.settings.library_shown
        if shown not in (self.SHOW_ALL, self.SHOW_OUTSIDE) and L.library_named(self.settings, shown) is None:
            shown = self.SHOW_ALL
        if shown == self.SHOW_ALL and len(names) == 1:
            shown = names[0]
        self.library_pick.setCurrentIndex(max(0, self.library_pick.findData(shown)))
        self.library_pick.blockSignals(False)

    def _library_picked(self, _index: int):
        data = self.library_pick.currentData()
        if data == self.NEW_LIBRARY:
            self._fill_library_pick()          # back to what was shown, then ask
            self._new_library()
            return
        self.settings.library_shown = data or self.SHOW_ALL
        self.settings.save()
        self._apply()

    def _shown_library(self) -> dict | None:
        shown = self.settings.library_shown
        if shown in (self.SHOW_ALL, self.SHOW_OUTSIDE):
            return None
        return L.library_named(self.settings, shown)

    def _in_any_library(self, path: str) -> bool:
        return any(L.in_library(path, lib) for lib in self.settings.libraries)

    def _in_view(self, records: list[FileRecord]) -> list[FileRecord]:
        if self.settings.library_shown == self.SHOW_OUTSIDE:
            return [r for r in records if not self._in_any_library(r.path)]
        lib = self._shown_library()
        if lib is not None:
            return [r for r in records if L.in_library(r.path, lib)]
        if not any(lib.get("folders") for lib in self.settings.libraries):
            return records                     # no library folders yet: show what there is
        return [r for r in records if self._in_any_library(r.path)]

    def _apply(self):
        self._search_timer.stop()
        keep = {str(p).casefold() for p in self.selected_paths()}  # still selected after a change or rescan
        in_view = self._in_view(self.records)
        pf = self._plugin_filter()
        if pf is not None:                     # 0.12.0: a plugin's filter, with the search's tags and text
            base = L.Search(self.search.tags, self.search.text, L.FILTER_ALL)
            shown = [r for r in in_view if L.matches(r, base) and self._plugin_filter_matches(pf, str(r.path))]
        else:
            shown = [r for r in in_view if L.matches(r, self.search)]
        active = next((f for f in self._smart_filters() if f.name == self.active_smart), None)
        if active is None:
            self.active_smart = None
        else:
            shown = [r for r in shown if L.smart_matches(r, active)]
        self._show_pins(in_view)
        self.model.faint = _color(theme.current().faint)
        self.model.set_records(shown)
        if keep:
            self._select_paths(keep)
        total = len(in_view)
        self.count.setText(f"{len(shown)} of {total} video{'s' if total != 1 else ''}")
        inbox = sum(1 for r in in_view if r.in_inbox)
        at = self.filter.findData(L.FILTER_INBOX)
        if at >= 0:
            self.filter.setItemText(at, f"Inbox · {inbox}" if inbox else "Inbox (empty)")
        gaps = sum(1 for r in in_view if L.matches(r, L.Search(filter=L.FILTER_GAPS)))
        at = self.filter.findData(L.FILTER_GAPS)
        if at >= 0:
            self.filter.setItemText(at, f"Still to do · {gaps}" if gaps else "Still to do (nothing left)")
        self._fit_popups()
        if not total and self.settings.library_shown == self.SHOW_OUTSIDE:
            self.empty.setText("Nothing here: every video Runemark knows is inside one of your libraries.")
            self.empty.show()
        elif not total:
            self.empty.setText("This library is empty. Use Folders → Add a folder… to give it a folder of videos. "
                               "Videos you save from other folders show under Everything else.")
            self.empty.show()
        elif not shown and self.search.filter == L.FILTER_INBOX and not self.search.tags and not self.search.text:
            self.empty.setText("The inbox is empty: every video has been saved in Runemark or marked done. New "
                               "videos land here after a scan.")
            self.empty.show()
        elif not shown and self.search.filter == L.FILTER_GAPS and not self.search.tags and not self.search.text:
            self.empty.setText("Nothing left to do: every video you've saved has tags, a genre, people, a thumbnail, "
                               "chapters and a studio.")
            self.empty.show()
        elif not shown and self.active_smart:
            self.empty.setText(f"Nothing here matches the smart filter “{self.active_smart}” (and the search).")
            self.empty.show()
        elif not shown:
            self.empty.setText("Nothing matches this search.")
            self.empty.show()
        else:
            self.empty.hide()
        self._selection_changed()

    def _select_paths(self, folded_paths: set[str]) -> None:
        selection = QItemSelection()
        last = self.model.columnCount() - 1
        for row, rec in enumerate(self.model.rows):
            if rec.path.casefold() in folded_paths:
                selection.select(self.model.index(row, 0), self.model.index(row, last))
        flags = QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows
        self.table.selectionModel().select(selection, flags)

    def selected_paths(self) -> list[Path]:
        # rows with any cell selected: the grid selects only the first column
        rows = sorted({i.row() for i in self.table.selectionModel().selectedIndexes()})
        return [Path(self.model.record(r).path) for r in rows if r < self.model.rowCount()]

    def _selection_changed(self):
        n = len(self.selected_paths())
        for b in (self.open_button, self.add_tag_button, self.remove_tag_button, self.rating_button,
                  self.details_button):
            b.setEnabled(n > 0)

    # ---------------------------------------------------------------- search
    def _text_changed(self, text: str):
        self.search.text = text
        self._search_timer.start()          # restarts on every key; _apply runs after the pause

    def _filter_changed(self, *_):
        self.search.filter = self.filter.currentData()
        self._prepare_plugin_filter()
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
        self._prepare_plugin_filter()
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
        """One section per library: its folders, the subfolders it leaves out, and what can be done with it."""
        menu = self.folders_menu
        menu.clear()
        libs = self.settings.libraries
        if not libs:
            menu.addAction(theme.icon("folder_plus", size=16), "Add a folder…", lambda: self._add_folder(None))
        for lib in libs:
            sub = menu.addMenu(theme.icon("folder", size=16), lib["name"]) if len(libs) > 1 else menu
            sub.addAction(theme.icon("folder_plus", size=16), "Add a folder…", lambda n=lib["name"]: self._add_folder(n))
            for folder in lib["folders"]:
                fm = sub.addMenu(folder)
                fm.addAction("Remove from this library (files aren't touched)",
                             lambda n=lib["name"], f=folder: self._remove_folder(n, f))
            sub.addSeparator()
            sub.addAction("Leave out a subfolder…", lambda n=lib["name"]: self._add_skip(n))
            for skip in lib["skip"]:
                sub.addAction(f"Include again: {skip}", lambda n=lib["name"], f=skip: self._remove_skip(n, f))
            sub.addSeparator()
            sub.addAction("Rename library…", lambda n=lib["name"]: self._rename_library(n))
            if len(libs) > 1:
                sub.addAction("Delete library (files aren't touched)", lambda n=lib["name"]: self._delete_library(n))
        menu.addSeparator()
        menu.addAction("New library…", self._new_library)

    def _library(self, name: str | None) -> dict:
        """The named library, or the only/first one, created ("Library") when there's none yet."""
        lib = L.library_named(self.settings, name) if name else None
        if lib is None:
            if not self.settings.libraries:
                self.settings.libraries.append({"name": "Library", "folders": [], "skip": []})
            lib = self.settings.libraries[0]
        return lib

    def _add_folder(self, name: str | None = None):
        folder = QFileDialog.getExistingDirectory(self, "Add a folder of videos", self.settings.last_folder or "")
        if not folder:
            return
        folder = str(Path(folder))
        lib = self._library(name)
        if folder.casefold() not in {f.casefold() for f in lib["folders"]}:
            lib["folders"].append(folder)
            self.settings.save()
        self.scan()

    def _remove_folder(self, name: str, folder: str):
        answer = QMessageBox.question(self, "Remove folder", f"Stop including {folder} in {name}?\n\n"
                                      "The videos stay where they are, with their tags.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        lib = self._library(name)
        lib["folders"] = [f for f in lib["folders"] if f != folder]
        self.settings.save()
        if not any(L.in_library(os.path.join(folder, "x"), other) for other in self.settings.libraries):
            self.db.forget_folder(folder)          # no other library has it: drop its videos from the list
        self.refresh()

    def _add_skip(self, name: str):
        lib = self._library(name)
        start = lib["folders"][0] if lib["folders"] else ""
        folder = QFileDialog.getExistingDirectory(self, f"A subfolder to leave out of {name}", start)
        if not folder:
            return
        folder = str(Path(folder))
        if not any(L._inside(folder, f) for f in lib["folders"]):
            QMessageBox.information(self, "Leave out a subfolder", f"{folder} isn't inside one of {name}'s folders.")
            return
        if folder.casefold() not in {f.casefold() for f in lib["skip"]}:
            lib["skip"].append(folder)
            self.settings.save()
        self.refresh()

    def _remove_skip(self, name: str, folder: str):
        lib = self._library(name)
        lib["skip"] = [f for f in lib["skip"] if f != folder]
        self.settings.save()
        self.scan()

    def _ask_library_name(self, title: str, current: str = "") -> str | None:
        from PySide6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(self, title, "Name:", text=current)
        text = " ".join(text.split())
        if not ok or not text:
            return None
        if any(lib["name"].casefold() == text.casefold() for lib in self.settings.libraries
               if lib["name"].casefold() != current.casefold()):
            QMessageBox.information(self, title, f"There's already a library called {text}.")
            return None
        return text

    def _new_library(self):
        name = self._ask_library_name("New library")
        if name:
            self.settings.libraries.append({"name": name, "folders": [], "skip": []})
            self.settings.library_shown = name          # show the new library straight away
            self.settings.save()
            self._add_folder(name)
            self.refresh()

    def _rename_library(self, name: str):
        new = self._ask_library_name("Rename library", name)
        if new:
            self._library(name)["name"] = new
            if self.settings.library_shown.casefold() == name.casefold():
                self.settings.library_shown = new
            self.settings.save()
            self.refresh()

    def _delete_library(self, name: str):
        answer = QMessageBox.question(self, "Delete library", f"Delete the library {name}?\n\n"
                                      "Only the library goes: the videos and their tags stay where they are.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.settings.libraries = [lib for lib in self.settings.libraries if lib["name"] != name]
        if self.settings.library_shown.casefold() == name.casefold():
            self.settings.library_shown = ""
        self.settings.save()
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
        skip = L.scan_skips(self.settings)
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
                return L.scan(db, folders, progress=job.report, cancel=job.cancel, rescan_all=rescan_all, skip=skip)
            finally:
                db.close()

        self._scan_job = workers.start(work, on_done=done, on_error=failed, on_progress=progress)

    def db_for_thread(self) -> TagDB:
        """SQLite connections belong to one thread: background work opens its own."""
        return TagDB(self.db.path)

    # ---------------------------------------------------------------- actions
    def _open(self):
        paths = [p for p in self.selected_paths() if p.exists()]
        if len(paths) == 1 and self.search.filter == L.FILTER_INBOX:
            # one video picked in the inbox: open the rest of the inbox after it, so Save and next
            # (Ctrl+Enter) works straight through it
            shown = [Path(self.model.record(r).path) for r in range(self.model.rowCount())]
            shown = [p for p in shown if p.exists()]
            at = next((i for i, p in enumerate(shown) if str(p).casefold() == str(paths[0]).casefold()), 0)
            paths = shown[at:] + shown[:at]
        if paths:
            self.openRequested.emit(paths)
            self._step_aside()

    def _step_aside(self):
        """After opening a video, get out of the way so the main window shows it (0.8.1). Hidden rather than closed,
        so a scan keeps running and the Library comes back as it was (same search, same selection)."""
        self.settings.library_last_search = self.search.to_dict()
        self._remember_size()
        self.settings.save()
        self.hide()
        host = self.parent()
        if host is not None:
            host.raise_()
            host.activateWindow()

    def _mark_done(self, done: bool):
        paths = self.selected_paths()
        if paths:
            self.db.set_inbox_done(paths, done)
            self.refresh()

    def find_duplicates(self):
        from .duplicates_window import DuplicatesWindow
        window = DuplicatesWindow(self.db, self.settings, host=self.host, parent=self)
        window.openRequested.connect(self.openRequested)
        window.changed.connect(self.refresh)
        window.exec()
        self.refresh()

    def health_check(self):
        from .health_dialog import HealthDialog
        records = [r for r in self.records if L.matches(r, self.search)] if self.search.filter != L.FILTER_ALL \
            or self.search.tags or self.search.text else list(self.records)
        dialog = HealthDialog(self.db, records, self.settings, host=self.host, parent=self)
        dialog.openRequested.connect(self.openRequested)
        dialog.changed.connect(self.refresh)
        dialog.show()
        dialog.run_check()
        dialog.exec()
        self.refresh()

    def save_space(self):
        from .spacesaver_dialog import SpaceSaverDialog
        records = self._targets()
        if not records:
            return
        dialog = SpaceSaverDialog(self.db, records, host=self.host, parent=self)
        dialog.openRequested.connect(self.openRequested)
        dialog.changed.connect(self.refresh)
        dialog.show()
        dialog.assess()
        dialog.exec()
        self.refresh()

    def show_stats(self):
        from .stats_dialog import StatsDialog
        StatsDialog(self.db.library(), self).exec()

    def _targets(self) -> list:
        """The selected videos, or every video shown when nothing is selected."""
        chosen = {str(p).casefold() for p in self.selected_paths()}
        shown = [self.model.record(r) for r in range(self.model.rowCount())]
        return [r for r in shown if not r.missing and (not chosen or r.path.casefold() in chosen)]

    def file_names(self, mode: str):
        from .names_dialog import NamesDialog
        records = self._targets()
        if not records:
            return
        dialog = NamesDialog(self.db, records, mode, self.settings, host=self.host, parent=self)
        dialog.exec()
        if dialog.applied:
            self.refresh()
            if self.host is not None and mode in ("read", "nfo"):
                self.host.files_changed([Path(r.path) for r in records])
                self._tell_saved([Path(r.path) for r in records], [])
            elif self.host is not None and dialog.moved and hasattr(self.host, "files_moved"):
                self.host.files_moved(dialog.moved)

    def _undo_rename(self):
        from .. import names as N
        rows = self.db.renames()
        if not rows:
            QMessageBox.information(self, "Undo last rename", "There's no rename to undo.")
            return
        listing = "\n".join(f"• {Path(new).name} → {Path(old).name}" for old, new in rows[:12])
        more = f"\n…and {len(rows) - 12} more" if len(rows) > 12 else ""
        if QMessageBox.question(self, "Undo last rename", f"Put {len(rows)} video(s) back?\n\n{listing}{more}") \
                != QMessageBox.StandardButton.Yes:
            return

        def go():
            problems = N.undo_renames(self.db)
            self.refresh()
            if self.host is not None and hasattr(self.host, "files_moved"):
                self.host.files_moved([(new, old) for old, new in rows])
            if problems:
                TextReportDialog("Some videos weren't put back",
                                 "\n".join(f"{Path(p).name}: {why}" for p, why in problems), self).exec()

        if self.host is not None:
            self.host.release_for([Path(new) for _old, new in rows], go)
        else:
            go()

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

    def _bulk_details(self):
        from .. import names as N
        paths = [p for p in self.selected_paths() if p.suffix.lower() in (".mp4", ".m4v")]
        skipped = len(self.selected_paths()) - len(paths)
        if not paths:
            QMessageBox.information(self, "Details", "Select some MP4 videos first (MKVs need converting first).")
            return
        dialog = BulkDetailsDialog(self.db, len(paths), skipped, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        studio, year, people = dialog.values()
        if not (studio or year or people):
            return
        plans = [N.ReadPlan(str(p), year=year, studio=studio or None, people=list(people), matched=True)
                 for p in paths]
        self.host.release_for(paths, lambda: self._start_details(paths, plans))

    def _start_details(self, paths: list[Path], plans):
        from .. import names as N
        dialog = ProgressDialog("Updating files", cancellable=True, parent=self)

        def work(job):
            db = self.db_for_thread()
            try:
                return N.apply_read(db, plans, overwrite=True, create_people=True, progress=job.report,
                                    cancel=job.cancel, label="Library: set details")
            finally:
                db.close()

        def done(problems):
            dialog.finish()
            self.refresh()
            self.host.files_changed(paths)
            self._tell_saved(paths, problems)
            if problems:
                TextReportDialog("Some files weren't changed",
                                 "\n".join(f"{Path(p).name}: {why}" for p, why in problems), self).exec()

        def failed(exc, _tb):
            dialog.finish()
            self.refresh()
            QMessageBox.critical(self, "Details", str(exc))

        job = workers.start(work, on_done=done, on_error=failed, on_progress=dialog.update_progress)
        dialog.cancelRequested.connect(job.cancel.set)
        dialog.show()

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
            self._tell_saved(paths, problems)
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

    def _tell_saved(self, paths, problems):
        """Plugins hear file_saved for each video a bulk change or undo wrote (0.10.1)."""
        if hasattr(self.host, "files_saved"):
            failed = {file_key(p) for p, _why in problems or []}
            self.host.files_saved([p for p in paths if file_key(p) not in failed])

    def _run_bulk(self, change: L.BulkChange):
        paths = self.selected_paths()
        if not paths:
            return
        mkv = [p for p in paths if p.suffix.lower() == ".mkv"]
        if len(paths) > 1:
            answer = QMessageBox.question(self, "Change files", f"{L.sentence_case(change.describe())} on "
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
            self._tell_saved(paths, problems)
            if problems:
                TextReportDialog("Some files weren't changed",
                                 "\n".join(f"{p.name}: {why}" for p, why in problems), self).exec()

        def failed(exc, _tb):
            dialog.finish()
            self.refresh()
            self.host.files_changed(paths)
            self._tell_saved(paths, [])
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
        self._remember_size()
        self.settings.save()
        super().closeEvent(event)


def _color(value: str):
    from PySide6.QtGui import QColor
    return QColor(value)


__all__ = ["LibraryWindow", "QPushButton"]


class BulkDetailsDialog(QDialog):
    """Studio, year and people for many videos at once. Blank fields are left alone; people are added."""

    def __init__(self, db: TagDB, count: int, skipped: int, parent=None):
        super().__init__(parent)
        from PySide6.QtGui import QIntValidator
        from PySide6.QtWidgets import QDialogButtonBox, QFormLayout
        self.setWindowTitle("Details")
        layout = QVBoxLayout(self)
        intro = QLabel(f"Set these on {count} video(s). Blank fields are left as they are; people are added to "
                       "those already there (new names are added to your People list). One undoable change."
                       + (f"\n{skipped} MKV file(s) are skipped: convert them first." if skipped else ""))
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.studio = QLineEdit()
        self.studio.setCompleter(_completer(db.studios(), self.studio))
        self.year = QLineEdit()
        self.year.setValidator(QIntValidator(1800, 2199, self.year))
        self.year.setMaximumWidth(80)
        self.people = QLineEdit()
        self.people.setPlaceholderText("Names, separated by commas")
        names = [p.name for p in db.people()] if hasattr(db, "people") else []
        self.people.setCompleter(_completer(names, self.people))
        form.addRow("Studio", self.studio)
        form.addRow("Year", self.year)
        form.addRow("Add people", self.people)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[str, int | None, list[str]]:
        text = self.year.text().strip()
        year = int(text) if text.isdigit() and len(text) == 4 else None
        people = [" ".join(n.split()) for n in self.people.text().split(",") if n.strip()]
        return " ".join(self.studio.text().split()), year, people


def _completer(names: list[str], parent) -> QCompleter:
    completer = QCompleter(QStringListModel(names, parent), parent)
    completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    completer.setFilterMode(Qt.MatchFlag.MatchContains)
    return completer
