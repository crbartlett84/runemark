"""The main window: open, play, pick a thumbnail, tag, and save."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
                               QSplitter, QTabWidget, QToolButton, QVBoxLayout, QWidget,
                               QAbstractSpinBox, QComboBox as _QComboBox)

from .. import __version__, explorer, library, selftest
from ..media import chapters as chapter_io
from ..media import convert as C
from ..media import frames, suggest
from ..media.mp4meta import UNCHANGED, VideoMeta
from ..media.probe import MediaInfo, VIDEO_EXTENSIONS, describe, fmt_duration, fmt_size, frame_rate_mode, probe
from ..paths import log_dir
from ..settings import Settings
from ..tagdb import TagDB, file_key
from ..tagsync import (FileState, leftover_temp_files, merge_unique, read_state, remove_file, route_fields,
                       same_tags, save_mp4, save_mp4_with_chapters)
from . import theme, workers
from .dialogs import ConvertDialog, ProgressDialog, SettingsDialog, TextReportDialog
from .chapters_panel import ChaptersPanel
from .panels import DetailsPanel, ThumbnailPanel
from .player import Filmstrip, PlayerPanel, bytes_to_pixmap, pil_to_qimage
from .tag_editor import TagEditor
from .tagdb_dialog import FileChange, TagDatabaseDialog, ask_new_tag
from .widgets import icon_button, text_button

log = logging.getLogger(__name__)

REMOVE = b""


class Document:
    """The open file and the edits waiting to be saved."""

    def __init__(self, path: Path, info: MediaInfo, state: FileState, db: TagDB):
        self.path = path
        self.info = info
        self.state = state
        self.tags = [db.canonical(t) for t in state.all_tags()]
        self.saved_tags = list(self.tags)
        self.cover = UNCHANGED          # UNCHANGED, jpeg bytes, or REMOVE
        self.cover_note = ""
        self.meta = VideoMeta(**vars(state.meta))
        self.saved_meta = VideoMeta(**vars(state.meta))
        self.chapters = chapter_io.marks_from_info(info)
        self.saved_chapters = chapter_io.marks_from_info(info)
        self.suggestions: list[tuple[float, QPixmap]] | None = None

    @property
    def container(self) -> str:
        return self.state.container

    def meta_changed(self) -> bool:
        return self.meta.key() != self.saved_meta.key()

    def chapters_changed(self) -> bool:
        return not chapter_io.same_marks(self.chapters, self.saved_chapters)

    def dirty(self) -> bool:
        return (not same_tags(self.tags, self.saved_tags)) or self.cover is not UNCHANGED \
            or self.meta_changed() or self.chapters_changed()

    def discard(self) -> None:
        self.tags = list(self.saved_tags)
        self.cover = UNCHANGED
        self.meta = VideoMeta(**vars(self.saved_meta))
        self.chapters = [chapter_io.ChapterMark(m.start, m.name, m.auto) for m in self.saved_chapters]

    def whole_cover(self) -> bytes | None:
        """The cover the file should end up with (for saves that rebuild the file)."""
        if self.cover is UNCHANGED:
            return self.state.cover
        return self.cover or None

    def needs_sync(self) -> bool:
        return self.container == "mp4" and not self.state.sync().in_sync


class MainWindow(QMainWindow):
    def __init__(self, db: TagDB, settings: Settings):
        super().__init__()
        self.db = db
        self.settings = settings
        self.doc: Document | None = None
        self.queue: list[Path] = []
        self._busy = False
        self._open_token = 0
        self._suggest_job = None
        self.library_window = None
        self.setWindowTitle("Runemark")
        self.setWindowIcon(theme.app_icon())
        self.setAcceptDrops(True)
        self.resize(1360, 860)
        self.setMinimumSize(980, 640)

        self._build_header()
        self._build_body()
        self._build_status()
        self._build_actions()

        self.player.set_volume(settings.volume, settings.muted)
        self.tag_editor.set_db(db)
        if settings.window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(settings.window_geometry.encode()))
        if settings.splitter_state:
            self.splitter.restoreState(QByteArray.fromBase64(settings.splitter_state.encode()))
        self._refresh_ui()

    # ================================================================ layout
    def _build_header(self):
        bar = QFrame()
        bar.setObjectName("HeaderBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(14, 8, 12, 8)
        row.setSpacing(8)
        logo = QLabel()
        logo.setPixmap(theme.rune_pixmap(26))
        title = QLabel("Runemark")
        title.setObjectName("AppTitle")
        row.addWidget(logo)
        row.addWidget(title)
        row.addSpacing(14)
        self.open_button = text_button("Open", "open")
        self.open_button.setToolTip("Open MP4 or MKV files (Ctrl+O)")
        self.open_button.clicked.connect(self.choose_files)
        row.addWidget(self.open_button)
        self.library_button = text_button("Library", "library")
        self.library_button.setToolTip("Every video in your folders: search by tag, change many at once (Ctrl+B)")
        self.library_button.clicked.connect(self.show_library)
        row.addWidget(self.library_button)
        row.addSpacing(10)
        self.prev_file = icon_button("chev_left", "Previous file in the list (Ctrl+PgUp)", 18)
        self.next_file = icon_button("chev_right", "Next file in the list (Ctrl+PgDown)", 18)
        self.prev_file.clicked.connect(lambda: self._move_in_queue(-1))
        self.next_file.clicked.connect(lambda: self._move_in_queue(1))
        self.file_combo = QComboBox()
        self.file_combo.setMinimumWidth(320)
        self.file_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.file_combo.activated.connect(self._combo_picked)
        self.dirty_dot = QLabel("●")
        self.dirty_dot.setObjectName("DirtyDot")
        self.dirty_dot.setToolTip("Unsaved changes")
        row.addWidget(self.prev_file)
        row.addWidget(self.file_combo, 1)
        row.addWidget(self.next_file)
        row.addWidget(self.dirty_dot)
        row.addStretch(0)
        self.save_button = text_button("Save", "save", primary=True)
        self.save_button.setToolTip("Write tags and thumbnail to the file (Ctrl+S)")
        self.save_button.clicked.connect(lambda: self.save())
        row.addWidget(self.save_button)
        for button in (self.open_button, self.library_button, self.prev_file, self.next_file, self.save_button):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.more_button = icon_button("more", "More", 20)
        self.more_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        row.addWidget(self.more_button)
        self.header = bar

    def _build_body(self):
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.header)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(16, 14, 8, 12)
        left_layout.setSpacing(10)
        self.player = PlayerPanel()
        self.player.captureRequested.connect(self.capture_frame)
        self.player.loadFailed.connect(self._player_failed)
        self.player.frame.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        left_layout.addWidget(self.player, 1)
        strip_head = QHBoxLayout()
        strip_label = QLabel("FRAMES ACROSS THE VIDEO")
        strip_label.setObjectName("CardTitle")
        strip_hint = QLabel("click to jump")
        strip_hint.setObjectName("Faint")
        strip_head.addWidget(strip_label)
        strip_head.addWidget(strip_hint)
        strip_head.addStretch(1)
        left_layout.addLayout(strip_head)
        self.filmstrip = Filmstrip()
        self.filmstrip.seekRequested.connect(self.player.seek_seconds)
        left_layout.addWidget(self.filmstrip)
        self.strip_widgets = (strip_label, strip_hint, self.filmstrip)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 8, 16, 12)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tag_editor = TagEditor()
        self.tag_editor.tagsChanged.connect(self._tags_edited)
        self.tag_editor.manageRequested.connect(self.manage_tags)
        self.tag_editor.createRequested.connect(self._create_tag)
        self.tag_editor.addUnknownRequested.connect(self._file_unknown_tag)
        self.tag_editor.syncChoice.connect(self._use_one_side)
        self.details = DetailsPanel()
        self.details.meta.changed.connect(self._meta_edited)
        self.thumbs = ThumbnailPanel()
        self.thumbs.captureRequested.connect(self.capture_frame)
        self.thumbs.chooseFileRequested.connect(self.choose_cover_file)
        self.thumbs.removeRequested.connect(self.remove_cover)
        self.thumbs.revertRequested.connect(self.revert_cover)
        self.thumbs.suggestRequested.connect(self.find_suggestions)
        self.thumbs.suggestionPicked.connect(self._show_suggestion)
        self.thumbs.suggestionUse.connect(self._use_suggestion)
        self.chapters = ChaptersPanel()
        self.chapters.addRequested.connect(self.add_chapter)
        self.chapters.seekRequested.connect(self._seek_paused)
        self.chapters.changed.connect(self._chapters_edited)
        self.chapters.nameTagged.connect(self._chapter_named)
        self.tabs.addTab(self.tag_editor, theme.icon("tag", size=16), "Tags")
        self.tabs.addTab(self.chapters, theme.icon("chapters", size=16), "Chapters")
        self.tabs.addTab(self.thumbs, theme.icon("image", size=16), "Thumbnail")
        self.tabs.addTab(self.details, theme.icon("info", size=16), "Details")
        self.tabs.currentChanged.connect(self._tab_changed)
        right_layout.addWidget(self.tabs)
        right.setMinimumWidth(380)

        self.splitter.addWidget(left)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([860, 500])
        outer.addWidget(self.splitter, 1)
        self.setCentralWidget(central)

    def _build_status(self):
        status = self.statusBar()
        status.setSizeGripEnabled(False)
        self.status_text = QLabel("Ready")
        self.status_progress = QProgressBar()
        self.status_progress.setFixedWidth(140)
        self.status_progress.setRange(0, 0)
        self.status_progress.hide()
        writer = "Windows" if explorer.windows_api_available() and self.settings.explorer_writer == "auto" \
            else "built-in writer"
        self.status_writer = QLabel(f"Explorer Tags: {writer}")
        self.status_writer.setObjectName("Faint")
        status.addWidget(self.status_text, 1)
        status.addPermanentWidget(self.status_progress)
        status.addPermanentWidget(self.status_writer)

    def _build_actions(self):
        def action(text, slot, shortcut=None, icon_name=None):
            act = QAction(text, self)
            if icon_name:
                act.setIcon(theme.icon(icon_name, size=16))
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
                act.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
            act.triggered.connect(slot)
            self.addAction(act)
            return act

        self.act_open = action("Open…", self.choose_files, "Ctrl+O", "open")
        self.act_save = action("Save", lambda: self.save(), "Ctrl+S", "save")
        self.act_capture = action("Use this frame as the thumbnail", self.capture_frame, "Ctrl+T", "camera")
        self.act_manage = action("Tag database…", self.manage_tags, "Ctrl+D", "database")
        self.act_library = action("Library…", self.show_library, "Ctrl+B", "library")
        self.act_settings = action("Settings…", self.open_settings, "Ctrl+,", "settings")
        self.act_selftest = action("Run self-test…", self.run_self_test, None, "shield")
        self.act_logs = action("Open log folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir()))),
                               None, "folder")
        self.act_about = action("About Runemark", self.about, None, "info")
        action("Previous file", lambda: self._move_in_queue(-1), "Ctrl+PgUp")
        action("Next file", lambda: self._move_in_queue(1), "Ctrl+PgDown")
        action("Focus tag box", lambda: (self.tabs.setCurrentWidget(self.tag_editor), self.tag_editor.input.setFocus()),
               "Ctrl+L")
        self.recent_menu = QMenu("Open recent", self)
        menu = QMenu(self)
        menu.addAction(self.act_open)
        menu.addMenu(self.recent_menu)
        menu.addSeparator()
        menu.addAction(self.act_library)
        menu.addAction(self.act_manage)
        menu.addAction(self.act_settings)
        menu.addSeparator()
        menu.addAction(self.act_selftest)
        menu.addAction(self.act_logs)
        menu.addAction(self.act_about)
        menu.aboutToShow.connect(self._fill_recent)
        self.more_button.setMenu(menu)

    # ================================================================ keyboard and drops
    def keyPressEvent(self, event):  # noqa: N802
        focus = QApplication.focusWidget()
        typing = isinstance(focus, (QLineEdit, QAbstractSpinBox, QAbstractItemView, _QComboBox, QPlainTextEdit))
        key = event.key()
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if self.doc is not None and not typing:
            if key == Qt.Key.Key_Space:
                self.player.toggle_play()
                return
            if key in (Qt.Key.Key_Left, Qt.Key.Key_Right) and shift:
                self.player.nudge(-5 if key == Qt.Key.Key_Left else 5)
                return
            if key in (Qt.Key.Key_Comma, Qt.Key.Key_Left):
                self.player.step(-1)
                return
            if key in (Qt.Key.Key_Period, Qt.Key.Key_Right):
                self.player.step(1)
                return
            if key == Qt.Key.Key_Home:
                self.player.seek_seconds(0)
                return
            if key == Qt.Key.Key_M and not event.modifiers():
                self.add_chapter()
                return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event):  # noqa: N802
        if any(Path(u.toLocalFile()).suffix.lower() in VIDEO_EXTENSIONS for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls()]
        self.open_paths(paths)

    # ================================================================ opening
    def choose_files(self):
        start = self.settings.last_folder or str(Path.home())
        names, _ = QFileDialog.getOpenFileNames(self, "Open videos", start, "Videos (*.mp4 *.m4v *.mkv)")
        if names:
            self.open_paths([Path(n) for n in names])

    def open_paths(self, paths: list[Path]):
        videos = [p for p in paths if p.suffix.lower() in VIDEO_EXTENSIONS and p.is_file()]
        if not videos:
            if paths:
                self.toast("Runemark opens MP4 and MKV files.")
            return
        keys = {file_key(p) for p in self.queue}
        for path in videos:
            if file_key(path) not in keys:
                self.queue.append(path)
                keys.add(file_key(path))
        self._fill_combo()
        self.open_file(videos[0])

    def open_file(self, path: Path, position_ms: int = 0, force: bool = False):
        if self._busy:
            return
        if not force and self.doc is not None and file_key(self.doc.path) != file_key(path):
            if not self._confirm_leave(lambda: self.open_file(path, position_ms, force=True)):
                return
        self._load(path, position_ms)

    def _load(self, path: Path, position_ms: int = 0, status: str | None = None):
        """Open `path` in the background. `status` replaces "Opened …" once it's shown (after a save)."""
        self._open_token += 1
        token = self._open_token
        self.player.unload()
        self.filmstrip.clear()
        self._cancel_suggestions()
        if self.doc is not None and file_key(self.doc.path) != file_key(path):
            self.doc = None          # nothing can be saved or captured while the next file loads
            self._refresh_ui()
        self.set_status(f"Opening {path.name}…", busy=True)
        self.settings.last_folder = str(path.parent)
        self.settings.add_recent(str(path))

        def cover_reader(info: MediaInfo):
            sources = info.cover_sources
            return frames.dump_attachment(info.path, sources[0]) if sources else None

        def work(job):
            info = probe(path)
            state = read_state(path, info, cover_reader)
            return info, state

        def done(result):
            if token != self._open_token:
                return
            info, state = result
            self.doc = Document(path, info, state, self.db)
            self._show_document(position_ms)
            self.set_status(status or f"Opened {path.name}")
            QTimer.singleShot(0, lambda: self._offer_leftover_cleanup(path))

        def failed(exc, _tb):
            if token != self._open_token:
                return
            self.set_status("Couldn't open the file")
            self.doc = None
            self._refresh_ui()
            self.player.show_message("Couldn't open this file", f"{path.name}\n{exc}")
            QMessageBox.warning(self, "Open", f"Couldn't open {path.name}:\n\n{exc}")

        workers.start(work, on_done=done, on_error=failed)

    def _offer_leftover_cleanup(self, path: Path):
        """A crash or power cut mid-save leaves a near-complete working copy (<name>.runemark-*) beside the
        video. The Library hides those, so offer to delete them here, where the user is looking at the file."""
        if self._busy:
            return
        leftovers = leftover_temp_files(path)
        if not leftovers:
            return
        total = 0
        for p in leftovers:
            try:
                total += p.stat().st_size
            except OSError:
                pass
        names = "\n• ".join(p.name for p in leftovers)
        answer = QMessageBox.question(
            self, "Leftover working files",
            f"Runemark found {len(leftovers)} working file(s) beside {path.name}, left by a save or conversion "
            f"that didn't finish (a crash, power cut or forced close). They're incomplete copies taking "
            f"{fmt_size(total)}:\n\n• {names}\n\nDelete them? The video itself isn't touched.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        kept = [p.name for p in leftovers if not remove_file(p)]
        if kept:
            QMessageBox.warning(self, "Leftover working files",
                                "Couldn't delete:\n• " + "\n• ".join(kept) + "\n\nThey may be open in another program.")
        else:
            self.set_status(f"Deleted {len(leftovers)} leftover working file(s), {fmt_size(total)}")

    def _show_document(self, position_ms: int = 0):
        doc = self.doc
        if doc is None:
            return
        self.player.reset_message()
        self.player.load(doc.path, doc.info, position_ms)
        if self.settings.filmstrip_count:
            self.filmstrip.load(doc.path, doc.info, self.settings.filmstrip_count)
        for widget in self.strip_widgets:
            widget.setVisible(bool(self.settings.filmstrip_count))
        self.details.show_sections(describe(doc.info))
        self._load_frame_mode(doc)
        self.tag_editor.set_enabled(True)
        self.tag_editor.set_tags(doc.tags)
        self.tag_editor.show_sync(doc.container, doc.state.sync(), doc.state.explorer_error)
        self.thumbs.set_container(doc.container)
        self._refresh_thumbs()
        self.thumbs.clear_suggestions()
        self.details.meta.set_meta(doc.meta, doc.container)
        self._refresh_tag_names()
        self.chapters.set_enabled(True, doc.container)
        self.chapters.set_marks(doc.chapters)
        self._fill_combo()
        self._refresh_ui()
        self.player.frame.setFocus()
        if self.tabs.currentWidget() is self.thumbs:
            self.find_suggestions()

    def _load_frame_mode(self, doc: Document):
        video = doc.info.main_video
        if video is None:
            return

        def done(mode):
            if self.doc is doc and mode:
                self.details.show_sections(describe(doc.info, mode))

        workers.start(lambda job: frame_rate_mode(doc.path, video), on_done=done)

    def _player_failed(self, message: str):
        if self.doc is not None:
            self.toast(f"Playback problem: {message}. Tags and details still work.")

    # ================================================================ queue
    def _fill_combo(self):
        self.file_combo.blockSignals(True)
        self.file_combo.clear()
        for path in self.queue:
            self.file_combo.addItem(theme.icon("film", size=16), path.name, str(path))
        if self.doc is not None:
            index = next((i for i, p in enumerate(self.queue) if file_key(p) == file_key(self.doc.path)), -1)
            self.file_combo.setCurrentIndex(index)
        if not self.queue:
            self.file_combo.setPlaceholderText("No video open")
        self.file_combo.blockSignals(False)
        self.prev_file.setEnabled(len(self.queue) > 1)
        self.next_file.setEnabled(len(self.queue) > 1)

    def _combo_picked(self, index: int):
        if 0 <= index < len(self.queue):
            target = self.queue[index]
            if self.doc is None or file_key(target) != file_key(self.doc.path):
                self.open_file(target)
            self._fill_combo()

    def _move_in_queue(self, step: int):
        if len(self.queue) < 2:
            return
        current = 0
        if self.doc is not None:
            current = next((i for i, p in enumerate(self.queue) if file_key(p) == file_key(self.doc.path)), 0)
        self.open_file(self.queue[(current + step) % len(self.queue)])

    def _fill_recent(self):
        self.recent_menu.clear()
        recent = [p for p in self.settings.recent_files if Path(p).exists()]
        if not recent:
            empty = self.recent_menu.addAction("Nothing yet")
            empty.setEnabled(False)
        for item in recent:
            act = self.recent_menu.addAction(Path(item).name)
            act.setToolTip(item)
            act.triggered.connect(lambda _=False, p=item: self.open_paths([Path(p)]))

    # ================================================================ tags
    def _tags_edited(self, tags: list[str]):
        if self.doc is not None:
            self.doc.tags = list(tags)
            self._refresh_ui()

    def _create_tag(self, text: str):
        tag, category = ask_new_tag(self, self.db, text, self.settings.last_category_id)
        if tag is not None:
            self.settings.last_category_id = category
            self.tag_editor.refresh_database()
            self._refresh_tag_names()
            self.tag_editor.add_tag(tag.name)

    def _file_unknown_tag(self, name: str):
        tag, category = ask_new_tag(self, self.db, name, self.settings.last_category_id, fixed_name=True)
        if tag is not None:
            self.settings.last_category_id = category
            self.tag_editor.refresh_database()

    def _use_one_side(self, side: str):
        doc = self.doc
        if doc is None or doc.state.explorer is None:
            return
        chosen = doc.state.explorer if side == "explorer" else doc.state.keywords
        doc.tags = [self.db.canonical(t) for t in merge_unique(chosen)]
        doc.saved_tags = []  # force a save
        self.tag_editor.set_tags(doc.tags)
        self._refresh_ui()

    def manage_tags(self):
        dialog = TagDatabaseDialog(self.db, self)
        dialog.changed.connect(self.tag_editor.refresh_database)
        dialog.fileChanges.connect(self._queue_file_change)
        self._pending_changes: list[FileChange] = []
        dialog.exec()
        self.tag_editor.refresh_database()
        self._refresh_tag_names()
        changes, self._pending_changes = self._pending_changes, []
        if changes:
            self._apply_file_changes(changes)

    def _queue_file_change(self, change: FileChange):
        self._pending_changes.append(change)

    # ================================================================ thumbnail
    def _refresh_thumbs(self):
        doc = self.doc
        if doc is None:
            self.thumbs.set_container(None)
            self.thumbs.set_state(None, None, "")
            return
        current = bytes_to_pixmap(doc.state.cover)
        if doc.cover is UNCHANGED:
            note = ""
            if doc.container == "mkv" and doc.state.cover:
                note = "The MKV's cover image will become the MP4's thumbnail."
            self.thumbs.set_state(current, None, note)
        elif doc.cover == REMOVE:
            self.thumbs.set_state(current, None, "The thumbnail will be removed when you save.", removing=True)
        else:
            self.thumbs.set_state(current, bytes_to_pixmap(doc.cover), doc.cover_note, changed=True)

    def capture_frame(self, pts: float | None = None):
        doc = self.doc
        if doc is None or self._busy:
            return
        self.player.pause()
        if pts is None or isinstance(pts, bool):
            pts = self.player.current_pts()
        if pts is None:
            pts = self.player.player.position() / 1000
        max_edge = self.settings.thumbnail_max_edge or None
        quality = self.settings.jpeg_quality
        self.set_status("Capturing frame…", busy=True)

        def work(job):
            image = frames.grab_at_pts(doc.path, doc.info, pts, max_edge=None)
            return frames.to_jpeg(image, max_edge=max_edge, quality=quality), image.size

        def done(result):
            jpeg, size = result
            if self.doc is not doc:
                return
            doc.cover = jpeg
            doc.cover_note = f"Frame at {fmt_duration(pts, millis=True)} · saved as a {_fit(size, max_edge)} JPEG"
            self._refresh_thumbs()
            self.tabs.setCurrentWidget(self.thumbs)
            self._refresh_ui()
            self.set_status("Thumbnail ready. Save to write it to the file.")

        def failed(exc, _tb):
            self.set_status("Couldn't capture the frame")
            QMessageBox.warning(self, "Thumbnail", f"Couldn't capture this frame:\n\n{exc}")

        workers.start(work, on_done=done, on_error=failed)

    def choose_cover_file(self):
        doc = self.doc
        if doc is None:
            return
        name, _ = QFileDialog.getOpenFileName(self, "Choose thumbnail image", str(doc.path.parent),
                                              "Images (*.jpg *.jpeg *.png *.webp *.bmp)")
        if not name:
            return
        try:
            doc.cover = frames.image_file_to_jpeg(name, max_edge=self.settings.thumbnail_max_edge or None,
                                                  quality=self.settings.jpeg_quality)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Thumbnail", f"Couldn't read that image:\n\n{exc}")
            return
        doc.cover_note = f"From {Path(name).name}"
        self._refresh_thumbs()
        self._refresh_ui()

    def remove_cover(self):
        if self.doc is not None:
            self.doc.cover = REMOVE
            self._refresh_thumbs()
            self._refresh_ui()

    def revert_cover(self):
        if self.doc is not None:
            self.doc.cover = UNCHANGED
            self.doc.cover_note = ""
            self._refresh_thumbs()
            self._refresh_ui()

    # ================================================================ suggestions
    def _tab_changed(self, _index: int):
        if self.tabs.currentWidget() is self.thumbs and self.doc is not None and self.doc.suggestions is None:
            self.find_suggestions()

    def _cancel_suggestions(self):
        if self._suggest_job is not None:
            self._suggest_job.cancel.set()
            self._suggest_job = None

    def find_suggestions(self):
        doc = self.doc
        if doc is None or self._busy or self._suggest_job is not None or doc.info.main_video is None:
            return
        doc.suggestions = []
        count = max(2, min(12, self.settings.suggestions_count))
        self.thumbs.set_suggest_busy("Looking through the video for good frames…")

        def work(job):
            found = suggest.suggest(doc.info, count=count, progress=job.report, cancel=job.cancel)
            return [(s.pts, pil_to_qimage(s.image)) for s in found]

        def done(items):
            if self._suggest_job is job:
                self._suggest_job = None
            if self.doc is not doc:
                return
            doc.suggestions = [(pts, QPixmap.fromImage(image)) for pts, image in items]
            self.thumbs.set_suggestions(doc.suggestions, doc.info.start_time, doc.info.duration or 0)

        def failed(exc, _tb):
            if self._suggest_job is job:
                self._suggest_job = None
            if self.doc is doc:
                doc.suggestions = None
                self.thumbs.clear_suggestions(f"Couldn't look for frames: {exc}")

        def progress(percent, _message):
            if self.doc is doc and percent >= 0:
                self.thumbs.set_suggest_busy(f"Looking through the video for good frames… {percent}%")

        job = workers.start(work, on_done=done, on_error=failed, on_progress=progress)
        self._suggest_job = job

    def _seek_paused(self, pts: float):
        if self.doc is None:
            return
        self.player.pause()
        self.player.seek_seconds(pts)

    def _show_suggestion(self, pts: float):
        self._seek_paused(pts)

    def _use_suggestion(self, pts: float):
        self._seek_paused(pts)
        self.capture_frame(pts)

    # ================================================================ chapters and details
    def add_chapter(self):
        doc = self.doc
        if doc is None:
            return
        self.player.pause()
        pts = self.player.current_pts()
        if pts is None:
            pts = self.player.player.position() / 1000
        if self.tabs.currentWidget() is not self.chapters:
            self.tabs.setCurrentWidget(self.chapters)
        self.chapters.add_at(pts)
        self.set_status(f"Chapter added at {fmt_duration(pts, millis=True)}")

    def _chapters_edited(self):
        if self.doc is not None:
            self.doc.chapters = [chapter_io.ChapterMark(m.start, m.name, m.auto) for m in self.chapters.marks]
            self._refresh_ui()

    def _chapter_named(self, name: str):
        if self.doc is not None and name.casefold() not in {t.casefold() for t in self.doc.tags}:
            self.tag_editor.add_tag(name)
            self.set_status(f"Tagged the file “{name}” for the chapter")

    def _meta_edited(self):
        if self.doc is not None:
            self.doc.meta = self.details.meta.value()
            self._refresh_ui()

    def _refresh_tag_names(self):
        self.chapters.set_tag_names([t.name for t in self.db.tags()])

    # ================================================================ library
    def show_library(self):
        from .library_window import LibraryWindow
        if self.library_window is None:
            self.library_window = LibraryWindow(self.db, self.settings, self, self)
            self.library_window.openRequested.connect(self.open_paths)
            self.library_window.finished.connect(lambda *_: setattr(self, "library_window", None))
            self.library_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            self.library_window.show()
            if self.settings.library_folders:
                self.library_window.scan()
        else:
            self.library_window.refresh()
            self.library_window.show()
        self.library_window.raise_()
        self.library_window.activateWindow()

    def release_for(self, paths: list[Path], then):
        """Before the library changes files: make sure the open one is saved and let go of."""
        doc = self.doc
        keys = {file_key(p) for p in paths}
        if doc is None or file_key(doc.path) not in keys:
            then()
            return
        if self._busy:
            QMessageBox.information(self, "Runemark", "Please wait until the current save finishes.")
            return
        if doc.dirty():
            QMessageBox.information(self, "Unsaved changes", f"{doc.path.name} is open with unsaved changes. "
                                    "Save it (or undo the changes) first.")
            return
        self._release_then(lambda _position: then())

    def files_changed(self, paths: list[Path]):
        doc = self.doc
        if doc is not None and file_key(doc.path) in {file_key(p) for p in paths}:
            self._load(doc.path, 0, status=f"Reloaded {doc.path.name} after the library changed it")

    def _library_saved(self, *paths: Path):
        for path in paths:
            library.reindex(self.db, path)
        if self.library_window is not None:
            self.library_window.refresh()

    # ================================================================ saving
    def _release_then(self, then):
        """Stop everything reading the open file, give the player a moment to let go, then continue."""
        position = self.player.unload()
        self.filmstrip.clear()
        self._cancel_suggestions()
        QTimer.singleShot(400, lambda: then(position))

    def _fields(self, tags: list[str], original_genre: list[str]) -> tuple[list[str], list[str]]:
        return route_fields(tags, is_genre=self.db.is_genre_tag,
                            is_known=lambda n: self.db.find_tag(n) is not None,
                            original_genre=original_genre)

    def save(self, after=None):
        doc = self.doc
        if doc is None or self._busy:
            return
        if doc.container == "mp4":
            self._save_mp4(doc, after)
        elif doc.container == "mkv":
            self._convert_and_save(doc, after)

    def _save_mp4(self, doc: Document, after=None):
        keywords, genre = self._fields(doc.tags, doc.state.genre)
        cover = doc.cover
        rewrite = doc.chapters_changed()
        meta = doc.meta if (doc.meta_changed() or rewrite) else UNCHANGED
        whole_cover = doc.whole_cover()
        marks = [chapter_io.ChapterMark(m.start, m.name, m.auto) for m in doc.chapters]
        if rewrite:
            dropped = chapter_io.extra_tracks_dropped(doc.info)
            if dropped:
                answer = QMessageBox.question(
                    self, "Write chapters", f"This file has {dropped} extra data track(s) (such as camera "
                    "timecode or GPS) that aren't kept when chapters are written. Continue?")
                if answer != QMessageBox.StandardButton.Yes:
                    return
        dialog = ProgressDialog("Saving", cancellable=rewrite, parent=self)
        self._busy = True
        position = 0

        def work(job):
            if rewrite:
                return save_mp4_with_chapters(doc.path, doc.info, marks, keywords=keywords, genre=genre,
                                              cover=whole_cover, meta=meta,
                                              explorer_mode=self.settings.explorer_writer,
                                              preserve_mtime=self.settings.preserve_modified_date,
                                              progress=job.report, cancel=job.cancel)
            return save_mp4(doc.path, keywords=keywords, genre=genre, cover=cover,
                            explorer_mode=self.settings.explorer_writer,
                            preserve_mtime=self.settings.preserve_modified_date, progress=job.report,
                            meta=meta)

        def done(outcome):
            self._busy = False
            dialog.finish()
            self.db.remember_file(doc.path, keywords)
            if outcome.problems:
                QMessageBox.warning(self, "Saved, with warnings",
                                    "The file was saved, but some things didn't read back as expected:\n\n• "
                                    + "\n• ".join(outcome.problems))
            message = f"Saved {doc.path.name}" + (" with its chapters" if rewrite else "") \
                + _explorer_note(outcome.explorer_method)
            self.set_status(message)
            doc.saved_tags = list(doc.tags)   # no longer dirty while the file reloads
            doc.cover = UNCHANGED
            doc.saved_meta = VideoMeta(**vars(doc.meta))
            doc.saved_chapters = [chapter_io.ChapterMark(m.start, m.name, m.auto) for m in doc.chapters]
            self._library_saved(doc.path)
            self._load(doc.path, position, status=message)
            if after:
                QTimer.singleShot(300, after)

        def failed(exc, _tb):
            self._busy = False
            dialog.finish()
            if isinstance(exc, chapter_io.Cancelled):
                self.set_status("Save cancelled; the file is unchanged")
                self.player.load(doc.path, doc.info, position)
                return
            self.set_status("Save failed")
            QMessageBox.critical(self, "Save failed", f"Couldn't save {doc.path.name}:\n\n{exc}\n\n"
                                 "The file is unchanged unless noted above. Details are in the log.")
            self.player.load(doc.path, doc.info, position)

        def begin(pos):
            nonlocal position
            position = pos
            job = workers.start(work, on_done=done, on_error=failed, on_progress=dialog.update_progress)
            dialog.cancelRequested.connect(job.cancel.set)

        dialog.show()
        self._release_then(begin)

    def _convert_and_save(self, doc: Document, after=None):
        plan = C.plan_conversion(doc.info)
        if doc.chapters_changed():
            C.use_marks(plan, doc.chapters)
        dialog = ConvertDialog(plan, self)
        if dialog.exec() != ConvertDialog.DialogCode.Accepted:
            return
        keywords, genre = self._fields(doc.tags, doc.state.genre)
        meta = VideoMeta(**vars(doc.meta))
        if doc.cover is UNCHANGED:
            cover = doc.state.cover if doc.state.cover else UNCHANGED
        elif doc.cover == REMOVE:
            cover = UNCHANGED
        else:
            cover = doc.cover
        progress = ProgressDialog("Converting to MP4", cancellable=True, parent=self)
        self._busy = True
        source = doc.path
        position = 0

        def work(job):
            tmp, sidecars = C.run_conversion(plan, progress=job.report, cancel=job.cancel)
            problems = C.verify_conversion(plan, tmp)
            if problems:
                C.cleanup([tmp, *sidecars])
                raise RuntimeError("The converted file didn't check out, so nothing was changed:\n• "
                                   + "\n• ".join(problems))
            job.report(-1, "Writing tags and thumbnail…")
            final = plan.output
            try:
                outcome = save_mp4(tmp, keywords=keywords, genre=genre, cover=cover,
                                   explorer_mode=self.settings.explorer_writer, meta=meta)
                os.replace(tmp, final)
            except BaseException:
                C.cleanup([tmp, *sidecars])
                raise
            explorer.notify_changed(final)
            recycle_error = None
            try:
                explorer.recycle(source)
            except Exception as exc:  # noqa: BLE001
                recycle_error = str(exc)
            return final, sidecars, outcome.problems, recycle_error

        def done(result):
            self._busy = False
            progress.finish()
            final, sidecars, problems, recycle_error = result
            self.db.move_file(source, final)
            self.db.remember_file(final, keywords)
            self._library_saved(final)
            self.queue = [final if file_key(p) == file_key(source) else p for p in self.queue]
            notes = []
            if sidecars:
                notes.append("Saved beside it: " + ", ".join(p.name for p in sidecars))
            if problems:
                notes.append("Some tags didn't read back as expected:\n• " + "\n• ".join(problems))
            if recycle_error:
                notes.append(f"The original MKV couldn't be moved to the Recycle Bin ({recycle_error}). "
                             "It's still there; delete it yourself once you're happy with the MP4.")
            message = f"Converted and saved {final.name}" + ("" if recycle_error else
                                                              " · MKV moved to the Recycle Bin")
            self.set_status(message)
            if notes:
                QMessageBox.information(self, "Converted", f"{final.name} is ready.\n\n" + "\n\n".join(notes))
            self.doc = None
            self._load(final, position, status=message)
            if after:
                QTimer.singleShot(300, after)

        def failed(exc, _tb):
            self._busy = False
            progress.finish()
            if isinstance(exc, C.Cancelled):
                self.set_status("Conversion cancelled; nothing changed")
            else:
                self.set_status("Conversion failed")
                QMessageBox.critical(self, "Conversion failed", f"{exc}\n\nThe MKV is unchanged.")
            self.player.load(doc.path, doc.info, position)

        def begin(pos):
            nonlocal position
            position = pos
            job = workers.start(work, on_done=done, on_error=failed, on_progress=progress.update_progress)
            progress.cancelRequested.connect(job.cancel.set)

        progress.show()
        self._release_then(begin)

    # ================================================================ database changes → files
    def _apply_file_changes(self, changes: list[FileChange]):
        self.tag_editor.refresh_database()
        files: dict[str, Path] = {}
        for change in changes:
            for f in change.files:
                files.setdefault(file_key(f), Path(f))
        doc = self.doc
        doc_key = file_key(doc.path) if doc else None
        skip_open = False
        if doc is not None and doc_key in files and doc.dirty():
            doc.tags = _transform(doc.tags, changes)
            self.tag_editor.set_tags(doc.tags)
            files.pop(doc_key)
            skip_open = True
            self._refresh_ui()
        if not files:
            if skip_open:
                self.toast("The open file was updated in the editor. Save it to write the change.")
            return
        summary = "\n".join(f"• {c.description}" for c in changes if c.description)
        answer = QMessageBox.question(
            self, "Update tagged files",
            f"{summary}\n\nUpdate the {len(files)} file(s) in your library that use it, so they match?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        genre_names = {t.name.casefold() for t in self.db.tags() if t.category_id in self.db.genre_categories()}
        known = {t.name.casefold() for t in self.db.tags()}
        canonical = {t.name.casefold(): t.name for t in self.db.tags()}
        reload_open = doc is not None and doc_key in files
        position = 0
        dialog = ProgressDialog("Updating files", cancellable=True, parent=self)
        mode, preserve = self.settings.explorer_writer, self.settings.preserve_modified_date
        self._busy = True

        def work(job):
            results, skipped = [], []
            items = list(files.values())
            for n, path in enumerate(items):
                if job.cancel.is_set():          # stop between files, never halfway through one
                    skipped = items[n:]
                    break
                job.report(int(n * 100 / len(items)), f"Updating {path.name}…")
                if not path.exists():
                    results.append((path, None, "file not found (moved or deleted?)"))
                    continue
                if path.suffix.lower() not in (".mp4", ".m4v"):
                    results.append((path, None, "not an MP4"))
                    continue
                try:
                    state = read_state(path)
                    tags = [canonical.get(t.casefold(), t) for t in _transform(state.all_tags(), changes)]
                    keywords, genre = route_fields(
                        tags, is_genre=lambda t: t.casefold() in genre_names,
                        is_known=lambda t: t.casefold() in known, original_genre=state.genre)
                    outcome = save_mp4(path, keywords=keywords, genre=genre, explorer_mode=mode,
                                       preserve_mtime=preserve)
                    results.append((path, keywords, "; ".join(outcome.problems)))
                except Exception as exc:  # noqa: BLE001
                    results.append((path, None, str(exc)))
            return results, skipped

        def done(outcome):
            results, skipped = outcome
            self._busy = False
            dialog.finish()
            failures = []
            for path, keywords, problem in results:
                if keywords is not None:
                    self.db.remember_file(path, keywords)
                if problem:
                    failures.append(f"{path.name}: {problem}")
            self._library_saved(*[path for path, keywords, _ in results if keywords is not None])
            updated = sum(1 for _, k, p in results if k is not None and not p)
            total = len(results) + len(skipped)
            self.set_status(f"Updated {updated} of {total} file(s)" + (" (stopped)" if skipped else ""))
            if skipped:
                failures.append("")
                failures.append(f"Stopped before these {len(skipped)} file(s); they still have the old tags. "
                                "Their tags are listed in the Library, where you can update them later:")
                failures.extend(f"• {p.name}" for p in skipped)
            if failures:
                title = "Stopped" if skipped else "Some files weren't updated"
                TextReportDialog(title, "\n".join(failures).strip(), self).exec()
            if reload_open and doc is not None:
                self._load(doc.path, position)

        def failed(exc, _tb):
            self._busy = False
            dialog.finish()
            QMessageBox.critical(self, "Update files", str(exc))
            if reload_open and doc is not None:
                self._load(doc.path, position)

        def begin(pos):
            nonlocal position
            position = pos
            job = workers.start(work, on_done=done, on_error=failed, on_progress=dialog.update_progress)
            dialog.cancelRequested.connect(job.cancel.set)

        dialog.show()
        if reload_open:
            self._release_then(begin)
        else:
            begin(0)

    # ================================================================ misc actions
    def open_settings(self):
        old_theme = self.settings.theme
        dialog = SettingsDialog(self.settings, self, db=self.db)
        dialog.databaseRestored.connect(self._database_restored)
        if dialog.exec():
            if self.settings.theme != old_theme:
                QMessageBox.information(self, "Appearance", "The new appearance applies the next time you start Runemark.")
            writer = "Windows" if explorer.windows_api_available() and self.settings.explorer_writer == "auto" \
                else "built-in writer"
            self.status_writer.setText(f"Explorer Tags: {writer}")

    def _database_restored(self):
        self.tag_editor.refresh_database()
        self._refresh_tag_names()
        if self.doc is not None:
            self.tag_editor.set_tags(self.doc.tags)
        if self.library_window is not None:
            self.library_window.refresh()
        self.set_status("Tag database restored from the backup")

    def run_self_test(self):
        dialog = TextReportDialog("Self-test", "Running checks…", self)
        progress = ProgressDialog("Self-test", parent=self)

        def done(results):
            progress.finish()
            dialog.set_text(selftest.report(results))
            dialog.exec()

        def failed(exc, tb):
            progress.finish()
            dialog.set_text(f"The self-test itself failed:\n{tb}")
            dialog.exec()

        workers.start(lambda job: selftest.run(job.report), on_done=done, on_error=failed,
                      on_progress=progress.update_progress)
        progress.show()

    def about(self):
        QMessageBox.about(self, "About Runemark",
                          f"<h3>Runemark {__version__}</h3>"
                          "<p>Tag MP4 and MKV videos, choose their thumbnails, and keep Explorer's Tags, "
                          "the file's own tags and Genre in sync.</p>"
                          "<p>Video tools: FFmpeg. Tags: mutagen. Interface: Qt for Python.</p>"
                          "<p>Copyright © 2026 Chris Bartlett. Free software under the GNU GPL v3 or later, "
                          "with no warranty.<br>"
                          "<a href='https://github.com/crbartlett84/runemark'>github.com/crbartlett84/runemark</a></p>")

    # ================================================================ state
    def _confirm_leave(self, then) -> bool:
        """Ask about unsaved changes. Returns True if it's fine to continue right away."""
        doc = self.doc
        if doc is None or not doc.dirty():
            return True
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved changes")
        box.setText(f"Save changes to {doc.path.name}?")
        save = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Don't save", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel:
            self._fill_combo()
            return False
        if clicked is save:
            self.save(after=then)
            return False
        doc.discard()
        return True

    def _refresh_ui(self):
        doc = self.doc
        dirty = bool(doc and doc.dirty())
        self.dirty_dot.setVisible(dirty)
        if doc is None:
            self.save_button.setEnabled(False)
            self.save_button.setText("Save")
            self.setWindowTitle("Runemark")
            self.tag_editor.set_enabled(False)
            self.tag_editor.set_tags([])
            self.tag_editor.show_sync(None, None)
            self.details.show_sections([])
            self.details.meta.set_meta(None)
            self.chapters.set_marks([])
            self.chapters.set_enabled(False)
            self.thumbs.set_container(None)
            self.thumbs.clear_suggestions()
            self._refresh_thumbs()
            self._fill_combo()
            return
        if doc.container == "mkv":
            self.save_button.setText("Convert and save")
            self.save_button.setToolTip("Convert to MP4 (video isn't re-encoded) and write tags and thumbnail")
            self.save_button.setEnabled(True)
        else:
            self.save_button.setText("Save")
            self.save_button.setToolTip("Write your changes to the file (Ctrl+S)")
            self.save_button.setEnabled(dirty or doc.needs_sync())
        self.setWindowTitle(f"{doc.path.name}{' •' if dirty else ''} — Runemark")

    def set_status(self, text: str, busy: bool = False):
        self.status_text.setText(text)
        self.status_progress.setVisible(busy)
        if busy:
            QTimer.singleShot(4000, lambda: self.status_progress.hide())

    def toast(self, text: str):
        self.status_text.setText(text)

    def closeEvent(self, event):  # noqa: N802
        if self._busy:
            QMessageBox.information(self, "Runemark", "Please wait until the current save finishes.")
            event.ignore()
            return
        if self.doc is not None and self.doc.dirty():
            box = QMessageBox(self)
            box.setWindowTitle("Unsaved changes")
            box.setText(f"Save changes to {self.doc.path.name} before closing?")
            save = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
            discard = box.addButton("Don't save", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is save:
                event.ignore()
                self.save(after=self.close)
                return
            if box.clickedButton() is not discard:
                event.ignore()
                return
            self.doc.discard()
        if self.library_window is not None:
            self.library_window.close()
        self.player.unload()
        self.settings.volume = self.player.volume.value()
        self.settings.muted = self.player.mute_button.isChecked()
        self.settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode()
        self.settings.splitter_state = bytes(self.splitter.saveState().toBase64()).decode()
        self.settings.save()
        event.accept()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        explorer.set_dark_title_bar(int(self.winId()), theme.current().dark)


def _transform(tags: list[str], changes: list[FileChange]) -> list[str]:
    out = list(tags)
    for change in changes:
        if change.kind == "rename":
            out = [change.new if t.casefold() == change.old.casefold() else t for t in out]
        elif change.kind == "remove":
            out = [t for t in out if t.casefold() != change.old.casefold()]
    return merge_unique(out)


def _fit(size: tuple[int, int], max_edge: int | None) -> str:
    width, height = size
    if max_edge and max(width, height) > max_edge:
        scale = max_edge / max(width, height)
        width, height = round(width * scale), round(height * scale)
    return f"{width}×{height}"


def _explorer_note(method: str | None) -> str:
    """How Explorer's Tags were written, for the status bar after a save."""
    return {"windows": " · Explorer Tags written by Windows",
            "builtin": " · Explorer Tags written by Runemark"}.get(method or "", "")


__all__ = ["MainWindow", "QPixmap", "QSize"]
