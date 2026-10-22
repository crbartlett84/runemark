"""Chapters, title/year/description/rating, thumbnail suggestions, the library and backups."""
import sqlite3

import pytest
from PIL import Image

from runemark import datastore, library as L, paths
from runemark.media import chapters as CH
from runemark.media import convert as C
from runemark.media import mp4meta, suggest, xtra
from runemark.media.probe import probe
from runemark.settings import Settings
from runemark.tagdb import TagDB, TagDBError
from runemark.tagsync import read_state, save_mp4, save_mp4_with_chapters


# ------------------------------------------------------------------ details and rating
def test_rating_lives_in_xtra_beside_the_tags(copy_of):
    path = copy_of("mp4")
    save_mp4(path, keywords=["A", "B"], genre=[], meta=mp4meta.VideoMeta("T", 1999, "D", 5))
    assert xtra.read_category(path) == ["A", "B"]
    assert xtra.read_rating(path) == 99
    save_mp4(path, keywords=["A"], genre=[])                  # tags only: rating untouched
    assert xtra.read_rating(path) == 99 and xtra.read_category(path) == ["A"]
    save_mp4(path, keywords=["A"], genre=[], meta=mp4meta.VideoMeta())
    state = read_state(path)
    assert xtra.read_rating(path) == 0 and state.meta.key() == ("", None, "", 0)


def test_star_values_match_explorer():
    assert [mp4meta.stars_to_value(n) for n in range(6)] == [0, 1, 25, 50, 75, 99]
    assert [mp4meta.value_to_stars(v) for v in (0, 1, 12, 13, 25, 50, 62, 75, 99)] == [0, 1, 1, 2, 2, 3, 3, 4, 5]
    assert mp4meta.parse_year("2019-05-01T00:00:00Z") == 2019 and mp4meta.parse_year("") is None


def test_details_survive_on_a_faststart_file(copy_of):
    path = copy_of("mp4_faststart")
    long_text = "A long description. " * 200
    outcome = save_mp4(path, keywords=["X"], genre=[], meta=mp4meta.VideoMeta("Title", 2001, long_text, 2))
    assert outcome.ok
    assert read_state(path).meta == mp4meta.VideoMeta("Title", 2001, long_text.strip(), 2)


# ------------------------------------------------------------------ chapters
def test_marks_number_themselves_and_start_at_zero():
    marks, first = CH.add_mark([], 12.0)
    assert [(m.start, m.name) for m in marks] == [(0.0, "Chapter 1"), (12.0, "Chapter 2")]
    marks, middle = CH.add_mark(marks, 5.0)
    marks[2].name, marks[2].auto = "Halifax", False
    marks = CH.renumber(marks)
    assert [m.name for m in marks] == ["Chapter 1", "Chapter 2", "Halifax"]
    marks, again = CH.add_mark(marks, 5.0)            # same frame: no duplicate
    assert again is middle and len(marks) == 3
    ends = CH.with_ends(marks, 20.0)
    assert [(c.start, c.end) for c in ends] == [(0.0, 5.0), (5.0, 12.0), (12.0, 20.0)]


def test_write_chapters_keeps_everything_else(copy_of, media):
    path = copy_of("mp4_cover")
    cover = media["cover_jpg"].read_bytes()
    save_mp4(path, keywords=["Keep", "Me"], genre=["Keep"], cover=cover,
             meta=mp4meta.VideoMeta("A Title", 2020, "Words", 4))
    info = probe(path)
    marks, _ = CH.add_mark([], 0.0)
    marks, part = CH.add_mark(marks, 2.0)
    part.name, part.auto = "Part = Two, really", False
    state = read_state(path)
    outcome = save_mp4_with_chapters(path, info, marks, keywords=state.keywords,
                                     genre=state.genre, cover=state.cover, meta=state.meta)
    assert outcome.ok, outcome.problems
    after, new_state = probe(path), read_state(path)
    assert [c.title for c in after.chapters] == ["Chapter 1", "Part = Two, really"]
    assert abs(after.chapters[1].start - 2.0) < 0.05
    assert len(after.audio_streams) == 1 and len(after.video_streams) == 1
    assert new_state.cover == cover and new_state.meta.key() == ("A Title", 2020, "Words", 4)
    assert new_state.sync().in_sync and new_state.genre == ["Keep"]
    assert not CH.temp_path(path).exists()
    # reading them back gives the same marks (named ones stay named, numbered ones stay automatic)
    back = CH.marks_from_info(after)
    assert [(round(m.start, 2), m.name, m.auto) for m in back] == [(0.0, "Chapter 1", True),
                                                                  (2.0, "Part = Two, really", False)]


