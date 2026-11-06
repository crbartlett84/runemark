"""0.10.0: save handlers and scan providers (plugin API 3)."""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from runemark import covers, explorer, library as L, saving
from runemark.media import chapters as chapter_io
from runemark.media.mp4meta import UNCHANGED, VideoMeta
from runemark.media.probe import probe
from runemark.settings import Settings
from runemark.tagdb import TagDB, file_key
from runemark.tagsync import read_state, save_mp4, save_mp4_with_chapters

needs_window = pytest.mark.skipif(
    bool(os.environ.get("GITHUB_ACTIONS")) or (sys.platform != "win32" and not os.environ.get("RUNEMARK_TRY_PLAYER")),
    reason="needs Qt Multimedia playback on a real desktop")


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_handlers():
    saving.clear_save_handlers()
    L.clear_scan_providers()
    yield
    saving.clear_save_handlers()
    L.clear_scan_providers()


def wait_until(qapp, condition, timeout=60.0):
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return False


def elsewhere(log=None, fail=None, problems=()):
    """A save handler that does what a remote one would: the saved version written beside the file by
    saving.write_copy, then swapped in."""
    def handler(request, progress):
        if log is not None:
            log.append(json.loads(json.dumps({"kind": request.kind, "changes": request.to_dict(),
                                              "expected": request.expected, "path": str(request.path)})))
        if fail:
            return saving.SaveResult.failed(fail)
        again = saving.SaveRequest.from_dict(request.path, request.to_dict(), request.expected)
        progress(10, "Saving elsewhere…")
        tmp = request.path.with_name(request.path.stem + ".runemark-elsewhere.mp4")
        found = saving.write_copy(again, request.path, tmp)
        os.replace(tmp, request.path)
        st = request.path.stat()
        return saving.SaveResult.handled(list(found) + list(problems), st.st_size, st.st_mtime)
    return handler


# ---------------------------------------------------------------- the request
def test_a_request_round_trips_as_plain_data(tmp_path):
    marks = [chapter_io.ChapterMark(0.0, "Chapter 1", True), chapter_io.ChapterMark(2.5, "Lake", end=4.0)]
    meta = VideoMeta("Beach", 2019, "Sand", 4, "Studio", ["https://example.com/a"], "2019-07-14")
    req = saving.SaveRequest(tmp_path / "a.mp4", ["Lake", "Sun"], ["Lake"], b"\xff\xd8jpeg", meta,
                             {"format": "runemark-data", "version": 1}, marks, True, 123, 1700000000.5, "undo")
    text = json.dumps(req.to_dict())                     # plain JSON
    back = saving.SaveRequest.from_dict(req.path, json.loads(text), req.expected)
    assert back.keywords == req.keywords and back.genre == req.genre and back.cover == req.cover
    assert vars(back.meta) == vars(meta) and back.data == req.data and back.kind == "undo"
    assert [(m.start, m.name, m.auto, m.end) for m in back.chapters] == [(0.0, "Chapter 1", True, None),
                                                                          (2.5, "Lake", False, 4.0)]
    assert back.preserve_mtime and back.expected == {"size": 123, "mtime": 1700000000.5}
    assert req.changed_fields() == ["keywords", "genre", "cover", "details", "data", "chapters"]

    plain = saving.SaveRequest(tmp_path / "a.mp4", ["Lake"], [])
    d = plain.to_dict()
    assert "cover" not in d and "meta" not in d and "data" not in d and "chapters" not in d
    back = saving.SaveRequest.from_dict(plain.path, d)
    assert back.cover is UNCHANGED and back.meta is UNCHANGED and back.data is UNCHANGED and not back.rewrite
    removed = saving.SaveRequest.from_dict(plain.path, dict(d, cover=None, data=None))
    assert removed.cover is None and removed.data is None
    for bad in ({"keywords": "x"}, {"cover": 5}, {"data": [1]}, {"format": 99}):
        with pytest.raises(ValueError):
            saving.SaveRequest.from_dict(plain.path, dict(d, **bad))


