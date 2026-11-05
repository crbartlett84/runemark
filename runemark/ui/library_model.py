"""The Library's table model (v0.3.0). Rows are FileRecords; cells are worked out only when Qt draws them,
so a search over thousands of videos doesn't create tens of thousands of table items."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from ..library import still_to_do  # noqa: F401 (also imported from here)
from ..media.probe import fmt_duration
from ..tagdb import FileRecord

# 0.9.0: the first column shows the video's title when it has one (else its file name); the file name has its own
COLUMNS = ["Video", "File name", "Year", "Rating", "Tags", "Thumbnail", "Length", "Folder", "People", "Studio",
           "Still to do"]
COL_TODO = 10

PATH_ROLE = Qt.ItemDataRole.UserRole


def stars(n: int) -> str:
    return "★" * n + "☆" * (5 - n) if n else ""


def shown_name(rec: FileRecord) -> str:
    """How a video is named in the Library: its title when it has one, else its file name (0.9.0)."""
    return (rec.title or "").strip() or Path(rec.path).name


def _text(rec: FileRecord, col: int) -> str:
    path = Path(rec.path)
    if col == 0:
        return shown_name(rec)
    if col == 1:
        return path.name
    if col == 2:
        return str(rec.year or "")
    if col == 3:
        return stars(rec.rating)
    if col == 4:
        return "; ".join(rec.tags)
    if col == 5:
        return "Yes" if rec.has_cover else "—"
    if col == 6:
        return fmt_duration(rec.duration) if rec.duration else ""
    if col == 8:
        return "; ".join(rec.people)
    if col == 9:
        return rec.studio
    if col == COL_TODO:
        return ", ".join(still_to_do(rec))
    return str(path.parent)


def _key(rec: FileRecord, col: int):
    """Sort key; None means blank (blanks sort after everything when ascending)."""
    path = Path(rec.path)
    if col == 0:
        return shown_name(rec).casefold()
    if col == 1:
        return path.name.casefold()
    if col == 2:
        return rec.year
    if col == 3:
        return rec.rating or None
    if col == 4:
        return len(rec.tags) or None
    if col == 5:
        return int(rec.has_cover)
    if col == 6:
        return rec.duration
    if col == 8:
        return "; ".join(rec.people).casefold() or None
    if col == 9:
        return rec.studio.casefold() or None
    if col == COL_TODO:
        return len(still_to_do(rec)) or None
    return str(path.parent).casefold()


class LibraryModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[FileRecord] = []
        self.faint = None                     # QColor for missing files (set by the window from the theme)
        self.covers: dict = {}                # path -> QPixmap (or None: no cover), for the grid
        self.want_covers = False
        self.request_cover = None             # callable(path): load a cover in the background
        self._sort: tuple[int, Qt.SortOrder] | None = None
        self.plugin_columns: list = []         # plugins.LibraryColumn, after the built-in columns (0.9.0)
        self.request_plugin_values = None      # callable(column, path): a plugin column has no value for it yet

    # ---- data
    def set_records(self, records: list[FileRecord]) -> None:
        self.beginResetModel()
        self.rows = list(records)
        if self._sort is not None:
            self._sort_rows(*self._sort)
        self.endResetModel()

    def set_cover(self, path: str, pixmap) -> None:
        self.covers[path] = pixmap
        for row, rec in enumerate(self.rows):
            if rec.path == path:
                idx = self.index(row, 0)
                self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])
                break

    def record(self, row: int) -> FileRecord:
        return self.rows[row]

    # ---- Qt model API
    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS) + len(self.plugin_columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            if section < len(COLUMNS):
                return COLUMNS[section]
            if section - len(COLUMNS) < len(self.plugin_columns):
                return self.plugin_columns[section - len(COLUMNS)].title
        return None

    def set_plugin_columns(self, columns: list) -> None:
        if [id(c) for c in columns] == [id(c) for c in self.plugin_columns]:
            return
        self.beginResetModel()
        self.plugin_columns = list(columns)
        self.endResetModel()

    def plugin_values_changed(self) -> None:
        if self.plugin_columns and self.rows:
            self.dataChanged.emit(self.index(0, len(COLUMNS)),
                                  self.index(len(self.rows) - 1, self.columnCount() - 1))

    def _plugin_cell(self, rec: FileRecord, col: int, role):
        column = self.plugin_columns[col - len(COLUMNS)]
        try:
            if role == Qt.ItemDataRole.ToolTipRole:
                return column.tooltip(rec.path) if column.tooltip else None
            value = column.cell(rec.path)
        except Exception:  # noqa: BLE001
            return None
        if value is None:
            if self.request_plugin_values is not None:
                self.request_plugin_values(column, rec.path)
            return ""
        return str(value)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        rec = self.rows[index.row()]
        col = index.column()
        if col >= len(COLUMNS):
            if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
                return self._plugin_cell(rec, col, role)
            if role == PATH_ROLE:
                return rec.path
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return _text(rec, col)
        if role == PATH_ROLE:
            return rec.path
        if role == Qt.ItemDataRole.DecorationRole and col == 0 and self.want_covers:
            if rec.path in self.covers:
                return self.covers[rec.path]
            if self.request_cover is not None:
                self.request_cover(rec.path)
            return None
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == 0:
                if rec.title and not rec.missing:
                    return f"{rec.path}\n{rec.title}" if rec.container != "mkv" else \
                        f"{rec.path}\nMKV: open it and save to convert it to MP4"
                if rec.missing:
                    return f"{rec.path}\nNot found: moved, renamed or deleted?"
                if rec.container == "mkv":
                    return f"{rec.path}\nMKV: open it and save to convert it to MP4"
                return rec.path
            if col == 4:
                return "\n".join(rec.tags)
            if col == 8 and rec.people_aliases:
                return "Also known as: " + ", ".join(rec.people_aliases)
        if role == Qt.ItemDataRole.ForegroundRole and rec.missing and self.faint is not None:
            return self.faint
        return None

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        self._sort = (column, order)
        self.layoutAboutToBeChanged.emit()
        self._sort_rows(column, order)
        self.layoutChanged.emit()

    def _sort_rows(self, column: int, order: Qt.SortOrder) -> None:
        """Blanks (no year, no rating, …) go last in both directions, so "highest rated first"
        starts with 5 stars rather than with every unrated video."""
        if column >= len(COLUMNS):
            plugin = self.plugin_columns[column - len(COLUMNS)] if column - len(COLUMNS) < len(self.plugin_columns) \
                else None

            def key(r):
                if plugin is None:
                    return None
                try:
                    value = plugin.sort_key(r.path) if plugin.sort_key else plugin.cell(r.path)
                except Exception:  # noqa: BLE001
                    return None
                return value if value not in (None, "") else None
            keyed = [(key(r), r) for r in self.rows]
        else:
            keyed = [(_key(r, column), r) for r in self.rows]
        values = [kr for kr in keyed if kr[0] is not None]
        blanks = [r for k, r in keyed if k is None]
        values.sort(key=lambda kr: kr[0], reverse=(order == Qt.SortOrder.DescendingOrder))
        self.rows = [r for _, r in values] + blanks
