"""0.9.0: background saves, timeline ticks and hover previews, suggested cuts, J/K/L, a better Library, chapter
undo, and the plugin API 2 additions."""
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from runemark import library as L
from runemark.media import chapters as chapter_io
from runemark.media.probe import probe
from runemark.settings import Settings
from runemark.tagdb import FileRecord, TagDB
from runemark.tagsync import read_state, save_mp4_with_chapters

from conftest import ff

needs_window = pytest.mark.skipif(
    bool(os.environ.get("GITHUB_ACTIONS")) or (sys.platform != "win32" and not os.environ.get("RUNEMARK_TRY_PLAYER")),
    reason="needs Qt Multimedia playback on a real desktop")


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def wait_until(qapp, condition, timeout=60.0):
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return False


def record(path="C:/v/a.mp4", **kw):
    base = dict(path=path, tags=[], container="mp4", title="", year=None, rating=0, has_cover=False, duration=60.0,
                genre=[], missing=False, saved_at=None)
    base.update(kw)
    return FileRecord(**base)


# ---------------------------------------------------------------- the save queue
def test_saves_run_one_at_a_time_in_order(qapp):
    from runemark.ui.save_queue import SaveJob, SaveQueue
    released, running, order, overlap = [], [], [], []
    lock = threading.Lock()

    def release(path, then):
        released.append(path.name)
        then(123)

    def work_for(name):
        def work(job):
            with lock:
                running.append(name)
                if len(running) > 1:
                    overlap.append(name)
            time.sleep(0.05)
            job.report(50, "halfway")
            with lock:
                running.remove(name)
            return name
        return work

    queue = SaveQueue(release)
    jobs = [queue.add(SaveJob(Path(f"/v/{n}.mp4"), work_for(n), on_done=order.append)) for n in ("a", "b", "a")]
    assert queue.pending("/v/a.mp4") is jobs[2] and not queue.idle()
    idle = []
    queue.when_idle(lambda: idle.append(True))
    assert wait_until(qapp, queue.idle, 10)
    assert order == ["a", "b", "a"] and not overlap          # never two writes at once, same file or not
    assert released == ["a.mp4", "b.mp4", "a.mp4"] and jobs[0].position_ms == 123
    assert idle == [True] and queue.pending("/v/a.mp4") is None


def test_a_failed_save_reports_and_the_next_one_still_runs(qapp):
    from runemark.ui.save_queue import SaveJob, SaveQueue
    failures, done, waited = [], [], []

    def bad(job):
        raise OSError("disk full")

    queue = SaveQueue()
    first = queue.add(SaveJob(Path("/v/x.mp4"), bad, on_failed=failures.append))
    queue.add(SaveJob(Path("/v/y.mp4"), lambda job: "ok", on_done=done.append))
    assert queue.when_done("/v/x.mp4", waited.append)
    assert not queue.when_done("/v/nothing.mp4", waited.append)
    assert wait_until(qapp, queue.idle, 10)
    assert [str(e) for e in failures] == ["disk full"] and done == ["ok"] and waited == [False]
    assert first.state == "failed"
    again = queue.add(first.clone())                          # Retry
    assert again is not first and queue.pending("/v/x.mp4") is again
    assert wait_until(qapp, queue.idle, 10)


def test_status_line_summary(qapp):
    from runemark.ui.save_queue import SaveJob, SaveQueue
    gate = threading.Event()
    queue = SaveQueue()
    queue.add(SaveJob(Path("/v/big.mp4"), lambda job: (job.report(42, ""), gate.wait(5))))
    queue.add(SaveJob(Path("/v/next.mp4"), lambda job: None))
    assert wait_until(qapp, lambda: "42%" in queue.summary(), 10)
    assert queue.summary() == "Saving big.mp4… 42% · 1 more waiting"
    gate.set()
    assert wait_until(qapp, queue.idle, 10) and queue.summary() == ""


