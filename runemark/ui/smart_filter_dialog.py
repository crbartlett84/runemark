"""Making or changing a smart filter (0.9.0): a name, all or any of the rules, and the rules themselves, with how
many videos match as you go."""
from __future__ import annotations

from PySide6.QtCore import QStringListModel, Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget)

from .. import library as L


class _RuleRow(QWidget):
    def __init__(self, dialog: "SmartFilterDialog", rule: L.SmartRule | None):
        super().__init__()
        self.dialog = dialog
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.field = QComboBox()
        for key, (label, _ops, _takes) in L.SMART_FIELDS.items():
            self.field.addItem(label, key)
        self.op = QComboBox()
        self.value = QLineEdit()
        self.value.setPlaceholderText("value")
        self.remove = QPushButton("✕")
        self.remove.setToolTip("Remove this rule")
        self.remove.setFixedWidth(30)
        self.remove.setAutoDefault(False)
        row.addWidget(self.field, 2)
        row.addWidget(self.op, 2)
        row.addWidget(self.value, 3)
        row.addWidget(self.remove)
        self.field.currentIndexChanged.connect(self._field_changed)
        self.op.currentIndexChanged.connect(self._op_changed)
        self.value.textChanged.connect(lambda *_: dialog.update_count())
        self.remove.clicked.connect(lambda: dialog.remove_row(self))
        if rule is not None:
            self.field.setCurrentIndex(max(0, self.field.findData(rule.field)))
        self._field_changed()
        if rule is not None:
            self.op.setCurrentIndex(max(0, self.op.findData(rule.op)))
            self.value.setText(rule.value)

    def _field_changed(self, *_):
        key = self.field.currentData()
        _label, ops, takes = L.SMART_FIELDS[key]
        self.op.blockSignals(True)
        self.op.clear()
        for op, label in ops:
            self.op.addItem(label, op)
        self.op.blockSignals(False)
        names = self.dialog.names.get({"tag": "tag", "person": "person", "studio": "studio", "genre": "tag"}.get(
            key, ""), [])
        completer = QCompleter(QStringListModel(names, self.value), self.value)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.value.setCompleter(completer if names else None)
        self._op_changed()

    def _op_changed(self, *_):
        _label, _ops, takes = L.SMART_FIELDS[self.field.currentData()]
        self.value.setVisible(takes and self.op.currentData() != "none")
        self.dialog.update_count()

    def rule(self) -> L.SmartRule:
        return L.SmartRule(self.field.currentData(), self.op.currentData(), " ".join(self.value.text().split()))


class SmartFilterDialog(QDialog):
    def __init__(self, smart: L.SmartFilter | None, names: dict, records: list, taken: list[str], parent=None):
        super().__init__(parent)
        self.names = names
        self.records = [r for r in records if not r.missing]
        self.taken = {t.casefold() for t in taken}
        self.rows: list[_RuleRow] = []
        self.setWindowTitle("Smart filter")
        self.resize(640, 360)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Name"))
        self.name = QLineEdit(smart.name if smart else "")
        self.name.setPlaceholderText("Name shown on its button")
        top.addWidget(self.name, 1)
        self.match = QComboBox()
        self.match.addItem("Every rule matches (and)", "all")
        self.match.addItem("Any rule matches (or)", "any")
        self.match.setCurrentIndex(1 if smart and smart.match == "any" else 0)
        self.match.currentIndexChanged.connect(lambda *_: self.update_count())
        top.addWidget(self.match)
        layout.addLayout(top)
        self.rules_box = QVBoxLayout()
        layout.addLayout(self.rules_box)
        add = QPushButton("Add a rule")
        add.setAutoDefault(False)
        add.clicked.connect(lambda: self.add_row(None))
        layout.addWidget(add, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        self.pinned = QCheckBox("Pin it above the list (with how many videos match)")
        self.pinned.setChecked(smart.pinned if smart else True)
        layout.addWidget(self.pinned)
        self.count = QLabel("")
        self.count.setObjectName("Muted")
        layout.addWidget(self.count)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for rule in (smart.rules if smart else []) or [None]:
            self.add_row(rule)
        self.update_count()

    def add_row(self, rule):
        row = _RuleRow(self, rule)
        self.rows.append(row)
        self.rules_box.addWidget(row)
        self.update_count()

    def remove_row(self, row):
        if row in self.rows:
            self.rows.remove(row)
            row.setParent(None)
            row.deleteLater()
        self.update_count()

    def value(self) -> L.SmartFilter:
        rules = [r.rule() for r in self.rows]
        rules = [r for r in rules if r.value or not L.SMART_FIELDS[r.field][2] or r.op == "none"]
        return L.SmartFilter(" ".join(self.name.text().split()), rules, self.match.currentData(),
                             self.pinned.isChecked())

    def update_count(self):
        if not hasattr(self, "count"):
            return
        sf = self.value()
        n = sum(1 for r in self.records if L.smart_matches(r, sf))
        self.count.setText(f"{n} video{'s' if n != 1 else ''} match: {sf.describe()}")

    def _ok(self):
        sf = self.value()
        if not sf.name:
            QMessageBox.information(self, "Smart filter", "Give it a name.")
            return
        if sf.name.casefold() in self.taken:
            QMessageBox.information(self, "Smart filter", f"There's already a filter called {sf.name}.")
            return
        self.accept()