# ---------------------------------------------------------------- the writer, wherever it runs
def test_write_copy_makes_exactly_what_a_local_save_makes(tmp_path, media):
    cover = media["cover_jpg"].read_bytes()
    meta = VideoMeta("Beach", 2019, "Sand", 4, "Studio", ["https://example.com/a"])
    data = {"format": "runemark-data", "version": 1, "people": [{"name": "Someone"}]}
    for rewrite in (False, True):
        a, b = tmp_path / f"a{rewrite}.mp4", tmp_path / f"b{rewrite}.mp4"
        shutil.copy2(media["mp4"], a)
        shutil.copy2(media["mp4"], b)
        marks = [chapter_io.ChapterMark(0.0, "Chapter 1", True), chapter_io.ChapterMark(2.0, "Lake")] if rewrite \
            else None
        req = saving.SaveRequest(b, ["Lake", "Sun"], ["Lake"], cover, meta, data, marks)
        if rewrite:
            save_mp4_with_chapters(a, probe(a), marks, keywords=req.keywords, genre=req.genre, cover=cover,
                                   meta=meta, explorer_mode=explorer.MODE_BUILTIN, data=data)
        else:
            save_mp4(a, keywords=req.keywords, genre=req.genre, cover=cover, meta=meta, data=data,
                     explorer_mode=explorer.MODE_BUILTIN)
        before = b.read_bytes()
        out = tmp_path / f"out{rewrite}.mp4"
        assert saving.write_copy(req, b, out) == []
        assert b.read_bytes() == before                       # the source is left alone
        assert out.read_bytes() == a.read_bytes()             # byte for byte what the local writer made
        assert saving.verify(req, out) == []
        assert not list(tmp_path.glob("*.runemark-*"))        # no working files left behind


# ---------------------------------------------------------------- offering saves
def test_handled_saves_are_read_back_and_others_are_saved_here(tmp_path, media):
    path = tmp_path / "a.mp4"
    shutil.copy2(media["mp4"], path)
    stamp = saving.stamp(path)
    req = saving.SaveRequest(path, ["Lake"], [], meta=VideoMeta("Beach", 2019), expected_size=stamp[0],
                             expected_mtime=stamp[1])
    calls = []
    saving.add_save_handler("Passes", lambda r, p: (calls.append("passes"), None)[1])
    saving.add_save_handler("Breaks", lambda r, p: (calls.append("breaks"), 1 / 0))
    saving.add_save_handler("Elsewhere", elsewhere(calls))
    seen = []
    outcome = saving.save(req, progress=lambda p, m: seen.append((p, m)))
    assert calls[:2] == ["passes", "breaks"] and calls[2]["expected"] == {"size": stamp[0], "mtime": stamp[1]}
    assert outcome.problems == [] and read_state(path).keywords == ["Lake"] and read_state(path).meta.title == "Beach"
    assert (10, "Saving elsewhere…") in seen

    # a handler's warnings and what the read-back finds both come back as problems
    saving.clear_save_handlers()

    def liar(request, progress):
        return saving.SaveResult.handled(["the server noticed something"])
    saving.add_save_handler("Liar", liar)
    outcome = saving.save(saving.SaveRequest(path, ["Other"], []))
    assert outcome.problems[0] == "the server noticed something"
    assert any("Keywords read back" in p for p in outcome.problems)       # it didn't write: the check says so

    # nobody takes it: saved here exactly as before
    saving.clear_save_handlers()
    saving.add_save_handler("Nope", lambda r, p: saving.SaveResult.not_handled("not mine"))
    outcome = saving.save(saving.SaveRequest(path, ["Here"], []))
    assert outcome.problems == [] and read_state(path).keywords == ["Here"]


def test_a_failed_save_changes_nothing_and_says_why(tmp_path, media):
    path = tmp_path / "a.mp4"
    shutil.copy2(media["mp4"], path)
    before = path.read_bytes()
    saving.add_save_handler("Elsewhere", elsewhere(fail="it changed since it was opened"))
    with pytest.raises(saving.SaveFailed, match="it changed since it was opened"):
        saving.save(saving.SaveRequest(path, ["Lake"], []))
    assert path.read_bytes() == before


def test_the_stamp_a_share_reports_late_is_corrected(tmp_path, media):
    path = tmp_path / "a.mp4"
    shutil.copy2(media["mp4"], path)
    old = saving.stamp(path)
    req = saving.SaveRequest(path, [], [], expected_size=old[0], expected_mtime=old[1])
    saving._remember_written(req, saving.SaveResult.handled([], old[0] + 10, old[1] + 5))
    assert saving.stamp(path) == (old[0] + 10, old[1] + 5)     # the share still says the old one: use the new
    os.utime(path, (old[1] + 100, old[1] + 100))
    assert saving.stamp(path)[1] == pytest.approx(old[1] + 100)  # it changed again since: the real one
    saving._written.clear()