def test_chapter_rewrite_cancel_leaves_file_alone(copy_of):
    import threading
    path = copy_of("mp4")
    before = path.read_bytes()
    cancel = threading.Event()
    cancel.set()
    marks, _ = CH.add_mark([], 1.0)
    with pytest.raises(CH.Cancelled):
        CH.rewrite_with_chapters(probe(path), marks, cancel=cancel)
    assert path.read_bytes() == before and not CH.temp_path(path).exists()


def test_conversion_uses_edited_chapters(copy_of):
    source = copy_of("mkv")
    plan = C.plan_conversion(probe(source))
    marks, _ = CH.add_mark([], 0.0)
    marks, named = CH.add_mark(marks, 1.5)
    named.name, named.auto = "Birthday", False
    C.use_marks(plan, marks)
    tmp, sidecars = C.run_conversion(plan)
    try:
        assert [c.title for c in probe(tmp).chapters] == ["Chapter 1", "Birthday"]
    finally:
        C.cleanup([tmp, *sidecars])


# ------------------------------------------------------------------ suggestions
def _candidate(pts, brightness, sharp, tone):
    image = Image.new("RGB", (32, 18), (tone, tone, tone))
    return suggest._Candidate(pts, image, sharp, 40.0, 50.0, brightness, [tone] * (24 * 14))


def test_suggestions_skip_dark_frames_and_near_duplicates():
    pool = [_candidate(1.0, 5, 999, 10),        # black: skipped
            _candidate(2.0, 120, 50, 100), _candidate(2.1, 120, 49, 101),   # look the same
            _candidate(9.0, 130, 40, 200)]
    picked = suggest.pick(pool, 2, min_gap=1.0)
    assert [p.pts for p in picked] == [2.0, 9.0]


def test_suggestions_on_real_video(media):
    found = suggest.suggest(probe(media["mp4"]), count=4, samples=12)
    assert 2 <= len(found) <= 4
    assert all(0 < s.pts < 6 for s in found) and found == sorted(found, key=lambda s: s.pts)


# ------------------------------------------------------------------ library
def test_library_scan_search_bulk_and_nfo(tmp_path, media):
    import shutil
    folder = tmp_path / "videos"
    (folder / "sub").mkdir(parents=True)
    shutil.copy2(media["mp4"], folder / "a.mp4")
    shutil.copy2(media["mp4_faststart"], folder / "sub" / "b.mp4")
    shutil.copy2(media["mkv"], folder / "c.mkv")
    (folder / "x.runemark-tmp.mp4").write_bytes(b"ignored")
    db = TagDB(tmp_path / "tags.db")
    db.add_tag("Halifax", db.add_category("Places").id)
    genre = db.add_category("Genre", write_genre=True)
    db.add_tag("Comedy", genre.id)

    result = L.scan(db, [str(folder)])
    assert result.found == 3 and result.read == 3
    assert L.scan(db, [str(folder)]).read == 0                   # nothing changed: nothing re-read

    problems = L.apply_bulk(db, [folder / "a.mp4", folder / "sub" / "b.mp4", folder / "c.mkv"],
                            L.BulkChange(add=["halifax", "Comedy"], rating=4))
    assert [p.name for p, _ in problems] == ["c.mkv"]
    records = {r.path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]: r for r in db.library()}
    assert records["a.mp4"].tags == ["Comedy", "Halifax"] and records["a.mp4"].rating == 4
    assert records["a.mp4"].genre == ["Comedy"]
    assert read_state(folder / "sub" / "b.mp4").meta.rating == 4

    L.apply_bulk(db, [folder / "a.mp4"], L.BulkChange(remove=["Halifax"]))
    found = [r for r in db.library() if L.matches(r, L.Search(tags=["HALIFAX"]))]
    assert [r.path.endswith("b.mp4") for r in found] == [True]
    assert [r.container for r in db.library() if L.matches(r, L.Search(filter=L.FILTER_MKV))] == ["mkv"]
    assert len([r for r in db.library() if L.matches(r, L.Search(text="SUB"))]) == 0
    assert len([r for r in db.library() if L.matches(r, L.Search(text="b.mp"))]) == 1

    (folder / "a.mp4").rename(folder / "renamed.mp4")
    L.scan(db, [str(folder)])
    missing = [r for r in db.library() if L.matches(r, L.Search(filter=L.FILTER_MISSING))]
    assert [r.path.endswith("a.mp4") for r in missing] == [True]

    record = next(r for r in db.library() if r.path.endswith("b.mp4"))
    nfo = L.write_nfo(record, "Trips & Travel")
    text = nfo.read_text(encoding="utf-8")
    assert "<genre>Comedy</genre>" in text and "<tag>Halifax</tag>" in text and "<tag>Comedy</tag>" not in text
    assert "<name>Trips &amp; Travel</name>" in text and "<userrating>8</userrating>" in text
    assert L.write_nfo(record) is None                         # existing .nfo kept unless asked

    assert db.forget_folder(folder / "sub") == 1


