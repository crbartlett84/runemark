"""v0.5.0: file names in both directions. Reading details from names that follow a pattern, and renaming (and
sorting into folders) from details, with clash checks, sidecar files moving along, and undo."""
import os
import shutil
from pathlib import Path

import pytest

from runemark import library as L
from runemark import names as N
from runemark import people as P
from runemark.media.mp4meta import VideoMeta
from runemark.tagdb import FileRecord, TagDB
from runemark.tagsync import save_mp4


def rec(path, title="", year=None, people=(), rating=0):
    return FileRecord(str(path), [], "mp4", title, year, rating, False, None, [], False, None, list(people))


# --- reading details from names ------------------------------------------------------------------------------

def test_read_people_title_and_year_from_names():
    plans = N.plan_read(["/v/Ana Lee & Bea Cole - Beach Day (2019).mp4", "/v/Ana Lee-Snow(2021).mp4",
                         "/v/holiday clip.mp4"], "{People} - {Title} ({Year})")
    a, b, c = plans
    assert (a.matched, a.people, a.title, a.year) == (True, ["Ana Lee", "Bea Cole"], "Beach Day", 2019)
    assert (b.matched, b.people, b.title, b.year) == (True, ["Ana Lee"], "Snow", 2021)    # spacing is flexible
    assert not c.matched and c.title is None


@pytest.mark.parametrize("pattern", ["no fields", "{Title} {Title}", "{Studio} - {Title}"])
def test_bad_patterns(pattern):
    with pytest.raises(N.PatternError):
        N.name_regex(pattern)


# --- building names ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("values,expected", [
    ({"People": "Ana Lee", "Title": "Beach Day", "Year": "2019"}, "Ana Lee - Beach Day (2019)"),
    ({"People": "", "Title": "Beach Day", "Year": "2019"}, "Beach Day (2019)"),           # no stray " - "
    ({"People": "Ana Lee", "Title": "Beach Day", "Year": ""}, "Ana Lee - Beach Day"),     # no empty "()"
    ({"People": "Ana", "Title": 'What? Now: "yes"/no', "Year": "2019"}, "Ana - What Now - 'yes'-no (2019)"),
    ({"People": "", "Title": "", "Year": ""}, ""),
])
def test_build_name(values, expected):
    assert N.build_name("{People} - {Title} ({Year})", values) == expected


def test_build_name_with_folders():
    sep = os.sep
    assert N.build_name("{Year}\\{People}\\{Title}", {"Year": "2019", "People": "Ana", "Title": "Beach"}) == \
        f"2019{sep}Ana{sep}Beach"
    assert N.build_name("{Year}\\{People}\\{Title}", {"Year": "", "People": "Ana", "Title": "Beach"}) == \
        f"Ana{sep}Beach"                                                                   # no empty folder level


def test_plan_flags_clashes_existing_files_and_empty_names(tmp_path):
    (tmp_path / "Beach (2019).mp4").write_bytes(b"taken")
    plans = N.plan_renames([rec(tmp_path / "a.mp4", "Snow", 2020), rec(tmp_path / "b.mp4", "Snow", 2020),
                            rec(tmp_path / "c.mp4", "Beach", 2019), rec(tmp_path / "d.mp4"),
                            rec(tmp_path / "Lake (2018).mp4", "Lake", 2018)], "{Title} ({Year})")
    a, b, c, d, e = plans
    assert a.changes and Path(a.new).name == "Snow (2020).mp4"
    assert "same new name" in b.problem and not b.changes
    assert "already there" in c.problem
    assert "no title" in d.problem
    assert not e.problem and not e.changes                                                 # already right


# --- doing it ------------------------------------------------------------------------------------------------

def test_rename_moves_sidecars_keeps_the_library_and_undoes(tmp_path, media):
    folder = tmp_path / "v"
    folder.mkdir()
    video = folder / "clip.mp4"
    shutil.copy2(media["mp4"], video)
    (folder / "clip.nfo").write_text("<movie/>")
    (folder / "clip.en.srt").write_text("1\n")
    db = TagDB(tmp_path / "t.db")
    try:
        save_mp4(video, keywords=["Lake"], genre=[], meta=VideoMeta("Beach Day", 2019, "", 4))
        ana = db.add_person("Ana Lee")
        P.save_people(db, video, [(ana, [])], label="t")
        L.scan(db, [str(folder)])
        (record,) = db.library()
        plans = N.plan_renames([record], "{Year}\\{People} - {Title}", base=str(folder))
        batch, problems = N.apply_renames(db, plans)
        new = folder / "2019" / "Ana Lee - Beach Day.mp4"
        assert problems == [] and new.exists() and not video.exists()
        assert (folder / "2019" / "Ana Lee - Beach Day.nfo").exists()
        assert (folder / "2019" / "Ana Lee - Beach Day.en.srt").exists()
        assert [r.path for r in db.library()] == [str(new)]
        assert db.files_with_person(ana.id) == [str(new)] and db.history(new)            # people and undo follow

        assert N.undo_renames(db) == []
        assert video.exists() and (folder / "clip.nfo").exists() and (folder / "clip.en.srt").exists()
        assert not (folder / "2019").exists()                                            # the folder it made is gone
        assert [r.path for r in db.library()] == [str(video)] and db.renames() == []
    finally:
        db.close()


