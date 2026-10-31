"""Data-safety fixes after v0.2.0: metadata Runemark doesn't edit is left alone, originals are never
deleted where there's no Recycle Bin, read-only files fail early, and interrupted saves can be cleaned up."""
import os
import stat
import time

import pytest
from mutagen.mp4 import MP4

from runemark import explorer, tagsync
from runemark.media import mp4meta
from runemark.media.mp4meta import VideoMeta, read_mp4_tags, write_mp4_tags
from runemark.tagsync import SaveError, leftover_temp_files, read_state, remove_file, save_mp4

DAY, DESC, CMT = mp4meta.YEAR_KEY, "desc", "\xa9cmt"


def set_raw(path, **values):
    mp4 = MP4(path)
    if mp4.tags is None:
        mp4.add_tags()
    for key, value in values.items():
        mp4.tags[key] = [value]
    mp4.save()


def raw(path, key):
    tags = MP4(path).tags or {}
    return str(tags[key][0]) if tags.get(key) else ""


# --- ©day: a full date isn't cut down to the year -------------------------------------------------

def test_full_date_kept_when_year_unchanged(copy_of):
    path = copy_of("mp4")
    set_raw(path, **{DAY: "2019-07-14T10:00:00Z"})
    assert read_mp4_tags(path).year == 2019
    write_mp4_tags(path, keywords=["x"], genre=[], meta=VideoMeta("Title", 2019, "", None))
    assert raw(path, DAY) == "2019-07-14T10:00:00Z"


def test_year_written_when_changed_or_cleared(copy_of):
    path = copy_of("mp4")
    set_raw(path, **{DAY: "2019-07-14T10:00:00Z"})
    write_mp4_tags(path, keywords=[], genre=[], meta=VideoMeta("", 2021, "", None))
    assert raw(path, DAY) == "2021"
    write_mp4_tags(path, keywords=[], genre=[], meta=VideoMeta("", None, "", None))
    assert raw(path, DAY) == ""


def test_bulk_rating_style_save_keeps_full_date(copy_of):
    """The Library's bulk rating rebuilds meta from what it read; that must not truncate the date."""
    path = copy_of("mp4")
    set_raw(path, **{DAY: "2018-12-25", DESC: "Christmas morning"})
    state = read_state(path)
    meta = VideoMeta(state.meta.title, state.meta.year, state.meta.description, 4)
    outcome = save_mp4(path, keywords=["x"], genre=[], explorer_mode=explorer.MODE_BUILTIN, meta=meta)
    assert outcome.ok, outcome.problems
    assert raw(path, DAY) == "2018-12-25"
    assert raw(path, DESC) == "Christmas morning"


# --- ©cmt (Explorer's Comments) vs desc ------------------------------------------------------------

def test_separate_comment_survives_description_edit(copy_of):
    path = copy_of("mp4")
    set_raw(path, **{DESC: "Old description", CMT: "Shot on the old camera"})
    write_mp4_tags(path, keywords=[], genre=[], meta=VideoMeta("", None, "New description", None))
    assert raw(path, DESC) == "New description"
    assert raw(path, CMT) == "Shot on the old camera"


def test_mirrored_comment_follows_description(copy_of):
    path = copy_of("mp4")
    set_raw(path, **{DESC: "Same text", CMT: "Same text"})
    write_mp4_tags(path, keywords=[], genre=[], meta=VideoMeta("", None, "Updated", None))
    assert (raw(path, DESC), raw(path, CMT)) == ("Updated", "Updated")


def test_comment_only_file_is_edited_in_place(copy_of):
    """A file with only Explorer's Comments shows that as the description; editing it updates both."""
    path = copy_of("mp4")
    set_raw(path, **{CMT: "From Explorer"})
    assert read_mp4_tags(path).description == "From Explorer"
    write_mp4_tags(path, keywords=[], genre=[], meta=VideoMeta("", None, "Edited", None))
    assert (raw(path, DESC), raw(path, CMT)) == ("Edited", "Edited")


def test_unchanged_description_touches_nothing(copy_of):
    path = copy_of("mp4")
    set_raw(path, **{DESC: "Keep", CMT: "Different comment"})
    write_mp4_tags(path, keywords=["x"], genre=[], meta=VideoMeta("", None, "Keep", None))
    assert (raw(path, DESC), raw(path, CMT)) == ("Keep", "Different comment")


# --- Recycle Bin ------------------------------------------------------------------------------------

@pytest.mark.parametrize("where", [r"\\nas\media\film.mkv", "//nas/media/film.mkv"])
def test_network_paths_have_no_recycle_bin(monkeypatch, where):
    monkeypatch.setattr(explorer, "IS_WINDOWS", True)
    assert not explorer.has_recycle_bin(where)
    with pytest.raises(explorer.NoRecycleBin):
        explorer.recycle(where)


def test_unknown_drive_type_keeps_the_file(monkeypatch, tmp_path):
    """If the drive type can't be read, keep the file rather than risk a permanent delete."""
    monkeypatch.setattr(explorer, "IS_WINDOWS", True)
    import ctypes

    class Broken:
        def __getattr__(self, name):
            raise OSError("no kernel32 here")
    monkeypatch.setattr(ctypes, "windll", Broken(), raising=False)
    target = tmp_path / "film.mkv"
    target.write_bytes(b"x")
    with pytest.raises(explorer.NoRecycleBin):
        explorer.recycle(r"C:\Videos\film.mkv")
    assert target.exists()