def test_toasts_stack_and_problems_stay(qapp):
    from PySide6.QtWidgets import QWidget
    from runemark.ui.toast import ToastHost
    host_widget = QWidget()
    host_widget.resize(800, 600)
    host = ToastHost(host_widget)
    clicked = []
    info = host.show("Saved a.mp4")
    error = host.show("Couldn't save b.mp4", "error", [("Retry", lambda: clicked.append("retry"))])
    assert info.timer.isActive() and not error.timer.isActive()      # errors stay until closed
    host.show("Saved a.mp4")                                          # the same text again replaces the old one
    assert [t.text for t in host.toasts] == ["Couldn't save b.mp4", "Saved a.mp4"]
    error.buttons[0].click()
    assert clicked == ["retry"] and [t.text for t in host.toasts] == ["Saved a.mp4"]
    for n in range(5):
        host.show(f"message {n}")
    assert len(host.toasts) == ToastHost.MAX


# ---------------------------------------------------------------- timeline
def test_timeline_ticks_are_chapter_starts_never_ends():
    from runemark.ui.player import chapter_ticks
    marks = [chapter_io.ChapterMark(0.0, "A"), chapter_io.ChapterMark(10.0, "B", end=14.0),
             chapter_io.ChapterMark(20.0, "C", end=25.5)]
    assert chapter_ticks(marks) == [0.0, 10.0, 20.0]
    assert 14.0 not in chapter_ticks(marks) and 25.5 not in chapter_ticks(marks)


def test_ticks_are_drawn_at_starts_and_nothing_at_ends(qapp):
    from runemark.ui.player import JumpSlider
    slider = JumpSlider(Qt.Orientation.Horizontal)
    slider.resize(600, 30)
    slider.setRange(0, 60_000)
    slider.setValue(59_000)                       # the handle out of the way, at the far end
    plain = slider.grab().toImage()
    slider.set_chapter_starts([10.0, 30.0])
    ticked = slider.grab().toImage()
    y = slider.height() // 2
    for start in (10.0, 30.0):
        x = slider.x_for_ms(start * 1000)
        assert any(ticked.pixel(x + dx, y) != plain.pixel(x + dx, y) for dx in (-1, 0, 1))
    end_x = slider.x_for_ms(20_000)               # where chapter 1's end would be: nothing drawn
    assert ticked.pixel(end_x, y) == plain.pixel(end_x, y)


def test_hover_preview_reuses_pictures_and_asks_once_per_spot(qapp, tmp_path):
    from PySide6.QtCore import QPoint
    from PIL import Image
    from runemark.ui.player import HoverPreview, JumpSlider
    calls = []

    def fake_grab(path, info, seconds, max_edge=200):
        calls.append(seconds)
        return Image.new("RGB", (64, 36), (200, 20, 20))

    slider = JumpSlider(Qt.Orientation.Horizontal)
    slider.resize(400, 30)
    preview = HoverPreview(None)
    preview.grab = fake_grab
    path = tmp_path / "v.mp4"
    for t in (10.1, 10.4, 10.6):                   # the same couple of seconds: one grab
        preview.want(path, object(), t, QPoint(100, 100), slider)
    assert wait_until(qapp, lambda: preview.key(10.4) in preview.cache, 10)
    assert calls == [preview.key(10.6)]
    preview.want(path, object(), 10.2, QPoint(100, 100), slider)
    qapp.processEvents()
    assert preview.picture.pixmap() is not None and not preview.picture.pixmap().isNull()
    preview.hide_now()
    assert not preview.isVisible()


def test_shuttle_jkl():
    from runemark.ui.player import Shuttle
    s = Shuttle()
    assert s.press("L") == (1, 1.0) and s.label() == ""
    assert s.press("L") == (1, 1.5) and s.press("L") == (1, 2.0) and s.press("L") == (1, 4.0)
    assert s.press("L") == (1, 4.0) and s.label() == "4×"
    assert s.press("J") == (1, 2.0)                # J while going forward fast slows down first
    assert s.press("K") == (0, 1.0) and s.label() == ""   # 0.9.1: K stops at 1×, and the readout shows 1×
    assert s.press("J") == (-1, 1.0) and s.label() == "◀ 1×"
    assert s.press("J") == (-1, 1.5)               # J again: back faster
    assert s.press("L") == (1, 1.0)


# ---------------------------------------------------------------- suggested cuts
def make_cuts_video(path: Path) -> Path:
    ff("-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=2", "-f", "lavfi", "-i", "color=c=red:s=320x240:r=25:d=2",
       "-f", "lavfi", "-i", "mandelbrot=s=320x240:r=25", "-filter_complex",
       "[2]trim=duration=2,setpts=PTS-STARTPTS[m];[0][1][m]concat=n=3:v=1:a=0",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path))
    return path


