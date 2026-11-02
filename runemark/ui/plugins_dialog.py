"""⋯ → Manage plugins: what's installed, turn plugins on and off, open the plugins folder."""
from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QPushButton, QVBoxLayout)

from .. import plugins as P


class PluginsDialog(QDialog):
    def __init__(self, host: P.PluginHost, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Plugins — Runemark")
        self.resize(560, 380)
        layout = QVBoxLayout(self)
        intro = QLabel("Plugins add features to Runemark: a pane beside Details, menu actions, things that happen "
                       "when a video opens or is saved. They're Python code with the same access to your files as "
                       "Runemark, so only install plugins you trust. Changes take effect when Runemark restarts.")
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        layout.addWidget(intro)
        self.list = QListWidget()
        layout.addWidget(self.list, 1)
        found = {p.key: p for p in host.plugins}
        for p in P.discover():                     # also ones added since Runemark started
            p = found.get(p.key, p)
            item = QListWidgetItem()
            state = "on" if p.loaded else ("off" if p.key in settings.plugins_disabled else
                                           ("not loaded: " + p.error if p.error else "loads on restart"))
            item.setText(f"{p.name} {p.version}\n{p.description}\n{state}".strip())
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if p.key in settings.plugins_disabled
                               else Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, p.key)
            self.list.addItem(item)
        if not self.list.count():
            self.list.addItem("No plugins yet. Put a plugin's folder in the plugins folder and restart Runemark.")
        row = QHBoxLayout()
        open_folder = QPushButton("Open plugins folder")
        open_folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(P.plugins_dir()))))
        row.addWidget(open_folder)
        row.addStretch(1)
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self):
        off = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            key = item.data(Qt.ItemDataRole.UserRole)
            if key and item.checkState() == Qt.CheckState.Unchecked:
                off.append(key)
        self.settings.plugins_disabled = off
        self.settings.save()
        self.accept()