def test_undo_goes_through_the_handler_with_its_chapters(tmp_path, media):
    path = tmp_path / "a.mp4"
    shutil.copy2(media["mp4"], path)
    db = TagDB(tmp_path / "tags.db")
    try:
        info = probe(path)
        state = read_state(path)
        marks = [chapter_io.ChapterMark(0.0, "Chapter 1", True), chapter_io.ChapterMark(2.0, "Lake")]
        db.record_history(path, **L.history_fields(state), chapters=chapter_io.marks_to_json([]),
                          label="Saved in the editor")
        save_mp4_with_chapters(path, info, marks, keywords=["Lake"], genre=[], cover=None, meta=state.meta)
        calls = []
        saving.add_save_handler("Elsewhere", elsewhere(calls))
        outcome = L.revert_file(db, db.history(path)[0], handlers=True)
        assert outcome.problems == []
        assert calls[0]["kind"] == "undo" and calls[0]["changes"]["chapters"] == []
        assert probe(path).chapters == [] and read_state(path).keywords == []
        # without handlers=True (the Library's batch undo) nothing is offered
        db.record_history(path, **L.history_fields(read_state(path)), label="again")
        L.revert_file(db, db.history(path)[0])
        assert len(calls) == 1
    finally:
        db.close()


# ---------------------------------------------------------------- scan providers
def _library(tmp_path, media):
    covered, local = tmp_path / "share" / "Feed", tmp_path / "share" / "Local"
    (covered / "sub").mkdir(parents=True)
    local.mkdir(parents=True)
    files = []
    for n, folder in enumerate((covered, covered / "sub", covered, local)):
        p = folder / f"v{n}.mp4"
        shutil.copy2(media["mp4_cover"] if n == 0 else media["mp4"], p)
        save_mp4(p, keywords=[f"Tag {n}", "Shared"], genre=["Shared"] if n % 2 else [],
                 meta=VideoMeta(f"Title {n}", 2000 + n, "", n % 6, f"Studio {n}"),
                 data={"format": "runemark-data", "version": 1, "chapter_ends": {"0.000": 1.5}})
        files.append(p)
    return covered, local, files


def _rows(db):
    return sorted((r.path, r.tags, r.container, r.title, r.year, r.rating, r.has_cover, round(r.duration or 0, 3),
                   r.genre, r.missing, r.people, r.studio, r.chapters) for r in db.library())


def _record(path: Path, **extra):
    st = path.stat()
    rec = dict(L.read_for_index(path), path=str(path), size=st.st_size, mtime=st.st_mtime)
    rec.update(extra)
    return rec


def test_a_scan_through_a_provider_matches_a_local_scan_without_opening_files(tmp_path, media, monkeypatch):
    covered, local, files = _library(tmp_path, media)
    folders = [str(covered.parent)]
    db_local, db_prov = TagDB(tmp_path / "local.db"), TagDB(tmp_path / "prov.db")
    try:
        L.scan(db_local, folders)
        records = [_record(p) for p in files[:3]]
        asked = []

        def listing(given, cancel):
            asked.append(list(given))
            return L.ScanListing([str(covered)], list(records))
        L.add_scan_provider("Test", listing)
        opened = []
        real = L.read_for_index
        monkeypatch.setattr(L, "read_for_index", lambda p: (opened.append(Path(p).name), real(p))[1])
        result = L.scan(db_prov, folders)
        assert asked == [folders] and opened == ["v3.mp4"]          # only the file outside the covered folder
        assert result.found == 4 and result.read == 4
        assert _rows(db_prov) == _rows(db_local)

        # unchanged: nothing imported, nothing opened
        opened.clear()
        assert L.scan(db_prov, folders).read == 0 and opened == []

        # 0.10.1: a provider's record is used only when it describes the file on disk
        records[1] = dict(records[1], mtime=records[1]["mtime"] + 30, title="Changed")    # the provider is wrong
        records[2] = {"path": records[2]["path"], "size": records[2]["size"], "mtime": records[2]["mtime"],
                      "unreadable": True}
        gone = records.pop(0)                                     # it doesn't list v0 (not indexed there yet)
        later = files[2].stat().st_mtime + 60
        os.utime(files[2], (later, later))                        # changed here: read here, as a scan always has
        result = L.scan(db_prov, folders)
        by_path = {r.path: r for r in db_prov.library()}
        assert result.missing == 0 and not any(r.missing for r in by_path.values())   # all still on disk
        assert by_path[str(files[1])].title == "Title 1"          # unchanged on disk: the odd record isn't used
        assert opened == ["v2.mp4"] and result.read == 1
        # the provider catches up with a file changed on the other computer: imported, not opened
        save_mp4(files[1], keywords=["Server"], genre=[], meta=VideoMeta("Server title", 2001))
        records[0] = _record(files[1])                        # (v0's record went: v1's is first now)
        opened.clear()
        assert L.scan(db_prov, folders).read == 1 and opened == []
        assert {r.path: r for r in db_prov.library()}[str(files[1])].title == "Server title"
        # only a file that isn't on disk is missing; the provider's record for it is ignored
        records.append(gone)
        files[0].unlink()
        result = L.scan(db_prov, folders)
        by_path = {r.path: r for r in db_prov.library()}
        assert by_path[gone["path"]].missing and result.missing == 1 and opened == []
        # a provider that fails or has nothing: the whole library is scanned here again
        L.clear_scan_providers()
        L.add_scan_provider("Broken", lambda given, cancel: 1 / 0)
        opened.clear()
        L.scan(db_prov, folders, rescan_all=True)
        assert sorted(opened) == ["v1.mp4", "v2.mp4", "v3.mp4"]
    finally:
        db_local.close()
        db_prov.close()