def test_cuts_are_found_cached_and_snapped(tmp_path):
    from runemark.media import scenes
    video = make_cuts_video(tmp_path / "cuts.mp4")
    info = probe(video)
    assert scenes.cached_cuts(video, info) is None
    cuts = scenes.detect_cuts(info)
    assert [round(c, 1) for c in cuts] == [2.0, 4.0]
    scenes.store_cuts(video, info, cuts)
    assert scenes.cached_cuts(video, info) == pytest.approx(cuts)
    assert scenes.nearest(cuts, 2.7) == pytest.approx(2.0)           # within a second: snaps
    assert scenes.nearest([2.0, 6.0], 3.2) is None                    # too far from either
    assert scenes.neighbour(cuts, 2.0, 1) == pytest.approx(4.0)       # ] from a cut goes to the next one
    assert scenes.neighbour(cuts, 2.0, -1) is None
    assert scenes.thin([1.0, 1.2, 1.9, 5.0]) == [1.0, 5.0]
    moved = tmp_path / "moved.mp4"
    shutil.move(video, moved)
    scenes.move_cache(video, moved)
    assert scenes.cached_cuts(moved, probe(moved)) == pytest.approx(cuts)


def test_cut_cache_survives_a_tag_save_but_not_another_picture(tmp_path, media):
    from runemark.media import scenes
    from runemark.tagsync import save_mp4
    path = tmp_path / "a.mp4"
    shutil.copy2(media["mp4"], path)
    info = probe(path)
    scenes.store_cuts(path, info, [1.5])
    save_mp4(path, keywords=["Lake"], genre=[])
    assert scenes.cached_cuts(path, probe(path)) == [1.5]
    other = probe(media["vfr"])
    assert scenes.cached_cuts(path, other) is None


# ---------------------------------------------------------------- chapters in the save history
def test_undo_last_save_puts_the_chapters_back(tmp_path, media):
    path = tmp_path / "a.mp4"
    shutil.copy2(media["mp4"], path)
    db = TagDB(tmp_path / "tags.db")
    try:
        info = probe(path)
        state = read_state(path)
        before = chapter_io.marks_from_info(info)          # none
        marks = [chapter_io.ChapterMark(0.0, "Chapter 1", True), chapter_io.ChapterMark(2.0, "Lake")]
        db.record_history(path, **L.history_fields(state), chapters=chapter_io.marks_to_json(before),
                          label="Saved in the editor")
        save_mp4_with_chapters(path, info, marks, keywords=["Lake"], genre=[], cover=None, meta=state.meta)
        assert [c.title for c in probe(path).chapters] == ["Chapter 1", "Lake"]
        entry = db.history(path)[0]
        assert entry.chapters == []
        L.revert_file(db, entry)
        assert probe(path).chapters == [] and read_state(path).keywords == []

        # a save that didn't touch the chapters leaves them alone on undo
        info = probe(path)
        save_mp4_with_chapters(path, info, marks, keywords=[], genre=[], cover=None, meta=state.meta)
        db.record_history(path, **L.history_fields(read_state(path)), label="tags only")
        assert db.history(path)[0].chapters is None
        L.revert_file(db, db.history(path)[0])
        assert [c.title for c in probe(path).chapters] == ["Chapter 1", "Lake"]
    finally:
        db.close()


def test_history_from_before_090_still_reads(tmp_path):
    db = TagDB(tmp_path / "tags.db")
    try:
        db.record_history(tmp_path / "x.mp4", keywords=["a"], genre=[], label="old")
        assert db.history(tmp_path / "x.mp4")[0].chapters is None
        marks = [chapter_io.ChapterMark(1.25, "A"), chapter_io.ChapterMark(0.0, "Chapter 1", True, end=0.5)]
        data = chapter_io.marks_to_json(marks)
        assert data == [{"start": 0.0, "name": "Chapter 1", "auto": True}, {"start": 1.25, "name": "A", "auto": False}]
        assert [(m.start, m.name, m.auto) for m in chapter_io.marks_from_json(data)] == \
            [(0.0, "Chapter 1", True), (1.25, "A", False)]
    finally:
        db.close()


