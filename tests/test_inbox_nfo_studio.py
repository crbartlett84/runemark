"""0.5.0: the studio field, reading details from .nfo files, chapter-name suggestions and the Library inbox."""
import shutil
import time

import pytest

from runemark import library as L
from runemark import nfo
from runemark import people as P
from runemark.media import chapters as CH
from runemark.media.mp4meta import VideoMeta, read_mp4_tags
from runemark.media.probe import probe
from runemark.tagdb import TagDB
from runemark.tagsync import read_state, save_mp4, save_mp4_with_chapters

WHISPARR_STYLE = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<movie>
  <title>Beach Day</title>
  <plot>A day at the beach.

Then a swim.</plot>
  <premiered>2019-07-14</premiered>
  <studio>Sunny Films</studio>
  <actor>
    <name>Ana Lee</name>
    <role />
  </actor>
  <actor>
    <name>Bo Park</name>
  </actor>
  <actor>
    <name>ana lee</name>
  </actor>
</movie>
https://example.com/scene/123
"""


# --- studio ---------------------------------------------------------------------------------------------------

def test_studio_round_trip_and_none_leaves_it_alone(tmp_path, media):
    path = tmp_path / "v.mp4"
    shutil.copy2(media["mp4"], path)
    outcome = save_mp4(path, keywords=["A"], genre=[], meta=VideoMeta("T", 2020, "", 3, "Sunny  Films"))
    assert outcome.ok, outcome.problems
    assert read_mp4_tags(path).studio == "Sunny Films" and read_state(path).meta.studio == "Sunny Films"
    # a change that doesn't know the studio (studio=None) keeps it
    save_mp4(path, keywords=["A"], genre=[], meta=VideoMeta("T", 2020, "", 5))
    assert read_state(path).meta.studio == "Sunny Films"
    # "" removes it
    save_mp4(path, keywords=["A"], genre=[], meta=VideoMeta("T", 2020, "", 5, ""))
    assert read_state(path).meta.studio is None
    # ffprobe (and so Stash, Jellyfin...) sees it as STUDIO
    save_mp4(path, keywords=["A"], genre=[], meta=VideoMeta("T", 2020, "", 5, "Sunny Films"))
    assert probe(path).tag("studio") == "Sunny Films"


def test_studio_in_library_search_nfo_export_and_undo(tmp_path, media):
    folder = tmp_path / "v"
    folder.mkdir()
    path = folder / "a.mp4"
    shutil.copy2(media["mp4"], path)
    save_mp4(path, keywords=["Lake"], genre=[], meta=VideoMeta("Beach", 2019, "", 0, "Sunny Films"))
    db = TagDB(tmp_path / "t.db")
    try:
        L.scan(db, [str(folder)])
        (rec,) = db.library()
        assert rec.studio == "Sunny Films" and db.studios() == ["Sunny Films"]
        assert L.matches(rec, L.Search(text="sunny"))
        assert "<studio>Sunny Films</studio>" in L.nfo_text(rec)
        # a bulk rating keeps the studio, and undo puts a changed studio back
        assert L.apply_bulk(db, [path], L.BulkChange(rating=4)) == []
        assert read_state(path).meta.studio == "Sunny Films"
        state = read_state(path)
        save_mp4(path, keywords=state.keywords, genre=[], meta=VideoMeta("Beach", 2019, "", 4, "Other"))
        db.record_history(path, **L.history_fields(state), label="t")
        L.revert_file(db, db.history(path)[0])
        assert read_state(path).meta.studio == "Sunny Films"
    finally:
        db.close()


# --- .nfo -----------------------------------------------------------------------------------------------------

def test_parse_nfo_like_whisparr_writes_it(tmp_path):
    f = tmp_path / "Beach.nfo"
    f.write_text(WHISPARR_STYLE, encoding="utf-8")
    d = nfo.parse_nfo(f)
    assert (d.title, d.year, d.studio) == ("Beach Day", 2019, "Sunny Films")
    assert d.description == "A day at the beach.\n\nThen a swim."
    assert d.people == ["Ana Lee", "Bo Park"]                       # duplicates dropped


@pytest.mark.parametrize("text", ["https://example.com/only-a-link", "<movie><title>broken", "", "<tvshow/>"])
def test_unusable_nfo_is_ignored(tmp_path, text):
    f = tmp_path / "x.nfo"
    f.write_text(text, encoding="utf-8")
    assert nfo.parse_nfo(f) is None or nfo.parse_nfo(f).empty


def test_find_nfo_same_name_or_lone_movie_nfo(tmp_path):
    a = tmp_path / "one" / "Beach.mp4"
    a.parent.mkdir()
    a.write_bytes(b"")
    assert nfo.find_nfo(a) is None
    (a.parent / "movie.nfo").write_text(WHISPARR_STYLE, encoding="utf-8")
    assert nfo.find_nfo(a).name == "movie.nfo"                     # the only video in its folder
    (a.parent / "Other.mp4").write_bytes(b"")
    assert nfo.find_nfo(a) is None                                 # movie.nfo could be either video's
    (a.parent / "Beach.nfo").write_text(WHISPARR_STYLE, encoding="utf-8")
    assert nfo.find_nfo(a).name == "Beach.nfo"


def test_reading_nfo_files_fills_blanks_adds_people_and_undoes(tmp_path, media):
    from runemark import names as N
    folder = tmp_path / "v"
    folder.mkdir()
    a, b = folder / "a.mp4", folder / "b.mp4"
    shutil.copy2(media["mp4"], a)
    shutil.copy2(media["mp4"], b)
    save_mp4(b, keywords=[], genre=[], meta=VideoMeta("My own title", None, "", 0))
    for v in (a, b):
        v.with_suffix(".nfo").write_text(WHISPARR_STYLE, encoding="utf-8")
    db = TagDB(tmp_path / "t.db")
    try:
        plans = nfo.plan_nfo([str(a), str(b), str(media["mp4"])])
        assert [p.matched for p in plans] == [True, True, False] and plans[0].source == "a.nfo"
        assert N.apply_read(db, plans, label="Details read from .nfo files") == []
        sa, sb = read_state(a), read_state(b)
        assert (sa.meta.title, sa.meta.year, sa.meta.studio) == ("Beach Day", 2019, "Sunny Films")
        assert sa.meta.description.startswith("A day at the beach.")
        assert sb.meta.title == "My own title" and sb.meta.studio == "Sunny Films"     # blanks only
        assert [p.name for p, _ in P.current_people(db, sa.data)] == ["Ana Lee", "Bo Park"]
        assert db.history(a)[0].label == "Details read from .nfo files"
        L.revert_file(db, db.history(a)[0])
        assert read_state(a).meta.title == "" and read_state(a).meta.studio is None
    finally:
        db.close()


# --- chapter names --------------------------------------------------------------------------------------------

def _chaptered(folder, name, media, chapter_names, studio="", person=None, db=None):
    path = folder / name
    shutil.copy2(media["mp4"], path)
    marks = []
    for i, n in enumerate(chapter_names):
        marks, mark = CH.add_mark(marks, float(i))
        mark.name, mark.auto = n, False
    marks = CH.renumber(marks)
    save_mp4_with_chapters(path, probe(path), marks, keywords=[], genre=[], cover=None,
                           meta=VideoMeta("", None, "", 0, studio), data=None)
    if person is not None:
        P.save_people(db, path, [(person, [])], label="t")
    return path


def test_chapter_names_used_with_these_people_or_studio_come_first(tmp_path, media):
    folder = tmp_path / "v"
    folder.mkdir()
    db = TagDB(tmp_path / "t.db")
    try:
        ana = db.add_person("Ana Lee")
        _chaptered(folder, "1.mp4", media, ["Intro", "Swimming"], person=ana, db=db)
        _chaptered(folder, "2.mp4", media, ["Intro", "Picnic"], studio="Sunny Films")
        _chaptered(folder, "3.mp4", media, ["Credits"])
        _chaptered(folder, "4.mp4", media, ["Credits", "Chapter 2"])   # numbered names aren't suggested
        L.scan(db, [str(folder)])
        assert sorted(r.chapters for r in db.library()) == [1, 2, 2, 2]
        assert db.chapter_names() == ["Credits", "Intro", "Picnic", "Swimming"]
        assert db.chapter_names([ana.uid])[:2] == ["Intro", "Swimming"]
        assert db.chapter_names([], "sunny films")[:2] == ["Intro", "Picnic"]
        assert "Swimming" not in db.chapter_names([ana.uid], exclude=folder / "1.mp4")
    finally:
        db.close()


# --- inbox ----------------------------------------------------------------------------------------------------

def test_inbox_is_new_videos_not_saved_or_marked_done(tmp_path, media):
    from runemark.ui.library_model import still_to_do
    folder = tmp_path / "v"
    folder.mkdir()
    for n in ("a.mp4", "b.mp4", "c.mp4"):
        shutil.copy2(media["mp4"], folder / n)
    db = TagDB(tmp_path / "t.db")
    try:
        L.scan(db, [str(folder)])
        from pathlib import Path
        inbox = lambda: sorted(Path(r.path).name for r in db.library()  # noqa: E731
                               if L.matches(r, L.Search(filter=L.FILTER_INBOX)))
        assert inbox() == ["a.mp4", "b.mp4", "c.mp4"]
        rec = next(r for r in db.library() if r.path.endswith("a.mp4"))
        assert still_to_do(rec) == ["tags", "people", "thumbnail", "chapters", "studio"]
        assert L.apply_bulk(db, [folder / "a.mp4"], L.BulkChange(add=["Lake"])) == []   # saved: out
        db.set_inbox_done([folder / "b.mp4"])                                             # done: out
        assert inbox() == ["c.mp4"]
        db.set_inbox_done([folder / "b.mp4"], False)
        assert inbox() == ["b.mp4", "c.mp4"]
        assert not db.was_saved(folder / "b.mp4") and db.was_saved(folder / "a.mp4")
    finally:
        db.close()


def test_upgrading_rereads_files_for_studios_and_chapters(tmp_path, media):
    import sqlite3
    folder = tmp_path / "v"
    folder.mkdir()
    shutil.copy2(media["mp4"], folder / "a.mp4")
    db = TagDB(tmp_path / "t.db")
    L.scan(db, [str(folder)])
    db.close()
    # pretend the database is from 0.4: no chapters column
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("ALTER TABLE files DROP COLUMN chapters")
    conn.commit()
    conn.close()
    db = TagDB(tmp_path / "t.db")
    try:
        assert db.index_states() == {}                 # everything is read again on the next scan
        assert L.scan(db, [str(folder)]).read == 1 and db.library()[0].chapters == 0
    finally:
        db.close()


# --- the editor -----------------------------------------------------------------------------------------------

def test_chapter_panel_named_chapters_and_keys():
    from PySide6.QtWidgets import QApplication
    from runemark.ui.chapters_panel import ChapterKeysDialog, ChaptersPanel
    QApplication.instance() or QApplication([])
    panel = ChaptersPanel()
    panel.set_tag_names(["Swimming", "Intro"])
    tagged, keys = [], []
    panel.nameTagged.connect(tagged.append)
    panel.keysChanged.connect(keys.append)
    panel.set_enabled(True, "mp4")
    panel.add_at(5.0, "swimming")                       # matches the tag's spelling
    assert [(m.start, m.name, m.auto) for m in panel.marks] == [(0.0, "Chapter 1", True), (5.0, "Swimming", False)]
    assert tagged == ["Swimming"]
    panel.add_at(9.0)
    assert panel.marks[-1].name == "Chapter 3" and panel.marks[-1].auto
    panel._set_key("3", "Swimming")
    panel._set_key("4", "Swimming")                     # one key per name: moves from 3 to 4
    assert keys[-1] == {"4": "Swimming"} and "4 Swimming" in panel.keys_line.text()
    dialog = ChapterKeysDialog({"1": "Intro"}, panel.names)
    dialog.edits["2"].setText("  Picnic ; lunch ")
    assert dialog.keys() == {"1": "Intro", "2": "Picnic , lunch"}


needs_player = pytest.mark.skipif(__import__("sys").platform != "win32",
                                  reason="needs Qt Multimedia playback (Windows build)")


@needs_player
def test_editor_fills_from_nfo_once_and_chapter_keys_add_named_chapters(tmp_path, media, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication
    from runemark.settings import Settings
    from runemark.ui.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "Beach.mp4"
    shutil.copy2(media["mp4"], path)
    path.with_suffix(".nfo").write_text(WHISPARR_STYLE, encoding="utf-8")
    db = TagDB(tmp_path / "t.db")
    settings = Settings(chapter_keys={"2": "Swimming"})
    monkeypatch.setattr(settings, "save", lambda: None)
    window = MainWindow(db, settings)
    try:
        window.open_paths([path])
        end = time.time() + 30
        while (window.doc is None or window.doc.path != path) and time.time() < end:
            app.processEvents()
            time.sleep(0.02)
        doc = window.doc
        assert (doc.meta.title, doc.meta.year, doc.meta.studio) == ("Beach Day", 2019, "Sunny Films")
        assert len(doc.people) == 2 and doc.dirty()                         # filled in, not saved yet
        assert window.details.meta.studio.text() == "Sunny Films"
        window.player.frame.setFocus()
        window.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_2, Qt.KeyboardModifier.NoModifier, "2"))
        assert [m.name for m in doc.chapters if not m.auto] == ["Swimming"]
        assert "Swimming" in doc.tags                                       # the chapter's name tags the file
        window.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_5, Qt.KeyboardModifier.NoModifier, "5"))
        assert len([m for m in doc.chapters if not m.auto]) == 1           # key 5 isn't set up
    finally:
        if window.doc is not None:
            window.doc.discard()                             # no "unsaved changes" prompt on close
        window.close()
        db.close()