def test_a_provider_covering_a_subfolder_and_skips(tmp_path, media):
    covered, local, files = _library(tmp_path, media)
    sub = covered / "sub"
    db = TagDB(tmp_path / "tags.db")
    try:
        L.add_scan_provider("Test", lambda given, cancel: {"covered": [str(sub), str(tmp_path / "elsewhere")],
                                                           "records": [_record(files[1]), _record(files[3])]})
        L.scan(db, [str(covered.parent)], skip=[str(local)])
        paths = {r.path for r in db.library()}
        # the covered subfolder from the provider (its record outside it is ignored), the rest walked here,
        # the skipped folder left out, a covered folder outside the library ignored
        assert paths == {str(files[0]), str(files[1]), str(files[2])}
        assert L.scan_providers()[0].last.covered == [str(sub)]
    finally:
        db.close()


def test_folders_a_provider_leaves_out_are_scanned_here(tmp_path, media):
    covered, local, files = _library(tmp_path, media)
    sub = covered / "sub"
    db = TagDB(tmp_path / "tags.db")
    try:
        L.scan(db, [str(covered.parent)])                      # everything known, read here
        listing = L.ScanListing([str(covered)], [_record(files[0]), _record(files[2]), _record(files[1])],
                                [str(sub)])
        L.add_scan_provider("Test", lambda given, cancel: listing)
        os.utime(files[1], (time.time() + 60, time.time() + 60))
        result = L.scan(db, [str(covered.parent)])
        by_path = {r.path: r for r in db.library()}
        assert not any(r.missing for r in by_path.values()) and result.missing == 0
        assert result.read == 1                                 # the changed one in the left-out folder, read here
        assert not listing.covers(str(files[1])) and listing.covers(str(files[0]))
        assert L.scan_providers()[0].last.covers(str(files[2]))
    finally:
        db.close()


def test_thumbnails_come_from_the_provider_when_shown(tmp_path, media):
    covered, local, files = _library(tmp_path, media)
    asked = []

    def thumb(path):
        asked.append(Path(path).name)
        return b"" if Path(path).name == "v2.mp4" else b"\xff\xd8small"
    L.add_scan_provider("Test", lambda given, cancel: L.ScanListing([str(covered)], []), thumb)
    db = TagDB(tmp_path / "tags.db")
    try:
        L.scan(db, [str(covered.parent)])
    finally:
        db.close()
    cache = tmp_path / "covers"
    cache.mkdir()
    assert covers.thumbnail(files[0], cache) == b"\xff\xd8small"
    assert covers.thumbnail(files[0], cache) == b"\xff\xd8small" and asked == ["v0.mp4"]     # cached
    assert covers.thumbnail(files[2], cache) is None                                          # it has none
    assert covers.thumbnail(files[3], cache) is None and asked == ["v0.mp4", "v2.mp4"]        # not covered: read
    data = covers.small_jpeg(media["cover_jpg"].read_bytes())
    assert data[:2] == b"\xff\xd8"


