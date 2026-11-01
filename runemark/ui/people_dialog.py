"""People: names, aliases and attribute values. Edits are saved to the database as you make them; videos with
the people you changed are offered an update when the window closes (one undoable batch)."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from ..library import split_tag_input
from ..tagdb import Person, TagDB, TagDBError

# Stash's performer fields an attribute can map to (used by the Stash import plugin; Runemark itself ignores it)
STASH_FIELDS = ["", "gender", "birthdate", "ethnicity", "country", "eye_color", "hair_color", "height_cm", "weight",
                "measurements", "tattoos", "piercings", "career_length", "disambiguation", "details"]

ROLE_ID = Qt.ItemDataRole.UserRole
VALUE_COLUMNS = ["Attribute", "Value", "Tag in files", "From year", "To year"]


def _year(text: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    if not text.isdigit() or not 1800 <= int(text) <= 2999:
        raise TagDBError(f"“{text}” isn't a year.")
    return int(text)


class PeopleDialog(QDialog):
    filesNeedUpdate = Signal(list, str, list)     # file paths, description, person uids to take out of files

    def __init__(self, db: TagDB, parent=None, select_uid: str | None = None):
        super().__init__(parent)
        self.db = db
        self.touched: set[int] = set()              # people whose profile changed (their videos need updating)
        self.removed: dict[str, list[str]] = {}     # deleted people: uid -> their files
        self.removed_names: list[str] = []
        self.person_id: int | None = None
        self._filling = False
        self.setWindowTitle("People")
        self.resize(960, 600)
        layout = QVBoxLayout(self)
        intro = QLabel("A person's name and attribute values are written to their videos as tags (so Explorer and "
                       "other programs find them). Choosing any of their names or aliases tags the same person.")
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Find a person (name or alias)")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._fill_list)
        left_layout.addWidget(self.filter)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(lambda *_: self._select())
        left_layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.new_button = QPushButtonLike("New person…", self._new_person)
        self.delete_button = QPushButtonLike("Delete…", self._delete_person)
        row.addWidget(self.new_button)
        row.addWidget(self.delete_button)
        row.addStretch(1)
        left_layout.addLayout(row)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.editingFinished.connect(self._save_names)
        self.aliases = QLineEdit()
        self.aliases.setPlaceholderText("Other names, separated by commas")
        self.aliases.editingFinished.connect(self._save_names)
        self.write_aliases = QCheckBox("Also write the aliases to the files as tags")
        self.write_aliases.setToolTip("Off: only the main name is written as a tag (aliases still find them in "
                                      "Runemark and are kept in the file's record). On: every alias is a tag too.")
        self.write_aliases.toggled.connect(self._save_write_aliases)
        form.addRow("Name", self.name)
        form.addRow("Aliases", self.aliases)
        form.addRow("", self.write_aliases)
        right_layout.addLayout(form)
        self.usage = QLabel("")
        self.usage.setObjectName("Faint")
        right_layout.addWidget(self.usage)

        values_title = QLabel("ATTRIBUTES (written to their videos as tags)")
        values_title.setObjectName("CardTitle")
        right_layout.addWidget(values_title)
        self.values = QTableWidget(0, len(VALUE_COLUMNS))
        self.values.setHorizontalHeaderLabels(VALUE_COLUMNS)
        self.values.verticalHeader().hide()
        self.values.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.values.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.values.itemChanged.connect(self._value_edited)
        right_layout.addWidget(self.values, 1)

        add = QHBoxLayout()
        self.attr_combo = QComboBox()
        self.attr_combo.setEditable(True)
        self.attr_combo.setPlaceholderText("Attribute")
        self.attr_combo.setToolTip("Pick an attribute, or type a new one (for example Hair, Marks, Build)")
        self.value_edit = QLineEdit()
        self.value_edit.setPlaceholderText("Value (e.g. Red)")
        self.tag_edit = QLineEdit()
        self.tag_edit.setPlaceholderText("Tag in files (e.g. Red hair)")
        self.from_edit = QLineEdit()
        self.from_edit.setPlaceholderText("From")
        self.from_edit.setMaximumWidth(64)
        self.to_edit = QLineEdit()
        self.to_edit.setPlaceholderText("To")
        self.to_edit.setMaximumWidth(64)
        self.value_edit.textChanged.connect(lambda t: self.tag_edit.setPlaceholderText(t or "Tag in files"))
        self.value_edit.returnPressed.connect(self._add_value)
        self.tag_edit.returnPressed.connect(self._add_value)
        add.addWidget(self.attr_combo, 2)
        add.addWidget(self.value_edit, 2)
        add.addWidget(self.tag_edit, 2)
        add.addWidget(self.from_edit)
        add.addWidget(self.to_edit)
        add.addWidget(QPushButtonLike("Add", self._add_value))
        right_layout.addLayout(add)
        bottom = QHBoxLayout()
        bottom.addWidget(QPushButtonLike("Remove selected value", self._remove_value))
        bottom.addStretch(1)
        bottom.addWidget(QPushButtonLike("Attributes…", self._edit_attributes))
        right_layout.addLayout(bottom)
        splitter.addWidget(right)
        splitter.setSizes([280, 680])
        layout.addWidget(splitter, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        for b in buttons.buttons():
            b.setAutoDefault(False)
        layout.addWidget(buttons)
        self._fill_attributes()
        self._fill_list()
        if select_uid:
            self._select_uid(select_uid)
        elif self.list.count():
            self.list.setCurrentRow(0)
        self._select()

    # ---------------------------------------------------------------- list
    def _fill_list(self, *_):
        current = self.person_id
        needle = self.filter.text().strip().casefold()
        self.list.blockSignals(True)
        self.list.clear()
        for person in self.db.people():
            names = [person.name, *person.aliases]
            if needle and not any(needle in n.casefold() for n in names):
                continue
            item = QListWidgetItem(person.name + (f"  ({', '.join(person.aliases)})" if person.aliases else ""))
            item.setData(ROLE_ID, person.id)
            self.list.addItem(item)
            if person.id == current:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)

    def _select_uid(self, uid: str):
        person = self.db.person_by_uid(uid)
        if person is None:
            return
        for i in range(self.list.count()):
            if self.list.item(i).data(ROLE_ID) == person.id:
                self.list.setCurrentRow(i)
                return

    def _select(self):
        item = self.list.currentItem()
        self.person_id = item.data(ROLE_ID) if item else None
        self._fill_person()

    def _person(self) -> Person | None:
        if self.person_id is None:
            return None
        try:
            return self.db.person(self.person_id)
        except TagDBError:
            return None

    def _fill_person(self):
        person = self._person()
        self._filling = True
        for widget in (self.name, self.aliases, self.write_aliases, self.values, self.attr_combo, self.value_edit,
                       self.tag_edit, self.from_edit, self.to_edit, self.delete_button):
            widget.setEnabled(person is not None)
        self.name.setText(person.name if person else "")
        self.aliases.setText(", ".join(person.aliases) if person else "")
        self.write_aliases.setChecked(bool(person and person.write_aliases))
        self.values.setRowCount(0)
        if person:
            for v in person.values:
                row = self.values.rowCount()
                self.values.insertRow(row)
                cells = [v.attribute, v.value, v.tag, str(v.from_year or ""), str(v.to_year or "")]
                for col, text in enumerate(cells):
                    cell = QTableWidgetItem(text)
                    cell.setData(ROLE_ID, v.id)
                    if col == 0:
                        cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self.values.setItem(row, col, cell)
            n = len(self.db.files_with_person(person.id))
            self.usage.setText(f"In {n} video{'s' if n != 1 else ''} in your library" if n else
                               "Not in any videos in your library yet")
        else:
            self.usage.setText("Add a person with New person…, or from the People box beside a video.")
        self._filling = False

    # ---------------------------------------------------------------- editing
    def _changed(self):
        if self.person_id is not None:
            self.touched.add(self.person_id)

    def _error(self, exc: Exception):
        QMessageBox.warning(self, "People", str(exc))

    def _new_person(self):
        name, ok = QInputDialog.getText(self, "New person", "Name:")
        if not ok or not name.strip():
            return
        try:
            person = self.db.add_person(name)
        except TagDBError as exc:
            return self._error(exc)
        self.filter.clear()
        self.person_id = person.id
        self._fill_list()
        self._select_uid(person.uid)

    def _save_names(self):
        person = self._person()
        if person is None or self._filling:
            return
        aliases = split_tag_input(self.aliases.text())
        if self.name.text().strip() == person.name and aliases == person.aliases:
            return
        try:
            self.db.update_person(person.id, name=self.name.text(), aliases=aliases)
        except TagDBError as exc:
            self._error(exc)
        else:
            self._changed()
        self._fill_list()
        self._fill_person()

    def _save_write_aliases(self, on: bool):
        person = self._person()
        if person is None or self._filling:
            return
        self.db.update_person(person.id, write_aliases=on)
        self._changed()

    def _delete_person(self):
        person = self._person()
        if person is None:
            return
        files = self.db.files_with_person(person.id)
        text = f"Delete {person.name} from your people?"
        if files:
            text += (f"\n\nThey're in {len(files)} video(s). When you close this window Runemark takes them out "
                     "of those videos, with the tags their profile added (tags you typed yourself stay). "
                     "You can undo that from the Library (More → Undo last bulk change).")
        if QMessageBox.question(self, "Delete person", text) != QMessageBox.StandardButton.Yes:
            return
        self.removed[person.uid] = files
        self.removed_names.append(person.name)
        self.touched.discard(person.id)
        self.db.delete_person(person.id)
        self.person_id = None
        self._fill_list()
        if self.list.count():
            self.list.setCurrentRow(0)
        self._select()

    def _fill_attributes(self):
        current = self.attr_combo.currentText()
        self.attr_combo.clear()
        for attr in self.db.attributes():
            self.attr_combo.addItem(attr.name, attr.id)
        self.attr_combo.setCurrentText(current)

    def _add_value(self):
        person = self._person()
        if person is None:
            return
        attr_name = " ".join(self.attr_combo.currentText().split())
        if not attr_name:
            return self._error(TagDBError("Choose an attribute (or type a new one, such as Hair)."))
        try:
            attr = self.db.find_attribute(attr_name)
            if attr is None:
                answer = QMessageBox.question(self, "New attribute", f"Add a new attribute “{attr_name}”?")
                if answer != QMessageBox.StandardButton.Yes:
                    return
                attr = self.db.add_attribute(attr_name)
                self._fill_attributes()
            self.db.add_person_value(person.id, attr.id, self.value_edit.text(), tag=self.tag_edit.text() or None,
                                     from_year=_year(self.from_edit.text()), to_year=_year(self.to_edit.text()))
        except TagDBError as exc:
            return self._error(exc)
        self._changed()
        for edit in (self.value_edit, self.tag_edit, self.from_edit, self.to_edit):
            edit.clear()
        self.attr_combo.setCurrentText(attr.name)
        self.value_edit.setFocus()
        self._fill_person()

    def _value_edited(self, cell: QTableWidgetItem):
        if self._filling:
            return
        value_id = cell.data(ROLE_ID)
        col = cell.column()
        try:
            if col == 1:
                self.db.update_person_value(value_id, value=cell.text())
            elif col == 2:
                self.db.update_person_value(value_id, tag=cell.text() or "")
            elif col == 3:
                self.db.update_person_value(value_id, from_year=_year(cell.text()))
            elif col == 4:
                self.db.update_person_value(value_id, to_year=_year(cell.text()))
        except TagDBError as exc:
            self._error(exc)
        else:
            self._changed()
        self._fill_person()

    def _remove_value(self):
        rows = sorted({i.row() for i in self.values.selectedItems()})
        if not rows:
            return
        for row in rows:
            self.db.remove_person_value(self.values.item(row, 0).data(ROLE_ID))
        self._changed()
        self._fill_person()

    def _edit_attributes(self):
        dialog = AttributesDialog(self.db, self)
        dialog.exec()
        self.touched |= dialog.affected_people
        self._fill_attributes()
        self._fill_person()

    # ---------------------------------------------------------------- closing
    def done(self, result):  # noqa: D401 - Qt override
        if self.list.hasFocus() is False:
            self._save_names()                       # a name typed but not yet confirmed
        files: dict[str, str] = {}
        names = []
        for person_id in sorted(self.touched):
            try:
                person = self.db.person(person_id)
            except TagDBError:
                continue
            paths = self.db.files_with_person(person_id)
            if paths:
                names.append(person.name)
            for p in paths:
                files.setdefault(p.casefold(), p)
        for uid, paths in self.removed.items():
            for p in paths:
                files.setdefault(p.casefold(), p)
        if files:
            bits = []
            if names:
                bits.append("changed " + ", ".join(names))
            if self.removed_names:
                bits.append("removed " + ", ".join(self.removed_names))
            self.filesNeedUpdate.emit(list(files.values()), "People: " + "; ".join(bits), list(self.removed))
        super().done(result)


class AttributesDialog(QDialog):
    """The attribute fields themselves: rename, delete, Stash field, whether they're written as tags."""

    def __init__(self, db: TagDB, parent=None):
        super().__init__(parent)
        self.db = db
        self.affected_people: set[int] = set()
        self.setWindowTitle("Attributes")
        self.resize(620, 380)
        layout = QVBoxLayout(self)
        intro = QLabel("Attributes are the fields people can have, such as Hair or Marks. The Stash field is only "
                       "used when importing into Stash.")
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Attribute", "Stash field", "Write as tags"])
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        row.addWidget(QPushButtonLike("New…", self._new))
        row.addWidget(QPushButtonLike("Rename…", self._rename))
        row.addWidget(QPushButtonLike("Delete…", self._delete))
        row.addStretch(1)
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._fill()

    def _people_with(self, attribute_id: int) -> set[int]:
        rows = self.db.conn.execute("SELECT DISTINCT person_id FROM person_values WHERE attribute_id = ?",
                                    (attribute_id,)).fetchall()
        return {r[0] for r in rows}

    def _fill(self):
        self.table.setRowCount(0)
        for attr in self.db.attributes():
            row = self.table.rowCount()
            self.table.insertRow(row)
            name = QTableWidgetItem(attr.name)
            name.setData(ROLE_ID, attr.id)
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, name)
            combo = QComboBox()
            combo.setEditable(True)
            combo.addItems(STASH_FIELDS)
            combo.setCurrentText(attr.stash_field or "")
            combo.currentTextChanged.connect(lambda text, a=attr.id: self._set_stash(a, text))
            self.table.setCellWidget(row, 1, combo)
            box = QCheckBox()
            box.setChecked(attr.write_tags)
            box.toggled.connect(lambda on, a=attr.id: self._set_write(a, on))
            self.table.setCellWidget(row, 2, box)

    def _selected(self) -> int | None:
        rows = {i.row() for i in self.table.selectedIndexes()}
        return self.table.item(min(rows), 0).data(ROLE_ID) if rows else None

    def _set_stash(self, attribute_id: int, text: str):
        self.db.update_attribute(attribute_id, stash_field=text.strip() or None)
        self.affected_people |= self._people_with(attribute_id)     # the files' record carries the mapping

    def _set_write(self, attribute_id: int, on: bool):
        self.db.update_attribute(attribute_id, write_tags=on)
        self.affected_people |= self._people_with(attribute_id)

    def _new(self):
        name, ok = QInputDialog.getText(self, "New attribute", "Name (for example Hair):")
        if ok and name.strip():
            try:
                self.db.add_attribute(name)
            except TagDBError as exc:
                QMessageBox.warning(self, "Attributes", str(exc))
            self._fill()

    def _rename(self):
        attribute_id = self._selected()
        if attribute_id is None:
            return
        attr = self.db.attribute(attribute_id)
        name, ok = QInputDialog.getText(self, "Rename attribute", "Name:", text=attr.name)
        if ok and name.strip():
            try:
                self.db.update_attribute(attribute_id, name=name)
            except TagDBError as exc:
                QMessageBox.warning(self, "Attributes", str(exc))
            else:
                self.affected_people |= self._people_with(attribute_id)
            self._fill()

    def _delete(self):
        attribute_id = self._selected()
        if attribute_id is None:
            return
        attr = self.db.attribute(attribute_id)
        people = self._people_with(attribute_id)
        text = f"Delete the attribute “{attr.name}”?"
        if people:
            text += (f"\n\n{len(people)} person(s) have values for it. Those values are deleted too, and their "
                     "videos lose the tags they gave (offered when you close People).")
        if QMessageBox.question(self, "Delete attribute", text) == QMessageBox.StandardButton.Yes:
            self.affected_people |= people
            self.db.delete_attribute(attribute_id)
            self._fill()


def QPushButtonLike(text: str, slot):  # noqa: N802 - a small factory that reads like a widget
    from PySide6.QtWidgets import QPushButton
    button = QPushButton(text)
    button.setAutoDefault(False)      # Enter in a text box must not press a button (e.g. New person…)
    button.setDefault(False)
    button.clicked.connect(slot)
    return button
