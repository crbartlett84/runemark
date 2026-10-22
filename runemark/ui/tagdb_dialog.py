"""Managing the tag database: categories, subcategories, tags, Genre flags, import and export."""
from __future__ import annotations

import json
from dataclasses import dataclass

from PySide6.QtCore import QMimeData, QSize, Qt, Signal
from PySide6.QtGui import QDrag, QFont
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog,
                               QLabel, QLineEdit, QMessageBox, QSplitter, QTableWidget,
                               QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..tagdb import DuplicateName, TagDB, TagDBError
from . import theme
from .widgets import icon_button, text_button

MIME_TAGS = "application/x-runemark-tag-ids"
MIME_CATEGORY = "application/x-runemark-category-id"
ALL = "all"
UNCATEGORIZED = "uncategorized"


@dataclass
class FileChange:
    """A database edit that should be carried into video files Runemark has tagged."""
    kind: str                 # rename, remove, genre
    files: list[str]
    old: str = ""
    new: str = ""
    description: str = ""


def category_choices(db: TagDB) -> list[tuple[str, int | None]]:
    """(indented label, id) for every category, Uncategorized first."""
    out: list[tuple[str, int | None]] = [("Uncategorized", None)]

    def walk(parent_id, depth):
        for cat in db.children(parent_id):
            out.append(("    " * depth + cat.name, cat.id))
            walk(cat.id, depth + 1)

    walk(None, 0)
    return out


class NewTagDialog(QDialog):
    def __init__(self, db: TagDB, name: str = "", category_id: int | None = None, *, fixed_name: bool = False,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add to tag database" if fixed_name else "New tag")
        self.db = db
        layout = QVBoxLayout(self)
        intro = QLabel("Tags are written to the video. The category only organises your list here.")
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.name = QLineEdit(name)
        self.name.setReadOnly(fixed_name)
        self.category = QComboBox()
        for label, cid in category_choices(db):
            self.category.addItem(label, cid)
        index = self.category.findData(category_id)
        self.category.setCurrentIndex(max(0, index))
        form.addRow("Tag", self.name)
        form.addRow("Category", self.category)
        layout.addLayout(form)
        self.genre_note = QLabel("")
        self.genre_note.setObjectName("Faint")
        layout.addWidget(self.genre_note)
        self.category.currentIndexChanged.connect(self._update_note)
        self._update_note()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Add tag")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("Primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(420, self.sizeHint().height())

    def _update_note(self):
        cid = self.category.currentData()
        self.genre_note.setText("Tags in this category are also written to Genre." if cid is not None and
                                cid in self.db.genre_categories() else "")

    def values(self) -> tuple[str, int | None]:
        return self.name.text(), self.category.currentData()


def ask_new_tag(parent, db: TagDB, name: str, category_id: int | None, fixed_name: bool = False):
    """Create a tag through a small dialog. Returns (Tag, category_id) or (None, None)."""
    dialog = NewTagDialog(db, name, category_id, fixed_name=fixed_name, parent=parent)
    while dialog.exec() == QDialog.DialogCode.Accepted:
        tag_name, cid = dialog.values()
        try:
            return db.add_tag(tag_name, cid), cid
        except DuplicateName as exc:
            if fixed_name and exc.existing_id is not None:
                return db.tag(exc.existing_id), cid
            QMessageBox.warning(parent, "Tag exists", str(exc))
        except TagDBError as exc:
            QMessageBox.warning(parent, "Can't add tag", str(exc))
    return None, None


def pick_category(parent, db: TagDB, title: str, current: int | None, allow_top: bool = True,
                  exclude: set[int] | None = None) -> tuple[bool, int | None]:
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    layout = QVBoxLayout(dialog)
    combo = QComboBox()
    for label, cid in category_choices(db):
        if exclude and cid in exclude:
            continue
        if cid is None:
            label = "Top level" if not allow_top else "Uncategorized / top level"
        combo.addItem(label, cid)
    combo.setCurrentIndex(max(0, combo.findData(current)))
    layout.addWidget(combo)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    dialog.resize(360, dialog.sizeHint().height())
    if dialog.exec() == QDialog.DialogCode.Accepted:
        return True, combo.currentData()
    return False, None


class CategoryTree(QTreeWidget):
    tagsDropped = Signal(list, object)       # tag ids, category id (None = uncategorized)
    categoryDropped = Signal(int, object)    # category id, new parent (None = top level)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

    def startDrag(self, actions):  # noqa: N802
        item = self.currentItem()
        if item is None or not isinstance(item.data(0, Qt.ItemDataRole.UserRole), int):
            return
        mime = QMimeData()
        mime.setData(MIME_CATEGORY, str(item.data(0, Qt.ItemDataRole.UserRole)).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasFormat(MIME_TAGS) or event.mimeData().hasFormat(MIME_CATEGORY):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):  # noqa: N802
        item = self.itemAt(event.position().toPoint())
        target = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if item is not None and target != ALL:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):  # noqa: N802
        item = self.itemAt(event.position().toPoint())
        if item is None:
            event.ignore()
            return
        target = item.data(0, Qt.ItemDataRole.UserRole)
        category_id = None if target == UNCATEGORIZED else target
        if target == ALL:
            event.ignore()
            return
        data = event.mimeData()
        if data.hasFormat(MIME_TAGS):
            ids = json.loads(bytes(data.data(MIME_TAGS)).decode())
            self.tagsDropped.emit(ids, category_id)
        elif data.hasFormat(MIME_CATEGORY):
            self.categoryDropped.emit(int(bytes(data.data(MIME_CATEGORY)).decode()), category_id)
        event.acceptProposedAction()