# ---------------------------------------------------------------- the Library
def test_smart_filters_and_or():
    a = record("C:/v/a.mp4", tags=["Lake", "Summer"], studio="Blue", rating=4, year=2020, has_cover=True,
               people=["Ana"], saved_at="2026-01-01")
    b = record("C:/v/b.mp4", tags=["Lake"], studio="Red", rating=0, year=2018, chapters=3)
    c = record("C:/v/c.mp4", tags=[], studio="Blue Sky", missing=False)
    gone = record("C:/v/gone.mp4", tags=["Lake"], missing=True)
    both = L.SmartFilter("Lake from Blue", [L.SmartRule("tag", "has", "lake"), L.SmartRule("studio", "is", "blue")])
    either = L.SmartFilter("Lake or Blue", both.rules, match="any")
    assert [r.path for r in (a, b, c, gone) if L.smart_matches(r, both)] == ["C:/v/a.mp4"]
    assert [r.path for r in (a, b, c, gone) if L.smart_matches(r, either)] == ["C:/v/a.mp4", "C:/v/b.mp4"]
    good = L.SmartFilter("Good", [L.SmartRule("rating", "ge", "4")])
    unrated = L.SmartFilter("Unrated", [L.SmartRule("rating", "le", "0")])
    assert L.smart_matches(a, good) and not L.smart_matches(b, good) and L.smart_matches(b, unrated)
    assert L.smart_matches(c, L.SmartFilter("x", [L.SmartRule("studio", "contains", "sky")]))
    assert L.smart_matches(b, L.SmartFilter("x", [L.SmartRule("chapters", "yes")]))
    assert L.smart_matches(a, L.SmartFilter("x", [L.SmartRule("person", "has", "ana"),
                                                  L.SmartRule("thumbnail", "yes"), L.SmartRule("saved", "yes")]))
    assert L.smart_matches(b, L.SmartFilter("x", [L.SmartRule("year", "le", "2019"), L.SmartRule("tag", "lacks",
                                                                                                "Summer")]))
    again = L.SmartFilter.from_dict(either.to_dict())
    assert again == either and "or" in either.describe() and "lake" in either.describe()


def test_library_shows_titles_and_plugin_columns(qapp):
    from runemark.plugins import LibraryColumn
    from runemark.ui.library_model import COLUMNS, LibraryModel
    model = LibraryModel()
    model.set_records([record("C:/v/scene_0412.mp4", title="Beach Day"), record("C:/v/other.mp4")])
    assert model.data(model.index(0, 0)) == "Beach Day"            # the title, when there is one
    assert model.data(model.index(1, 0)) == "other.mp4"            # else the file name
    assert model.data(model.index(0, 1)) == "scene_0412.mp4"
    known, asked = {}, []
    model.request_plugin_values = lambda column, path: asked.append(path)
    model.set_plugin_columns([LibraryColumn("Lore", "Lore", known.get, fetch=lambda p: None)])
    assert model.columnCount() == len(COLUMNS) + 1 and model.headerData(len(COLUMNS), Qt.Orientation.Horizontal) == "Lore"
    assert model.data(model.index(0, len(COLUMNS))) == "" and asked == ["C:/v/scene_0412.mp4"]
    known["C:/v/scene_0412.mp4"] = "by id"
    assert model.data(model.index(0, len(COLUMNS))) == "by id"
    model.sort(len(COLUMNS), Qt.SortOrder.AscendingOrder)
    assert model.record(0).path == "C:/v/scene_0412.mp4"           # blanks last


def test_library_window_drop_downs_pins_and_maximised(tmp_path, qapp):
    from runemark.ui.library_window import LibraryWindow
    db = TagDB(tmp_path / "tags.db")
    try:
        for i in range(3):
            db.index_file(tmp_path / f"c{i}.mp4", tags=["Lake"] if i else [], container="mp4", title=f"C{i}")
        settings = Settings.load()
        settings.smart_filters = [L.SmartFilter("Lakes", [L.SmartRule("tag", "has", "Lake")]).to_dict()]
        w = LibraryWindow(db, settings, host=None)
        view = w.filter.view()
        widest = max(view.fontMetrics().horizontalAdvance(w.filter.itemText(i)) for i in range(w.filter.count()))
        assert view.minimumWidth() >= widest                      # the Inbox list isn't cut off any more
        pins = [w.pins.itemAt(i).widget() for i in range(w.pins.count())]
        assert [p.text() for p in pins] == ["Lakes · 2"]
        pins[0].click()
        assert w.active_smart == "Lakes" and w.model.rowCount() == 2
        pins = [w.pins.itemAt(i).widget() for i in range(w.pins.count())]
        pins[0].click()
        assert w.active_smart is None and w.model.rowCount() == 3
        assert settings.library_maximized is True
        w.show_window()
        assert wait_until(qapp, w.isVisible, 5)
        w.close()
    finally:
        db.close()


