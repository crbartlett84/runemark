"""Telling the user about a new version: what's in it, and Update now / Later / Skip."""
from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout

from .. import __version__


class UpdateDialog(QDialog):
    def __init__(self, release, parent=None):
        super().__init__(parent)
        self.release = release
        self.choice = "later"
        self.setWindowTitle("Update Runemark")
        self.resize(640, 480)
        layout = QVBoxLayout(self)
        head = QLabel(f"<b>Runemark {release.version}</b> is out. You have {__version__}.")
        layout.addWidget(head)
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(release.notes or "No release notes.")
        layout.addWidget(notes, 1)
        how = QLabel("Update now downloads it from GitHub, checks it against the checksum GitHub publishes, then "
                     "closes Runemark, swaps in the new version and opens it again. The version you have now is "
                     "kept beside it, and your tags, people and settings aren't touched.")
        how.setObjectName("Faint")
        how.setWordWrap(True)
        layout.addWidget(how)
        row = QHBoxLayout()
        page = QPushButton("Release page")
        page.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(release.page)))
        skip = QPushButton("Skip this version")
        skip.clicked.connect(lambda: self._done("skip"))
        later = QPushButton("Later")
        later.clicked.connect(lambda: self._done("later"))
        install = QPushButton("Update now")
        install.setDefault(True)
        install.clicked.connect(lambda: self._done("install"))
        row.addWidget(page)
        row.addStretch(1)
        for b in (skip, later, install):
            row.addWidget(b)
        layout.addLayout(row)

    def _done(self, choice: str):
        self.choice = choice
        self.accept()

    def exec_choice(self) -> str:
        self.exec()
        return self.choice