def test_case_only_rename(tmp_path, media):
    video = tmp_path / "beach day.mp4"
    shutil.copy2(media["mp4"], video)
    db = TagDB(tmp_path / "t.db")
    try:
        (plan,) = N.plan_renames([rec(video, "Beach Day")], "{Title}")
        assert plan.changes
        N.apply_renames(db, [plan])
        assert [p.name for p in tmp_path.glob("*.mp4")] == ["Beach Day.mp4"]
    finally:
        db.close()


def test_apply_read_fills_blanks_adds_people_and_undoes(tmp_path, media):
    from runemark.tagsync import read_state
    folder = tmp_path / "v"
    folder.mkdir()
    a, b = folder / "Ana Lee & Annie B - Beach Day (2019).mp4", folder / "Bea Cole - Snow (2021).mp4"
    shutil.copy2(media["mp4"], a)
    shutil.copy2(media["mp4"], b)
    save_mp4(b, keywords=["Lake"], genre=[], meta=VideoMeta("My own title", None, "", 0))
    db = TagDB(tmp_path / "t.db")
    try:
        bea = db.add_person("Bea Cole", ["Annie B"])                      # an alias in the first name
        plans = N.plan_read([str(a), str(b)], "{People} - {Title} ({Year})")
        assert N.apply_read(db, plans) == []
        sa, sb = read_state(a), read_state(b)
        assert (sa.meta.title, sa.meta.year) == ("Beach Day", 2019)
        assert sa.keywords == ["Ana Lee", "Bea Cole"]                     # Annie B is Bea; Ana Lee was created
        assert (sb.meta.title, sb.meta.year) == ("My own title", 2021)     # a title already there is kept
        assert sb.keywords == ["Lake", "Bea Cole"]
        assert sorted(p.name for p in db.people()) == ["Ana Lee", "Bea Cole"] and bea.uid
        assert L.undo_batch(db, db.last_batch()) == []
        assert read_state(b).meta.year is None and read_state(b).keywords == ["Lake"]
        N.apply_read(db, N.plan_read([str(b)], "{People} - {Title} ({Year})"), overwrite=True)
        assert read_state(b).meta.title == "Snow"
    finally:
        db.close()


# --- the dialog ------------------------------------------------------------------------------------------------

def test_names_dialog_previews_and_renames(tmp_path, media, monkeypatch):
    import time
    from PySide6.QtWidgets import QApplication
    from runemark.settings import Settings
    from runemark.ui.names_dialog import NamesDialog
    app = QApplication.instance() or QApplication([])
    folder = tmp_path / "v"
    folder.mkdir()
    for name, title in (("a.mp4", "Beach Day"), ("b.mp4", "Snow"), ("c.mp4", "")):
        shutil.copy2(media["mp4"], folder / name)
        save_mp4(folder / name, keywords=[], genre=[], meta=VideoMeta(title, 2019, "", 0))
    db = TagDB(tmp_path / "t.db")
    settings = Settings.load()
    monkeypatch.setattr(settings, "save", lambda: None)
    try:
        L.scan(db, [str(folder)])
        dialog = NamesDialog(db, db.library(), "rename", settings)
        dialog.pattern.setCurrentText("{Title} ({Year})")
        dialog._refresh()
        rows = {dialog.table.item(r, 0).text(): (dialog.table.item(r, 1).text(), dialog.table.item(r, 2).text())
                for r in range(dialog.table.rowCount())}
        assert rows["a.mp4"][0] == "Beach Day (2019).mp4" and "no title" in rows["c.mp4"][1]
        assert dialog.apply_button.text() == "Rename 2 video(s)"
        dialog.pattern.setCurrentText("{Studio}")
        dialog._refresh()
        assert "Unknown field" in dialog.error.text() and not dialog.apply_button.isEnabled()
        dialog.pattern.setCurrentText("{Title} ({Year})")
        dialog._refresh()
        dialog._apply()
        end = time.time() + 30
        while not dialog.applied and time.time() < end:
            app.processEvents()
            time.sleep(0.02)
        assert sorted(p.name for p in folder.glob("*.mp4")) == ["Beach Day (2019).mp4", "Snow (2019).mp4", "c.mp4"]
        assert settings.name_patterns["rename"] == "{Title} ({Year})" and len(dialog.moved) == 2
    finally:
        db.close()