class TagTable(QTableWidget):
    def __init__(self, parent=None):
        super().__init__(0, 3, parent)
        self.setHorizontalHeaderLabels(["Tag", "Category", "Files"])
        self.verticalHeader().hide()
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setDragEnabled(True)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

    def selected_ids(self) -> list[int]:
        rows = sorted({i.row() for i in self.selectedIndexes()})
        return [self.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in rows if self.item(r, 0)]

    def startDrag(self, actions):  # noqa: N802
        ids = self.selected_ids()
        if not ids:
            return
        mime = QMimeData()
        mime.setData(MIME_TAGS, json.dumps(ids).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)


class TagDatabaseDialog(QDialog):
    changed = Signal()                 # database edited (refresh the tag editor)
    fileChanges = Signal(object)       # FileChange to apply to tagged files

    def __init__(self, db: TagDB, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Tag database")
        self.resize(900, 600)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        intro = QLabel("Categories keep your tags organised here and are never written to files. "
                       "Drag tags onto a category to move them, or drag a category into another.")
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.tree = CategoryTree()
        self.tree.currentItemChanged.connect(lambda *_: self._category_selected())
        self.tree.tagsDropped.connect(self._drop_tags)
        self.tree.categoryDropped.connect(self._drop_category)
        left_layout.addWidget(self.tree, 1)
        cat_buttons = QHBoxLayout()
        self.new_cat = icon_button("folder_plus", "New category at the top level")
        self.new_sub = icon_button("plus", "New subcategory inside the selected one")
        self.rename_cat = icon_button("edit", "Rename category")
        self.move_cat = icon_button("merge", "Move category into another")
        self.delete_cat = icon_button("trash", "Delete category (its tags move up a level)")
        for button, slot in ((self.new_cat, self._new_category), (self.new_sub, self._new_subcategory),
                             (self.rename_cat, self._rename_category), (self.move_cat, self._move_category),
                             (self.delete_cat, self._delete_category)):
            button.clicked.connect(slot)
            cat_buttons.addWidget(button)
        cat_buttons.addStretch(1)
        left_layout.addLayout(cat_buttons)
        self.genre_box = QCheckBox("Also write tags in this category to Genre")
        self.genre_box.setToolTip("Plex, Jellyfin and Explorer's Genre column read Genre. "
                                  "Subcategories inherit this.")
        self.genre_box.toggled.connect(self._genre_toggled)
        left_layout.addWidget(self.genre_box)
        self.genre_inherited = QLabel("")
        self.genre_inherited.setObjectName("Faint")
        self.genre_inherited.setWordWrap(True)
        left_layout.addWidget(self.genre_inherited)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search all tags")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda *_: self._fill_tags())
        right_layout.addWidget(self.search)
        self.table = TagTable()
        self.table.itemDoubleClicked.connect(lambda *_: self._rename_tag())
        right_layout.addWidget(self.table, 1)
        tag_buttons = QHBoxLayout()
        self.new_tag = text_button("New tag", "plus")
        self.rename_tag = text_button("Rename", "edit")
        self.move_tag = text_button("Move to…", "folder")
        self.merge_tag = text_button("Merge into…", "merge")
        self.delete_tag = text_button("Delete", "trash")
        for button, slot in ((self.new_tag, self._new_tag), (self.rename_tag, self._rename_tag),
                             (self.move_tag, self._move_tags), (self.merge_tag, self._merge_tag),
                             (self.delete_tag, self._delete_tags)):
            button.clicked.connect(slot)
            tag_buttons.addWidget(button)
        tag_buttons.addStretch(1)
        right_layout.addLayout(tag_buttons)
        splitter.addWidget(right)
        splitter.setSizes([320, 580])
        layout.addWidget(splitter, 1)

        bottom = QHBoxLayout()
        self.import_button = text_button("Import…", "upload")
        self.export_button = text_button("Export…", "download")
        self.import_button.clicked.connect(self._import)
        self.export_button.clicked.connect(self._export)
        bottom.addWidget(self.import_button)
        bottom.addWidget(self.export_button)
        bottom.addStretch(1)
        close = text_button("Done", primary=True)
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        layout.addLayout(bottom)
        self._rebuild()

    # ---------------------------------------------------------------- building
    def _rebuild(self, select=ALL):
        bold = QFont(self.tree.font())
        bold.setWeight(QFont.Weight.DemiBold)
        self.tree.blockSignals(True)
        self.tree.clear()
        everything = QTreeWidgetItem([f"All tags ({len(self.db.tags())})"])
        everything.setData(0, Qt.ItemDataRole.UserRole, ALL)
        everything.setFont(0, bold)
        self.tree.addTopLevelItem(everything)
        genre = self.db.genre_categories()
        found = {ALL: everything}

        def add(parent, cat):
            count = len(self.db.tags_in(cat.id))
            label = f"{cat.name}  ({count})" if count else cat.name
            item = QTreeWidgetItem([label])
            item.setData(0, Qt.ItemDataRole.UserRole, cat.id)
            if cat.id in genre:
                item.setForeground(0, _qcolor(theme.current().genre))
                item.setToolTip(0, "Writes to Genre" + ("" if cat.write_genre else " (inherited)"))
            parent.addChild(item)
            found[cat.id] = item
            for child in self.db.children(cat.id):
                add(item, child)

        for cat in self.db.children(None):
            add(self.tree.invisibleRootItem(), cat)
        loose = QTreeWidgetItem([f"Uncategorized ({len(self.db.tags_in(None))})"])
        loose.setData(0, Qt.ItemDataRole.UserRole, UNCATEGORIZED)
        self.tree.addTopLevelItem(loose)
        found[UNCATEGORIZED] = loose
        self.tree.expandAll()
        self.tree.blockSignals(False)
        self.tree.setCurrentItem(found.get(select, everything))
        self._category_selected()

    def _selected_category(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else ALL

    def _category_selected(self):
        target = self._selected_category()
        real = isinstance(target, int)
        for widget in (self.rename_cat, self.move_cat, self.delete_cat, self.new_sub, self.genre_box):
            widget.setEnabled(real)
        self.genre_box.blockSignals(True)
        if real:
            cat = self.db.category(target)
            self.genre_box.setChecked(cat.write_genre)
            inherited = target in self.db.genre_categories() and not cat.write_genre
            self.genre_inherited.setText("Already writes to Genre because a parent category does." if inherited else "")
        else:
            self.genre_box.setChecked(False)
            self.genre_inherited.setText("")
        self.genre_box.blockSignals(False)
        self._fill_tags()

    def _fill_tags(self):
        needle = self.search.text().strip().casefold()
        target = self._selected_category()
        if needle or target == ALL:
            tags = self.db.tags()
        elif target == UNCATEGORIZED:
            tags = self.db.tags_in(None)
        else:
            tags = self.db.tags_in(target)
        if needle:
            tags = [t for t in tags if needle in t.name.casefold()]
        usage = self.db.tag_usage()
        self.table.setRowCount(len(tags))
        for row, tag in enumerate(tags):
            name = QTableWidgetItem(tag.name)
            name.setData(Qt.ItemDataRole.UserRole, tag.id)
            path = " / ".join(self.db.category_path(tag.category_id)) or "Uncategorized"
            cat = QTableWidgetItem(path)
            count = usage.get(tag.name.casefold(), 0)
            files = QTableWidgetItem(str(count) if count else "")
            files.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, cat)
            self.table.setItem(row, 2, files)

    # ---------------------------------------------------------------- helpers
    def _error(self, exc: Exception):
        QMessageBox.warning(self, "Tag database", str(exc))

    def _after(self, select=None, genre_before: set[str] | None = None):
        """Refresh views and, if Genre membership changed, offer to update files."""
        self._rebuild(select if select is not None else self._selected_category())
        self.changed.emit()
        if genre_before is not None:
            after = self._genre_names()
            changed = sorted(genre_before ^ after)
            if changed:
                files = self.db.files_with_tags(changed)
                if files:
                    self.fileChanges.emit(FileChange("genre", files, description=(
                        f"Genre now {'includes' if after - genre_before else 'excludes'} "
                        f"{len(changed)} tag(s)")))

    def _genre_names(self) -> set[str]:
        cats = self.db.genre_categories()
        return {t.name.casefold() for t in self.db.tags() if t.category_id in cats}

    # ---------------------------------------------------------------- categories
    def _new_category(self):
        name, ok = QInputDialog.getText(self, "New category", "Name")
        if ok and name.strip():
            try:
                cat = self.db.add_category(name)
            except TagDBError as exc:
                return self._error(exc)
            self._after(cat.id)

    def _new_subcategory(self):
        parent = self._selected_category()
        if not isinstance(parent, int):
            return
        name, ok = QInputDialog.getText(self, "New subcategory", f"Name (inside {self.db.category(parent).name})")
        if ok and name.strip():
            before = self._genre_names()
            try:
                cat = self.db.add_category(name, parent)
            except TagDBError as exc:
                return self._error(exc)
            self._after(cat.id, before)

    def _rename_category(self):
        target = self._selected_category()
        if not isinstance(target, int):
            return
        cat = self.db.category(target)
        name, ok = QInputDialog.getText(self, "Rename category", "Name", text=cat.name)
        if ok and name.strip() and name != cat.name:
            try:
                self.db.rename_category(target, name)
            except TagDBError as exc:
                return self._error(exc)
            self._after(target)

    def _move_category(self):
        target = self._selected_category()
        if not isinstance(target, int):
            return
        exclude = {target} | self.db.descendants(target)
        ok, parent = pick_category(self, self.db, "Move category into…", self.db.category(target).parent_id,
                                   allow_top=False, exclude=exclude)
        if ok:
            self._drop_category(target, parent)

    def _drop_category(self, category_id: int, parent_id):
        before = self._genre_names()
        try:
            self.db.move_category(category_id, parent_id)
        except TagDBError as exc:
            return self._error(exc)
        self._after(category_id, before)

    def _delete_category(self):
        target = self._selected_category()
        if not isinstance(target, int):
            return
        cat = self.db.category(target)
        where = self.db.category(cat.parent_id).name if cat.parent_id else "the top level / Uncategorized"
        answer = QMessageBox.question(
            self, "Delete category",
            f"Delete “{cat.name}”? Its tags and subcategories move up to {where}. No tags are deleted.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        before = self._genre_names()
        self.db.delete_category(target)
        self._after(cat.parent_id if cat.parent_id else ALL, before)

    def _genre_toggled(self, checked: bool):
        target = self._selected_category()
        if not isinstance(target, int):
            return
        before = self._genre_names()
        self.db.set_write_genre(target, checked)
        self._after(target, before)

    # ---------------------------------------------------------------- tags
    def _new_tag(self):
        target = self._selected_category()
        category = target if isinstance(target, int) else None
        before = self._genre_names()
        tag, _ = ask_new_tag(self, self.db, "", category)
        if tag:
            self._after(None, before)

    def _rename_tag(self):
        ids = self.table.selected_ids()
        if len(ids) != 1:
            return
        tag = self.db.tag(ids[0])
        name, ok = QInputDialog.getText(self, "Rename tag", "New name", text=tag.name)
        if not ok or not name.strip() or name == tag.name:
            return
        files = self.db.files_with_tags([tag.name])
        try:
            renamed = self.db.rename_tag(tag.id, name)
        except DuplicateName as exc:
            if exc.existing_id is None:
                return self._error(exc)
            other = self.db.tag(exc.existing_id)
            answer = QMessageBox.question(self, "Merge tags",
                                          f"“{other.name}” already exists. Merge “{tag.name}” into it?")
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.db.merge_tags(tag.id, other.id)
            self._after()
            if files:
                self.fileChanges.emit(FileChange("rename", files, tag.name, other.name,
                                                 f"Merge “{tag.name}” into “{other.name}”"))
            return
        except TagDBError as exc:
            return self._error(exc)
        self._after()
        if files:
            self.fileChanges.emit(FileChange("rename", files, tag.name, renamed.name,
                                             f"Rename “{tag.name}” to “{renamed.name}”"))

    def _merge_tag(self):
        ids = self.table.selected_ids()
        if not ids:
            return
        names = [self.db.tag(i).name for i in ids]
        others = [t for t in self.db.tags() if t.id not in ids]
        if not others:
            return
        target_name, ok = QInputDialog.getItem(self, "Merge into", f"Merge {', '.join(names)} into:",
                                               [t.name for t in others], 0, False)
        if not ok:
            return
        target = self.db.find_tag(target_name)
        before = self._genre_names()
        for tag_id, name in zip(ids, names):
            files = self.db.files_with_tags([name])
            self.db.merge_tags(tag_id, target.id)
            if files:
                self.fileChanges.emit(FileChange("rename", files, name, target.name,
                                                 f"Merge “{name}” into “{target.name}”"))
        self._after(None, before)

    def _move_tags(self):
        ids = self.table.selected_ids()
        if not ids:
            return
        ok, category = pick_category(self, self.db, "Move to category", self.db.tag(ids[0]).category_id)
        if ok:
            self._drop_tags(ids, category)

    def _drop_tags(self, ids: list, category_id):
        before = self._genre_names()
        try:
            for tag_id in ids:
                self.db.move_tag(int(tag_id), category_id)
        except TagDBError as exc:
            return self._error(exc)
        self._after(None, before)

    def _delete_tags(self):
        ids = self.table.selected_ids()
        if not ids:
            return
        names = [self.db.tag(i).name for i in ids]
        files = self.db.files_with_tags(names)
        text = f"Delete {', '.join('“' + n + '”' for n in names)} from your tag database?"
        box = QMessageBox(self)
        box.setWindowTitle("Delete tags")
        box.setText(text)
        remove_too = None
        if files:
            remove_too = QCheckBox(f"Also remove from the {len(files)} file(s) Runemark tagged with it")
            remove_too.setChecked(True)
            box.setCheckBox(remove_too)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        before = self._genre_names()
        for tag_id in ids:
            self.db.delete_tag(tag_id)
        self._after(None, None)
        if files and remove_too is not None and remove_too.isChecked():
            for name in names:
                affected = self.db.files_with_tags([name])
                if affected:
                    self.fileChanges.emit(FileChange("remove", affected, name, "", f"Remove “{name}”"))
        elif before != self._genre_names():
            self._after(None, before)

    # ---------------------------------------------------------------- import / export
    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export tag database", "runemark-tags.json", "JSON (*.json)")
        if path:
            try:
                self.db.export_json(path)
            except Exception as exc:  # noqa: BLE001
                return self._error(exc)

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import tags", "", "JSON (*.json)")
        if not path:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Import tags")
        box.setText("Add the imported categories and tags to what you have, or replace everything?")
        merge = box.addButton("Add to mine", QMessageBox.ButtonRole.AcceptRole)
        replace = box.addButton("Replace all", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() not in (merge, replace):
            return
        before = self._genre_names()
        try:
            cats, tags = self.db.import_json(path, replace=box.clickedButton() is replace)
        except Exception as exc:  # noqa: BLE001
            return self._error(exc)
        QMessageBox.information(self, "Import tags", f"Added {cats} categories and {tags} tags.")
        self._after(ALL, before)


def _qcolor(value: str):
    from PySide6.QtGui import QColor
    return QColor(value)


__all__ = ["TagDatabaseDialog", "FileChange", "ask_new_tag", "pick_category", "QSize"]
