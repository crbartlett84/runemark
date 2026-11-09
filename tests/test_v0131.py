"""0.13.1: files missing from the library for more than 14 days are taken out of it, safely."""
import datetime as dt
import shutil

from runemark import library as L
from runemark.tagdb import TagDB


def _age(db, path, days):
    when = (dt.datetime.now() - dt.timedelta(days=days)).isoformat(timespec="seconds")
    db.conn.execute("UPDATE files SET missing_since = ? WHERE path = ?", (when, str(path)))
    db.commit()


def test_missing_for_two_weeks_is_forgotten(tmp_path, media):
    lib = tmp_path / "lib"
    lib.mkdir()
    keep, gone = lib / "keep.mp4", lib / "gone.mp4"
    shutil.copy(media["mp4"], keep)
    shutil.copy(media["mp4"], gone)
    for i in range(3):
        shutil.copy(media["mp4"], lib / f"other{i}.mp4")
    db = TagDB(tmp_path / "t.db")
    try:
        L.scan(db, [str(lib)])
        gone.unlink()
        r = L.scan(db, [str(lib)])
        assert r.missing == 1 and r.forgotten == 0                 # just gone: kept, marked missing
        first = db.conn.execute("SELECT missing_since FROM files WHERE path = ?", (str(gone),)).fetchone()[0]
        L.scan(db, [str(lib)])                                      # the date it was first missing stays
        assert db.conn.execute("SELECT missing_since FROM files WHERE path = ?", (str(gone),)).fetchone()[0] == first
        _age(db, gone, 13)
        assert L.scan(db, [str(lib)]).forgotten == 0
        _age(db, gone, 15)
        r = L.scan(db, [str(lib)])
        assert r.forgotten == 1
        assert {p.path for p in db.library()} == {str(p) for p in lib.iterdir()}
        assert L.scan(db, [str(lib)], forget_after_days=0).forgotten == 0
    finally:
        db.close()


def test_an_offline_folder_never_forgets_its_files(tmp_path, media):
    lib = tmp_path / "share"
    lib.mkdir()
    a = lib / "a.mp4"
    shutil.copy(media["mp4"], a)
    db = TagDB(tmp_path / "t.db")
    try:
        L.scan(db, [str(lib)])
        shutil.move(str(lib), str(tmp_path / "unplugged"))          # the share is offline
        L.scan(db, [str(lib)])
        _age(db, a, 30)
        assert L.scan(db, [str(lib)]).forgotten == 0
        assert [r.path for r in db.library()] == [str(a)]
    finally:
        db.close()


def test_coming_back_clears_missing(tmp_path, media):
    lib = tmp_path / "lib"
    lib.mkdir()
    a = lib / "a.mp4"
    shutil.copy(media["mp4"], a)
    db = TagDB(tmp_path / "t.db")
    try:
        L.scan(db, [str(lib)])
        shutil.move(str(a), str(tmp_path / "a.mp4"))
        L.scan(db, [str(lib)])
        shutil.move(str(tmp_path / "a.mp4"), str(a))
        L.scan(db, [str(lib)])
        row = db.conn.execute("SELECT missing, missing_since FROM files WHERE path = ?", (str(a),)).fetchone()
        assert row[0] == 0 and row[1] is None
    finally:
        db.close()