# ---------------------------------------------------------------- the main window and plugin API 3
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


def _plugin(folder: Path, api: int, body: str) -> None:
    folder.mkdir(parents=True)
    (folder / "plugin.json").write_text(json.dumps({"name": folder.name, "api": api}), encoding="utf-8")
    (folder / "plugin.py").write_text(body, encoding="utf-8")


@needs_window
def test_plugin_api_3_save_handler_in_the_editor_with_history_and_undo(tmp_path, media, qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from runemark import plugins
    _plugin(tmp_path / "plugins" / "remote", 3, "def register(api):\n    api._remote = api\n")
    _plugin(tmp_path / "plugins" / "old", 2, "def register(api):\n    pass\n")
    _plugin(tmp_path / "plugins" / "future", 4, "def register(api):\n    pass\n")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    shown = []
    for name in ("critical", "warning", "information"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: shown.append(a[2]))
    win, db, (a, b) = _window(tmp_path, media)
    try:
        host = plugins.load_all(win, [], tmp_path / "plugins")
        state = {p.key: (p.loaded, p.error) for p in host.plugins}
        assert state["remote"] == (True, "") and state["old"] == (True, "")
        assert not state["future"][0] and "needs a newer Runemark" in state["future"][1]
        api = plugins.PluginAPI(win, host, next(p for p in host.plugins if p.key == "remote"))
        assert api.api_version == 3 and api.SaveResult is saving.SaveResult and api.ScanListing is L.ScanListing
        calls = []
        api.add_save_handler(elsewhere(calls))

        win.open_paths([a, b])
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == a)
        opened_stamp = win.doc.stamp
        assert opened_stamp == saving.open_stamp(a) and opened_stamp[:2] == saving.stamp(a)     # 0.10.1: + fp
        win._tags_edited(["Lake"])
        win.chapters.add_at(1.0)
        first = win.doc
        win.save()
        assert wait_until(qapp, lambda: win.saves.idle() and win.doc is not None and win.doc is not first
                          and win.doc.path == a and not win.doc.dirty() and calls, 30)       # opened again
        assert calls[0]["kind"] == "save" and calls[0]["expected"] == {"size": opened_stamp[0],
                                                                       "mtime": opened_stamp[1],
                                                                       "fp": opened_stamp[2]}
        assert [c["name"] for c in calls[0]["changes"]["chapters"]] == ["Chapter 1", "Chapter 2"]
        assert read_state(a).keywords == ["Lake"] and [c.title for c in probe(a).chapters] == ["Chapter 1",
                                                                                               "Chapter 2"]
        assert db.history(a)[0].chapters == []                 # the history (and so Undo) works as for local saves
        assert not win.toasts.toasts or win.toasts.toasts[-1].kind != "error"

        second = win.doc
        win.undo_last_save()                                   # through the handler too
        assert wait_until(qapp, lambda: shown or (not win._busy and len(calls) == 2 and win.doc is not None
                          and win.doc is not second and win.doc.path == a and not win.doc.read_only), 30)
        assert shown == []
        assert calls[1]["kind"] == "undo" and calls[1]["changes"]["chapters"] == []
        assert wait_until(qapp, lambda: read_state(a).keywords == [] and probe(a).chapters == [], 10)

        # failed: the error toast with Retry, the edits kept, the file unchanged
        saving.clear_save_handlers()
        api.add_save_handler(elsewhere(calls, fail="RuneTest: it changed since it was opened"))
        assert wait_until(qapp, lambda: win.doc is not None and not win._busy, 10)
        before = a.read_bytes()
        win._tags_edited(["Lake"])
        win.save()
        assert wait_until(qapp, lambda: win.saves.idle() and win.toasts.toasts
                          and win.toasts.toasts[-1].kind == "error", 20)
        toast = win.toasts.toasts[-1]
        assert "it changed since it was opened" in toast.text and win.doc.dirty() and a.read_bytes() == before
        assert toast.buttons[0].text() == "Retry"

        # change_file goes the same way
        saving.clear_save_handlers()
        api.add_save_handler(elsewhere(calls))
        results = []
        api.change_file(b, {"add_tags": ["Lake"]}, lambda p, why: results.append((p, why)))
        assert wait_until(qapp, lambda: results, 30) and results == [(b, "")]
        assert calls[-1]["path"] == str(b) and read_state(b).keywords == ["Lake"]
    finally:
        _close(win, db, qapp)