# ---------------------------------------------------------------- the main window
def _window(tmp_path, media, names=("a.mp4", "b.mp4")):
    from runemark.ui.main_window import MainWindow
    paths = []
    for n in names:
        p = tmp_path / n
        shutil.copy2(media["mp4"], p)
        paths.append(p)
    db = TagDB(tmp_path / "tags.db")
    db.add_tag("Lake", db.add_category("Places").id)
    win = MainWindow(db, Settings.load())
    win.show()
    return win, db, paths


def _close(win, db, qapp):
    wait_until(qapp, win.saves.idle, 60)
    if win.doc is not None:
        win.doc.discard()
    win.close()
    db.close()


@needs_window
def test_save_and_next_goes_on_while_the_save_runs(tmp_path, media, qapp):
    win, db, (a, b) = _window(tmp_path, media)
    try:
        gate = threading.Event()
        real = win._queue_mp4_save

        def slow_save(doc, after=None, on_failed=None):          # a big file: the write takes a while
            job = real(doc, after, on_failed)
            work = job.work
            job.work = lambda j: (gate.wait(20), work(j))[1]
            return job
        win._queue_mp4_save = slow_save
        win.open_paths([a, b])
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == a)
        win._tags_edited(["Lake"])
        win.chapters.add_at(1.0)
        win.save_and_next()
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == b, 20)
        assert not win.saves.idle() and win.saves.pending(a) is not None    # b is open while a still saves
        assert read_state(a).keywords == []
        assert "a.mp4" in win.status_saves.text()

        win.open_file(a)                                          # back to a while it's saving: read-only
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == a)
        assert win.doc.read_only and not win.tags_tab.isEnabled() and not win.save_button.isEnabled()
        win.add_chapter()                                         # nothing changes
        assert len(win.doc.chapters) == 1                         # 0.11.0: no extra chapter at 0:00
        gate.set()
        assert wait_until(qapp, lambda: win.saves.idle() and win.doc is not None and not win.doc.read_only
                          and win.doc.path == a and win.tags_tab.isEnabled(), 30)
        assert read_state(a).keywords == ["Lake"] and [c.title for c in probe(a).chapters] == ["Start", "Chapter 1"]
        assert db.history(a)[0].chapters == []                     # chapters are part of the undo history now
    finally:
        _close(win, db, qapp)