# ------------------------------------------------------------------ database: upgrade, backup, restore
def test_old_database_is_upgraded(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE categories(id INTEGER PRIMARY KEY, name TEXT NOT NULL, parent_id INTEGER, write_genre INTEGER);
        CREATE TABLE tags(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, category_id INTEGER,
                          created_at TEXT);
        CREATE TABLE files(id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, path TEXT NOT NULL, size INTEGER,
                           mtime REAL, saved_at TEXT);
        CREATE TABLE file_tags(file_id INTEGER NOT NULL, tag_name TEXT NOT NULL COLLATE NOCASE,
                               PRIMARY KEY(file_id, tag_name));
        INSERT INTO meta VALUES('schema_version', '1');
        INSERT INTO tags(name) VALUES('Kept');
        INSERT INTO files(key, path) VALUES('/v/a.mp4', '/v/a.mp4');
        INSERT INTO file_tags VALUES(1, 'Kept');
    """)
    conn.commit()
    conn.close()
    db = TagDB(path)
    assert [t.name for t in db.tags()] == ["Kept"]
    assert db.library()[0].tags == ["Kept"] and db.library()[0].missing is False


def test_backup_restore_and_check(tmp_path):
    db = TagDB(tmp_path / "tags.db")
    db.add_tag("Before")
    copy = db.backup_to(tmp_path / "elsewhere" / "copy.db")
    assert TagDB.check_backup(copy) == {"tags": 1, "categories": 0, "files": 0}
    db.add_tag("After")
    db.restore_from(copy)
    assert [t.name for t in db.tags()] == ["Before"]
    (tmp_path / "junk.db").write_bytes(b"not a database at all" * 10)
    with pytest.raises(TagDBError):
        TagDB.check_backup(tmp_path / "junk.db")


def test_daily_backup_goes_to_chosen_folder(tmp_path):
    db = TagDB(tmp_path / "tags.db")
    settings = Settings(backup_folder=str(tmp_path / "OneDrive" / "Runemark"), backup_keep=2)
    first = datastore.daily_backup(db, settings)
    assert first is not None and first.parent == tmp_path / "OneDrive" / "Runemark"
    assert datastore.daily_backup(db, settings) is None          # once a day


def test_portable_mode_round_trip(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.delenv("RUNEMARK_HOME", raising=False)
    monkeypatch.setattr(paths, "app_folder", lambda: app)
    monkeypatch.setattr(datastore, "app_folder", lambda: app)
    standard = tmp_path / "appdata" / "Runemark"
    monkeypatch.setattr(paths, "standard_data_dir", lambda: standard)
    monkeypatch.setattr(datastore, "standard_data_dir", lambda: standard)
    monkeypatch.setattr(datastore, "portable_data_dir", lambda: app / paths.PORTABLE_FOLDER)
    db = TagDB(paths.app_data_dir() / "tags.db")
    db.add_tag("Travels")
    assert paths.app_data_dir() == standard
    target = datastore.make_portable(db)
    assert paths.is_portable() and paths.app_data_dir() == target == app / "Runemark data"
    assert [t.name for t in TagDB(target / "tags.db").tags()] == ["Travels"]
    back = datastore.make_standard(TagDB(target / "tags.db"))
    assert not paths.is_portable() and back == standard
