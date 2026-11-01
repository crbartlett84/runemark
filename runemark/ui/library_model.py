"""The Library's table model (v0.3.0). Rows are FileRecords; cells are worked out only when Qt draws them,
so a search over thousands of videos doesn't create tens of thousands of table items."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from ..media.probe import fmt_duration
from ..tagdb import FileRecord

COLUMNS = ["Name", "Title", "Year", "Rating", "Tags", "Thumbnail", "Length", "Folder", "People"]
PATH_ROLE = Qt.ItemDataRole.UserRole


def stars(n: int) -> str:
    return "★" * n + "☆" * (5 - n) if n else ""


def _text(rec: FileRecord, col: int) -> str:
    path = Path(rec.path)
    if col == 0:
        return path.name
    if col == 1:
        return rec.title
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
    return str(path.parent)


def _key(rec: FileRecord, col: int):
    """Sort key; None means blank (blanks sort after everything when ascending)."""
    path = Path(rec.path)
    if col == 0:
        return path.name.casefold()
    if col == 1:
        return rec.title.casefold() or None
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
    return str(path.parent).casefold()


class LibraryModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[FileRecord] = []
        self.faint = None                     # QColor for missing files (set by the window from the theme)
        self._sort: tuple[int, Qt.SortOrder] | None = None

    # ---- data
    def set_records(self, records: list[FileRecord]) -> None:
        self.beginResetModel()
        self.rows = list(records)
        if self._sort is not None:
            self._sort_rows(*self._sort)
        self.endResetModel()

    def record(self, row: int) -> FileRecord:
        return self.rows[row]

    # ---- Qt model API
    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        rec = self.rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return _text(rec, col)
        if role == PATH_ROLE:
            return rec.path
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == 0:
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
        keyed = [(_key(r, column), r) for r in self.rows]
        values = [kr for kr in keyed if kr[0] is not None]
        blanks = [r for k, r in keyed if k is None]
        values.sort(key=lambda kr: kr[0], reverse=(order == Qt.SortOrder.DescendingOrder))
        self.rows = [r for _, r in values] + blanks