@needs_window
def test_a_failed_save_comes_back_with_retry(tmp_path, media, qapp, monkeypatch):
    from runemark import saving as MW            # 0.10.0: the editor's saves are written through runemark.saving
    win, db, (a,) = _window(tmp_path, media, ("a.mp4",))
    try:
        win.open_paths([a])
        assert wait_until(qapp, lambda: win.doc is not None)
        calls = []
        real = MW.save_mp4

        def broken(*args, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("the disk went away")
            return real(*args, **kw)
        monkeypatch.setattr(MW, "save_mp4", broken)
        win._tags_edited(["Lake"])
        win.save()
        assert wait_until(qapp, lambda: win.saves.idle() and win.toasts.toasts, 20)
        toast = win.toasts.toasts[-1]
        assert toast.kind == "error" and "the disk went away" in toast.text and win.doc.dirty()
        toast.buttons[0].click()                                  # Retry
        assert wait_until(qapp, lambda: win.saves.idle() and read_state(a).keywords == ["Lake"], 20)
    finally:
        _close(win, db, qapp)


@needs_window
def test_closing_waits_for_saves(tmp_path, media, qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    win, db, (a,) = _window(tmp_path, media, ("a.mp4",))
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kw: (asked.append(args[2]),
                                                                       QMessageBox.StandardButton.Yes)[1])
    try:
        gate = threading.Event()
        real = win._queue_mp4_save

        def slow_save(doc, after=None, on_failed=None):
            job = real(doc, after, on_failed)
            work = job.work
            job.work = lambda j: (gate.wait(20), work(j))[1]
            return job
        win._queue_mp4_save = slow_save
        win.open_paths([a])
        assert wait_until(qapp, lambda: win.doc is not None)
        win._tags_edited(["Lake"])
        win.save()
        assert not win.close()                                    # asked, and stays open while it writes
        assert asked and "still being written" in asked[0] and win.isVisible()
        gate.set()
        assert wait_until(qapp, lambda: not win.isVisible(), 30)
        assert read_state(a).keywords == ["Lake"]
    finally:
        gate.set()
        db.close()


@needs_window
def test_chapter_undo_redo_and_snapping_and_session_keys(tmp_path, media, qapp):
    win, db, (a, b) = _window(tmp_path, media)
    try:
        win.open_paths([a, b])
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == a)
        win.chapters.add_at(1.0)
        win.chapters.add_at(3.0)
        assert [m.start for m in win.doc.chapters] == [1.0, 3.0]          # 0.11.0: no chapter at 0:00 as well
        assert win.player.slider.chapter_starts == [1.0, 3.0]
        win.undo_chapters()
        assert [m.start for m in win.doc.chapters] == [1.0] and win.player.slider.chapter_starts == [1.0]
        win.undo_chapters()
        assert win.doc.chapters == [] and not win.doc.dirty()
        win.redo_chapters()
        win.redo_chapters()
        assert [m.start for m in win.doc.chapters] == [1.0, 3.0]

        win.doc.cuts = [2.0, 4.0]                                 # as Find cuts leaves them
        win.player.seek_seconds(4.6)
        win.add_chapter()
        assert 4.0 in [round(m.start, 3) for m in win.doc.chapters]

        win.set_session_chapter_keys({"1": "Lake", "2": "Dock", "x": "ignored"}, "Acts in this scene")
        assert win.chapter_key_name("1") == "Lake" and win.session_keys.isVisibleTo(win)
        assert "Acts in this scene" in win.session_keys.text() and "Dock" in win.chapters.keys_line.text()
        win.doc.discard()
        win.open_file(b)
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == b)
        assert win.chapter_key_name("1") is None and not win.session_keys.isVisibleTo(win)
    finally:
        _close(win, db, qapp)


@needs_window
def test_plugin_api_2(tmp_path, media, qapp):
    from PySide6.QtWidgets import QLabel, QPushButton
    from runemark import plugins
    folder = tmp_path / "plugins" / "demo"
    folder.mkdir(parents=True)
    (folder / "plugin.json").write_text('{"name": "Demo", "api": 2}', encoding="utf-8")
    (folder / "plugin.py").write_text(
        "from PySide6.QtWidgets import QLabel, QPushButton\n"
        "def register(api):\n"
        "    api.add_tags_panel('Demo strip', QLabel('hello'))\n"
        "    api.add_header_widget(QPushButton('Approve'))\n"
        "    api.add_library_column('Demo', lambda p: 'x')\n"
        "    api._demo = api\n", encoding="utf-8")
    win, db, (a, b) = _window(tmp_path, media)
    try:
        host = plugins.load_all(win, [], folder.parent)
        assert [p.loaded for p in host.plugins] == [True], host.plugins[0].error
        api = plugins.PluginAPI(win, host, host.plugins[0])
        assert win.tags_tab.count() == 2 and win.tags_tab.widget(1).title == "Demo strip"
        assert any(isinstance(w, QPushButton) and w.text() == "Approve" for w in win.header.findChildren(QPushButton))
        assert [c.title for c in host.library_columns] == ["Demo"]

        win.open_paths([a, b])
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == a)
        win.doc.meta.title = "Beach Day"
        win.doc.meta.year = 2019
        assert api.name_from_details() == "Beach Day (2019)"
        assert api.open_list() == [a, b]

        results = []
        api.change_file(b, {"title": "Second", "add_tags": ["Lake"]}, lambda p, why: results.append((p, why)))
        assert wait_until(qapp, lambda: results, 30) and results == [(b, "")]
        assert read_state(b).keywords == ["Lake"] and read_state(b).meta.title == "Second"
        assert db.history(b)                                     # undoable like any save
        api.toast("Moved", "Undo", lambda: results.append("undo"))
        win.toasts.toasts[-1].buttons[0].click()
        assert results[-1] == "undo"
        win.doc.discard()
        assert api.open_next() and wait_until(qapp, lambda: win.doc is not None and win.doc.path == b)
    finally:
        _close(win, db, qapp)
