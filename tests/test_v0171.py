"""0.17.1: a file moved or renamed outside Runemark (Explorer, the NAS) keeps its record at its new path."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from runemark import library as L
from runemark.tagdb import TagDB


@pytest.fixture(autouse=True)
def no_providers():
    L.clear_scan_providers()
    yield
    L.clear_scan_providers()


def _library(tmp_path, media, n=3):
    lib = tmp_path / "lib"
    (lib / "a").mkdir(parents=True)
    (lib / "b").mkdir()
    files = []
    for i in range(n):
        p = lib / "a" / f"v{i}.mp4"
        shutil.copy(media["mp4"], p)
        os.utime(p, (1_600_000_000 + i * 100, 1_600_000_000 + i * 100))     # each its own time
        files.append(p)
    return lib, files


def _save(db, path, tag="Kept"):
    assert L.apply_bulk(db, [path], L.BulkChange(add=[tag])) == []


def _saved(db):
    return sorted(Path(r.path).name for r in db.library() if r.saved_at and not r.missing)


def test_a_saved_file_moved_and_renamed_outside_runemark_stays_saved(tmp_path, media):
    lib, files = _library(tmp_path, media)
    db = TagDB(tmp_path / "t.db")
    try:
        L.scan(db, [str(lib)])
        _save(db, files[0])
        _save(db, files[1], "Another tag")                     # (the same video: a different size, once saved)
        history = len(db.history(files[0]))
        assert _saved(db) == ["v0.mp4", "v1.mp4"]
        moved = lib / "b" / "v0.mp4"
        renamed = lib / "b" / "another name.mp4"
        shutil.move(str(files[0]), str(moved))                  # moved to another folder
        shutil.move(str(files[1]), str(renamed))                # moved and renamed
        r = L.scan(db, [str(lib)])
        assert r.relinked == 2 and r.missing == 0
        assert _saved(db) == ["another name.mp4", "v0.mp4"]
        assert len(db.library()) == 3                           # no records left behind at the old paths
        assert len(db.history(moved)) == history and db.history(files[0]) == []
        assert db.file_tags(moved) and "Kept" in {r.tags[0] for r in db.library() if r.path == str(moved)}
        assert [a["label"] for a in db.activity()].count("Found again after being moved outside Runemark") == 2
        assert L.scan(db, [str(lib)]).relinked == 0             # done once
    finally:
        db.close()


def test_records_split_by_an_earlier_scan_are_joined_again(tmp_path, media, monkeypatch):
    """What 0.17.0 left behind: the old path missing, the new path a fresh record never saved."""
    lib, files = _library(tmp_path, media)
    db = TagDB(tmp_path / "t.db")
    try:
        L.scan(db, [str(lib)])
        _save(db, files[2])
        moved = lib / "b" / "elsewhere.mp4"
        shutil.move(str(files[2]), str(moved))
        with monkeypatch.context() as m:
            m.setattr(L, "find_moves", lambda *a, **k: [])
            L.scan(db, [str(lib)])
        assert _saved(db) == [] and [Path(r.path).name for r in db.library() if r.missing] == ["v2.mp4"]
        r = L.scan(db, [str(lib)])
        assert r.relinked == 1 and _saved(db) == ["elsewhere.mp4"]
        assert not any(rec.missing for rec in db.library()) and len(db.library()) == 3
    finally:
        db.close()


def test_copies_of_one_video_are_never_guessed(tmp_path, media):
    lib, files = _library(tmp_path, media)
    db = TagDB(tmp_path / "t.db")
    try:
        L.scan(db, [str(lib)])
        _save(db, files[0])
        shutil.copy2(files[0], lib / "b" / "copy 1.mp4")
        shutil.copy2(files[0], lib / "b" / "copy 2.mp4")
        files[0].unlink()
        r = L.scan(db, [str(lib)])
        assert r.relinked == 0 and r.missing == 1
        assert _saved(db) == []
        assert [Path(rec.path).name for rec in db.library() if rec.missing] == ["v0.mp4"]
    finally:
        db.close()


def test_a_file_saved_at_its_new_path_is_never_replaced(tmp_path, media):
    lib, files = _library(tmp_path, media)
    db = TagDB(tmp_path / "t.db")
    try:
        L.scan(db, [str(lib)])
        _save(db, files[0], "Old")
        copy = lib / "b" / "copy.mp4"
        shutil.copy2(files[0], copy)
        L.scan(db, [str(lib)])
        _save(db, copy, "New")                                  # saved, so it has its own record and history
        st = files[0].stat()
        os.utime(copy, (st.st_atime, st.st_mtime))
        files[0].unlink()
        r = L.scan(db, [str(lib)])
        assert r.relinked == 0 and r.missing == 1
    finally:
        db.close()


def test_a_changed_file_or_a_different_video_is_not_taken_for_a_move(tmp_path, media):
    lib, files = _library(tmp_path, media)
    db = TagDB(tmp_path / "t.db")
    try:
        L.scan(db, [str(lib)])
        _save(db, files[0])
        moved = lib / "b" / "v0.mp4"
        shutil.move(str(files[0]), str(moved))
        os.utime(moved, (1_700_000_000, 1_700_000_000))         # changed since (another time)
        r = L.scan(db, [str(lib)])
        assert r.relinked == 0 and r.missing == 1
    finally:
        db.close()


def _known(path, **extra):
    rec = {"path": str(path), "size": 100, "mtime": 5.0, "indexed": True, "container": "mp4", "title": "",
           "duration": 10.0, "chapters": 0, "saved": True, "history": True, "missing": True}
    rec.update(extra)
    return rec


def test_matching_checks_the_content_and_the_fingerprint(tmp_path, monkeypatch):
    db = TagDB(tmp_path / "t.db")
    try:
        new = tmp_path / "new.mp4"
        new.write_bytes(b"x")
        old = tmp_path / "gone.mp4"
        on_disk = {L.file_key(new): (new, 100, 5.0)}
        known = {L.file_key(old): _known(old)}
        same = lambda p: {"container": "mp4", "title": "", "duration": 10.0, "chapters": 0}  # noqa: E731
        assert L.find_moves(db, on_disk, {}, known, same) == [(str(old), new, same(new))]
        longer = lambda p: dict(same(p), duration=12.0)  # noqa: E731
        assert L.find_moves(db, on_disk, {}, known, longer) == []
        titled = lambda p: dict(same(p), title="Something else")  # noqa: E731
        assert L.find_moves(db, on_disk, {}, known, titled) == []
        later = {L.file_key(new): (new, 100, 6.0)}
        assert L.find_moves(db, later, {}, known, same) == []

        class FP:
            version, fps, crop = 2, 2.0, ""

            def __init__(self, data):
                self.data = data

            def to_bytes(self):
                return self.data

        db.save_fingerprint(old, 100, 5.0, FP(b"a" * 9))
        db.save_fingerprint(new, 100, 5.0, FP(b"b" * 9))
        assert L.find_moves(db, on_disk, {}, known, same) == []          # the pictures differ
        db.save_fingerprint(new, 100, 5.0, FP(b"a" * 9))
        assert len(L.find_moves(db, on_disk, {}, known, same)) == 1

        old.write_bytes(b"x")                                           # still there: a copy, not a move
        assert L.find_moves(db, on_disk, {}, known, same) == []
    finally:
        db.close()


def test_an_offline_drive_never_looks_like_a_move(tmp_path, monkeypatch):
    gone = tmp_path / "gone.mp4"
    assert L._gone(str(gone))
    real = os.path.isdir
    anchor = Path(str(gone)).anchor
    monkeypatch.setattr(os.path, "isdir", lambda p: False if p == anchor else real(p))
    assert not L._gone(str(gone))


def test_a_file_a_provider_describes_at_its_new_path_is_relinked(tmp_path, media, monkeypatch):
    lib, files = _library(tmp_path, media)
    db = TagDB(tmp_path / "t.db")
    try:
        L.scan(db, [str(lib)])
        _save(db, files[1])
        moved = lib / "b" / "v1 moved.mp4"
        shutil.move(str(files[1]), str(moved))
        st = moved.stat()
        record = dict(L.read_for_index(moved), path=str(moved), size=st.st_size, mtime=st.st_mtime)
        L.add_scan_provider("Test", lambda given, cancel: L.ScanListing([str(lib)], [record]))
        opened = []
        monkeypatch.setattr(L, "read_for_index", lambda p: opened.append(p) or {})
        r = L.scan(db, [str(lib)])
        assert r.relinked == 1 and opened == [] and _saved(db) == ["v1 moved.mp4"]
    finally:
        db.close()


def test_a_copy_that_was_there_all_along_keeps_its_own_record(tmp_path, media):
    lib, files = _library(tmp_path, media)
    db = TagDB(tmp_path / "t.db")
    try:
        L.scan(db, [str(lib)])
        _save(db, files[0])
        copy = lib / "b" / "copy.mp4"
        shutil.copy2(files[0], copy)
        L.scan(db, [str(lib)])
        db.conn.execute("UPDATE files SET first_seen = '2020-01-01T00:00:00' WHERE path = ?", (str(copy),))
        db.commit()
        files[0].unlink()
        assert L.scan(db, [str(lib)]).relinked == 0
        assert L.scan(db, [str(lib)]).relinked == 0                # nor once the old one is marked missing
        assert _saved(db) == [] and [Path(r.path).name for r in db.library() if r.missing] == ["v0.mp4"]
    finally:
        db.close()
