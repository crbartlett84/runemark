"""The Tags tab: chips for the open file, quick add with search, and a browsable tag tree."""
from __future__ import annotations

from PySide6.QtCore import QSize, QStringListModel, Qt, Signal
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (QCompleter, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton,
                               QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..tagdb import TagDB
from ..tagsync import SyncReport
from . import theme
from .widgets import Banner, Chip, ChipArea, text_button

EMPTY_HINT = "No tags yet. Type below or tick tags in the list."
CLOSED_HINT = "Open a video to tag it."

ROLE_KIND = Qt.ItemDataRole.UserRole
ROLE_NAME = Qt.ItemDataRole.UserRole + 1


class TagEditor(QWidget):
    tagsChanged = Signal(list)
    personEntered = Signal(str)            # a name or alias typed in the People box
    personRemoved = Signal(str)            # person uid
    skipToggled = Signal(str, str)         # person uid, value uid: "not in this video" / "back in this video"
    personOpened = Signal(str)             # person uid: open their profile
    manageRequested = Signal()
    addUnknownRequested = Signal(str)      # put an unknown tag into the database
    createRequested = Signal(str)          # new tag typed in the box
    syncChoice = Signal(str)               # "explorer" or "keywords"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.db: TagDB | None = None
        self.tags: list[str] = []
        self._genre_set: set[str] = set()
        self._known: set[str] = set()
        self._refreshing = False
        self._enabled = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 10, 8, 10)
        layout.setSpacing(10)

        self.banner = Banner()
        self.banner.hide()
        layout.addWidget(self.banner)

        people_head = QHBoxLayout()
        people_title = QLabel("PEOPLE")
        people_title.setObjectName("CardTitle")
        people_head.addWidget(people_title)
        people_head.addStretch(1)
        layout.addLayout(people_head)
        self.people_frame = ChipArea()
        self.people_chips = self.people_frame.flow
        layout.addWidget(self.people_frame)
        person_row = QHBoxLayout()
        self.person_input = QLineEdit()
        self.person_input.setPlaceholderText("Add a person (any name or alias)")
        self.person_input.setClearButtonEnabled(True)
        self.person_input.returnPressed.connect(self._commit_person)
        self.person_button = text_button("Add", "plus")
        self.person_button.clicked.connect(self._commit_person)
        person_row.addWidget(self.person_input, 1)
        person_row.addWidget(self.person_button)
        layout.addLayout(person_row)
        self.person_model = QStringListModel(self)
        self.person_completer = QCompleter(self.person_model, self)
        self.person_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.person_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.person_completer.activated.connect(self._person_completed)
        self.person_input.setCompleter(self.person_completer)
        self._people: list[tuple[str, str, str]] = []      # (uid, name, tooltip)
        self._profile: list[dict] = []                     # profile tags, see set_people
        self._people_enabled = False

        head = QHBoxLayout()
        title = QLabel("ON THIS FILE")
        title.setObjectName("CardTitle")
        self.count_label = QLabel("")
        self.count_label.setObjectName("Faint")
        head.addWidget(title)
        head.addWidget(self.count_label)
        head.addStretch(1)
        layout.addLayout(head)

        self.chips_frame = ChipArea()
        self.chips = self.chips_frame.flow
        layout.addWidget(self.chips_frame)
        self.hint = QLabel("")             # only for dashed (unknown) tags
        self.hint.setObjectName("Faint")
        self.hint.setWordWrap(True)
        self.hint.hide()
        layout.addWidget(self.hint)

        add_row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Add a tag (type to search)")
        self.input.setClearButtonEnabled(True)
        self.input.returnPressed.connect(self._commit_input)
        self.add_button = text_button("Add", "plus")
        self.add_button.clicked.connect(self._commit_input)
        add_row.addWidget(self.input, 1)
        add_row.addWidget(self.add_button)
        layout.addLayout(add_row)

        self.completer_model = QStringListModel(self)
        self.completer = QCompleter(self.completer_model, self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.completer.setMaxVisibleItems(12)
        self.completer.activated.connect(self._completed)
        self.input.setCompleter(self.completer)

        browse_head = QHBoxLayout()
        browse_title = QLabel("BROWSE YOUR TAGS")
        browse_title.setObjectName("CardTitle")
        browse_head.addWidget(browse_title)
        browse_head.addStretch(1)
        self.manage_button = QPushButton("Manage…")
        self.manage_button.setIcon(theme.icon("database", size=16))
        self.manage_button.setIconSize(QSize(16, 16))
        self.manage_button.setToolTip("Edit categories and tags (rename, move, merge, Genre settings)")
        self.manage_button.clicked.connect(self.manageRequested)
        browse_head.addWidget(self.manage_button)
        layout.addSpacing(6)
        layout.addLayout(browse_head)

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(2)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.itemChanged.connect(self._tree_toggled)
        self.tree.itemActivated.connect(self._tree_activated)
        layout.addWidget(self.tree, 1)
        self.set_enabled(False)

    # ---------------------------------------------------------------- setup
    def set_db(self, db: TagDB):
        self.db = db
        self.refresh_database()

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        for widget in (self.input, self.add_button, self.tree, self.filter):
            widget.setEnabled(enabled)
        self.set_people_enabled(enabled)
        if not enabled:
            self.chips_frame.set_placeholder(CLOSED_HINT)
            self.hint.hide()

    def refresh_database(self):
        """Reload names, categories and Genre flags from the database."""
        if self.db is None:
            return
        tags = self.db.tags()
        genre_cats = self.db.genre_categories()
        self._known = {t.name.casefold() for t in tags}
        self._genre_set = {t.name.casefold() for t in tags if t.category_id in genre_cats}
        self.completer_model.setStringList([t.name for t in tags])
        names = set()
        for person in self.db.people():
            names.add(person.name)
            names.update(person.aliases)
        self.person_model.setStringList(sorted(names, key=str.casefold))
        self._build_tree()
        self._render_chips()

    def set_tags(self, tags: list[str]):
        self.tags = list(tags)
        self._render_chips()
        self._sync_tree_checks()

    def show_sync(self, container: str | None, report: SyncReport | None, explorer_error: str | None = None):
        if container is None:
            self.banner.hide()
            return
        if container == "mkv":
            self.banner.show_message(
                "info", "MKV files can't carry Explorer tags, so <b>saving converts this file to MP4</b>. "
                        "The picture isn't re-encoded; you'll see what happens to each track first.")
            return
        if explorer_error:
            self.banner.show_message("warn", f"Couldn't read Explorer's Tags: {_esc(explorer_error)}")
            return
        if report is None or report.in_sync:
            self.banner.show_message("ok", "Explorer's Tags and the file's own tags match.")
            return
        parts = []
        if report.only_in_explorer:
            parts.append("only in Explorer: <b>" + _esc(", ".join(report.only_in_explorer)) + "</b>")
        if report.only_in_keywords:
            parts.append("only in the file's tags: <b>" + _esc(", ".join(report.only_in_keywords)) + "</b>")
        menu_button = QToolButton()
        menu_button.setText("Use one side…")
        menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(menu_button)
        use_explorer = QAction("Use Explorer's tags", menu)
        use_explorer.triggered.connect(lambda: self.syncChoice.emit("explorer"))
        use_keywords = QAction("Use the file's tags", menu)
        use_keywords.triggered.connect(lambda: self.syncChoice.emit("keywords"))
        menu.addAction(use_explorer)
        menu.addAction(use_keywords)
        menu_button.setMenu(menu)
        self.banner.show_message(
            "warn", "Out of sync: " + "; ".join(parts) + ". The list below combines both. Save to sync them.",
            [menu_button])

    # ---------------------------------------------------------------- chips
    def _render_chips(self):
        self.chips.clear()
        unknown = 0
        for name in self.tags:
            is_unknown = name.casefold() not in self._known
            is_genre = name.casefold() in self._genre_set
            unknown += is_unknown
            tip = "Not in your tag database yet. Click to add it." if is_unknown else ""
            chip = Chip(name, unknown=is_unknown, genre=is_genre, tooltip=tip, parent=self.chips_frame)
            chip.removeClicked.connect(self.remove_tag)
            if is_unknown:
                chip.clicked.connect(self.addUnknownRequested)
            self.chips.addWidget(chip)
        hand = {t.casefold() for t in self.tags}
        for row in self._profile:
            if row["tag"].casefold() in hand:
                continue                                   # typed by hand as well: shown once, as a hand tag
            chip = Chip(row["tag"], removable=False, tooltip=self._profile_tip(row), parent=self.chips_frame)
            chip.setProperty("profile", "true")
            chip.setProperty("skipped", "true" if row["skipped"] else "false")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.clicked.connect(lambda _=None, r=row, c=chip: self._profile_menu(r, c))
            self.chips.addWidget(chip)
        count = len(self.tags) + sum(1 for r in self._profile if not r["skipped"] and r["tag"].casefold() not in hand)
        self.count_label.setText(f"{count} tag{'s' if count != 1 else ''}" if count else "")
        self.chips_frame.set_placeholder(EMPTY_HINT if self._enabled else CLOSED_HINT)
        if self._enabled and unknown:
            self.hint.setText("Dashed tags came from the file but aren't in your tag database. Click one to file it.")
            self.hint.show()
        else:
            self.hint.hide()

    # ---------------------------------------------------------------- people
    def set_people_enabled(self, enabled: bool):
        self._people_enabled = enabled
        for widget in (self.person_input, self.person_button):
            widget.setEnabled(enabled)
        self.people_frame.set_placeholder("No people yet." if enabled else "")

    def set_people(self, people: list[tuple[str, str, str]], profile: list[dict]):
        """people: (uid, name, tooltip). profile: one dict per profile tag:
        {"tag", "skipped", "givers": [(person uid, person name, value uid or None, what it is)]}."""
        self._people = list(people)
        self._profile = list(profile)
        self.people_chips.clear()
        for uid, name, tip in self._people:
            chip = Chip(name, tooltip=tip, parent=self.people_frame)
            chip.setProperty("person", "true")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.removeClicked.connect(lambda _=None, u=uid: self.personRemoved.emit(u))
            chip.clicked.connect(lambda _=None, u=uid: self.personOpened.emit(u))
            self.people_chips.addWidget(chip)
        self.people_frame.set_placeholder("No people yet." if self._people_enabled else "")
        self._render_chips()

    @staticmethod
    def _profile_tip(row: dict) -> str:
        givers = "\n".join(f"• {name}: {what}" for _u, name, _v, what in row["givers"])
        state = "Not in this video (click to bring it back)" if row["skipped"] else "From a profile (click for options)"
        return f"{state}\n{givers}"

    def _profile_menu(self, row: dict, chip):
        menu = QMenu(self)
        for person_uid, name, value_uid, what in row["givers"]:
            if value_uid is None:
                act = menu.addAction(f"{name}: {what}")
                act.setEnabled(False)
                continue
            label = (f"Back in this video ({name}: {what})" if row["skipped"]
                     else f"Not in this video ({name}: {what})")
            act = menu.addAction(label)
            act.triggered.connect(lambda _=False, p=person_uid, v=value_uid: self.skipToggled.emit(p, v))
        menu.addSeparator()
        hint = menu.addAction("Change it for every video in the person's profile")
        hint.setEnabled(bool(row["givers"]))
        hint.triggered.connect(lambda _=False, p=row["givers"][0][0]: self.personOpened.emit(p))
        menu.exec(chip.mapToGlobal(chip.rect().bottomLeft()))

    def _commit_person(self):
        text = " ".join(self.person_input.text().split())
        if text and self._people_enabled:
            self.personEntered.emit(text)
        self.person_input.clear()

    def _person_completed(self, text: str):
        self.personEntered.emit(text)
        self.person_input.clear()

    # ---------------------------------------------------------------- editing
    def add_tag(self, name: str):
        name = " ".join(name.split())
        if not name:
            return
        if self.db is not None:
            name = self.db.canonical(name)
        if name.casefold() in {t.casefold() for t in self.tags}:
            return
        self.tags.append(name)
        self._render_chips()
        self._sync_tree_checks()
        self.tagsChanged.emit(list(self.tags))

    def remove_tag(self, name: str):
        before = len(self.tags)
        self.tags = [t for t in self.tags if t.casefold() != name.casefold()]
        if len(self.tags) != before:
            self._render_chips()
            self._sync_tree_checks()
            self.tagsChanged.emit(list(self.tags))

    def _commit_input(self):
        text = " ".join(self.input.text().split())
        if not text or not self._enabled:
            return
        if ";" in text:
            for part in text.split(";"):
                if part.strip():
                    self._add_or_create(part.strip())
        else:
            self._add_or_create(text)
        self.input.clear()

    def _add_or_create(self, text: str):
        if self.db is not None and self.db.find_tag(text) is None:
            self.createRequested.emit(text)
        else:
            self.add_tag(text)

    def _completed(self, text: str):
        self.add_tag(text)
        self.input.clear()

    # ---------------------------------------------------------------- tree
    def _build_tree(self):
        if self.db is None:
            return
        self._refreshing = True
        self.tree.clear()
        bold = QFont(self.tree.font())
        bold.setWeight(QFont.Weight.DemiBold)
        pal = theme.current()
        genre_cats = self.db.genre_categories()

        def add_category(parent, category):
            item = QTreeWidgetItem([category.name, "Genre" if category.id in genre_cats else ""])
            item.setData(0, ROLE_KIND, "category")
            item.setFont(0, bold)
            item.setForeground(1, theme_color(pal.genre))
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            if category.id in genre_cats:
                item.setToolTip(0, "Tags here also go to the Genre field")
            (parent.addChild(item) if parent is not None else self.tree.addTopLevelItem(item))
            for child in self.db.children(category.id):
                add_category(item, child)
            for tag in self.db.tags_in(category.id):
                add_tag_item(item, tag.name)
            return item

        def add_tag_item(parent, name):
            item = QTreeWidgetItem([name, ""])
            item.setData(0, ROLE_KIND, "tag")
            item.setData(0, ROLE_NAME, name)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
            item.setCheckState(0, Qt.CheckState.Unchecked)
            parent.addChild(item)

        for category in self.db.children(None):
            add_category(None, category)
        loose = self.db.tags_in(None)
        if loose:
            item = QTreeWidgetItem(["Uncategorized", ""])
            item.setData(0, ROLE_KIND, "category")
            item.setFont(0, bold)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tree.addTopLevelItem(item)
            for tag in loose:
                add_tag_item(item, tag.name)
        self.tree.expandAll()
        self.tree.resizeColumnToContents(1)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, self.tree.header().ResizeMode.Stretch)
        self._refreshing = False
        self._sync_tree_checks()
        self._apply_filter(self.filter.text())
        if not self.db.tags():
            empty = QTreeWidgetItem(["Your tag database is empty. Add tags here or in Manage…", ""])
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.tree.addTopLevelItem(empty)

    def _iter_tag_items(self):
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if item is None:
                continue
            if item.data(0, ROLE_KIND) == "tag":
                yield item
            stack.extend(item.child(i) for i in range(item.childCount()))

    def _sync_tree_checks(self):
        self._refreshing = True
        current = {t.casefold() for t in self.tags}
        for item in self._iter_tag_items():
            name = item.data(0, ROLE_NAME)
            item.setCheckState(0, Qt.CheckState.Checked if name.casefold() in current else Qt.CheckState.Unchecked)
        self._refreshing = False

    def _tree_toggled(self, item: QTreeWidgetItem, column: int):
        if self._refreshing or column != 0 or item.data(0, ROLE_KIND) != "tag" or not self._enabled:
            return
        name = item.data(0, ROLE_NAME)
        if item.checkState(0) == Qt.CheckState.Checked:
            self.add_tag(name)
        else:
            self.remove_tag(name)

    def _tree_activated(self, item: QTreeWidgetItem, column: int):
        if item.data(0, ROLE_KIND) == "tag" and self._enabled:
            checked = item.checkState(0) == Qt.CheckState.Checked
            item.setCheckState(0, Qt.CheckState.Unchecked if checked else Qt.CheckState.Checked)

    def _apply_filter(self, text: str):
        needle = text.strip().casefold()

        def visit(item: QTreeWidgetItem) -> bool:
            kind = item.data(0, ROLE_KIND)
            if kind == "tag":
                show = not needle or needle in item.text(0).casefold()
                item.setHidden(not show)
                return show
            own = bool(needle) and needle in item.text(0).casefold()
            any_child = False
            for i in range(item.childCount()):
                child = item.child(i)
                if own:
                    child.setHidden(False)
                    _unhide(child)
                    any_child = True
                elif visit(child):
                    any_child = True
            show = not needle or own or any_child
            item.setHidden(not show)
            return show

        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))


def _unhide(item: QTreeWidgetItem):
    for i in range(item.childCount()):
        item.child(i).setHidden(False)
        _unhide(item.child(i))


def theme_color(hex_color: str):
    from PySide6.QtGui import QColor
    return QColor(hex_color)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