@pytest.mark.parametrize("drive_type,expected", [(3, True), (2, False), (4, False), (5, False)])
def test_only_fixed_drives_recycle(monkeypatch, drive_type, expected):
    monkeypatch.setattr(explorer, "IS_WINDOWS", True)
    import ctypes

    class Kernel32:
        def GetDriveTypeW(self, root):  # noqa: N802 (Windows API name)
            assert root == "C:\\"
            return drive_type

    class WinDLL:
        kernel32 = Kernel32()
    monkeypatch.setattr(ctypes, "windll", WinDLL(), raising=False)
    assert explorer.has_recycle_bin(r"C:\Videos\film.mkv") is expected


# --- Read-only files ---------------------------------------------------------------------------------

def test_read_only_file_fails_early_without_leftovers(copy_of, monkeypatch):
    path = copy_of("mp4_faststart")
    real_access = os.access
    monkeypatch.setattr(tagsync.os, "access",
                        lambda p, mode: False if os.fspath(p) == os.fspath(path) and mode == os.W_OK
                        else real_access(p, mode))
    before = path.read_bytes()
    with pytest.raises(SaveError, match="read-only"):
        save_mp4(path, keywords=["x"], genre=[], explorer_mode=explorer.MODE_BUILTIN)
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.runemark-*"))


def test_working_copy_does_not_inherit_read_only(tmp_path):
    src = tmp_path / "a.mp4"
    src.write_bytes(b"data" * 1000)
    os.chmod(src, stat.S_IREAD)
    dst = tmp_path / "a.runemark-tmp.mp4"
    try:
        tagsync.copy_with_progress(src, dst)
        assert dst.stat().st_mode & stat.S_IWRITE
        assert int(dst.stat().st_mtime) == int(src.stat().st_mtime)
    finally:
        os.chmod(src, stat.S_IREAD | stat.S_IWRITE)


def test_remove_file_clears_read_only(tmp_path):
    p = tmp_path / "x.runemark-tmp.mp4"
    p.write_bytes(b"x")
    os.chmod(p, stat.S_IREAD)
    assert remove_file(p)
    assert not p.exists()
    assert remove_file(p)  # already gone is fine


# --- Leftover working files ----------------------------------------------------------------------------

def test_leftovers_found_only_for_this_file_and_only_when_old(tmp_path):
    video = tmp_path / "Holiday.mp4"
    video.write_bytes(b"v")
    old = time.time() - 3600
    names = ["Holiday.runemark-tmp.mp4", "Holiday.runemark-chapters.mp4",
             "Holiday.runemark-chapters.runemark-tmp.mp4"]
    for name in names:
        (tmp_path / name).write_bytes(b"partial")
        os.utime(tmp_path / name, (old, old))
    fresh = tmp_path / "Holiday.runemark-tmp.mp4.part"  # a save still running elsewhere: too new
    fresh.write_bytes(b"in progress")
    other = tmp_path / "Holiday 2.runemark-tmp.mp4"     # belongs to a different video
    other.write_bytes(b"x")
    os.utime(other, (old, old))
    found = [p.name for p in leftover_temp_files(video)]
    assert found == sorted(names)


def test_leftovers_with_glob_characters_in_the_name(tmp_path):
    video = tmp_path / "Clip [1080p].mp4"
    video.write_bytes(b"v")
    left = tmp_path / "Clip [1080p].runemark-tmp.mp4"
    left.write_bytes(b"partial")
    old = time.time() - 3600
    os.utime(left, (old, old))
    assert leftover_temp_files(video) == [left]


# --- Backups in folders with special characters -----------------------------------------------------

# "?" can't appear in a Windows file name at all, so that case only runs elsewhere
_FOLDERS = ["bk #1", "100% sure", "plain"] + ([] if os.name == "nt" else ["what?"])


@pytest.mark.parametrize("folder", _FOLDERS)
def test_backup_check_and_restore_with_special_characters(tmp_path, folder):
    from runemark.tagdb import TagDB
    db = TagDB(tmp_path / "live.db")
    try:
        target = tmp_path / folder / "tags-backup.db"
        target.parent.mkdir()
        db.backup_to(target)
        counts = TagDB.check_backup(target)
        assert set(counts) == {"tags", "categories", "files"}
        db.restore_from(target)
    finally:
        db.close()


def test_read_only_uri_shapes(tmp_path):
    from pathlib import Path
    from runemark.tagdb import _read_only_uri
    uri = _read_only_uri(tmp_path / "bk #1" / "t.db")
    assert uri.startswith("file:/") and uri.endswith("/bk%20%231/t.db?mode=ro")
    assert "#" not in uri and " " not in uri
    if os.name == "nt":
        assert uri.startswith("file:/" + Path(tmp_path).resolve().drive)       # file:/C:/...
    else:
        assert uri == "file:" + (Path(tmp_path).resolve() / "bk%20%231" / "t.db").as_posix() + "?mode=ro"


# --- Unhandled errors ----------------------------------------------------------------------------------

def test_excepthook_logs_and_shows_once(monkeypatch, caplog):
    import sys
    from runemark import app
    shown = []
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    app.install_excepthook(show=shown.append)
    try:
        raise ValueError("boom")
    except ValueError:
        with caplog.at_level("ERROR"):
            sys.excepthook(*sys.exc_info())
    assert [str(e) for e in shown] == ["boom"]
    assert "Unhandled error" in caplog.text and "ValueError: boom" in caplog.text
