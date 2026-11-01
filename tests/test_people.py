"""v0.4.0: people. Names with aliases, attribute values that become tags, the data block inside each MP4,
updating every video when a profile changes, and rebuilding the people list from the files alone."""
import shutil
import sqlite3

import pytest

from runemark import library as L
from runemark import people as P
from runemark.media import chapters as CH
from runemark.media.mp4meta import VideoMeta, read_mp4_tags
from runemark.media.probe import probe
from runemark.tagdb import DuplicateName, InvalidName, TagDB
from runemark.tagsync import read_state, save_mp4, save_mp4_with_chapters


@pytest.fixture
def db(tmp_path):
    d = TagDB(tmp_path / "tags.db")
    yield d
    d.close()


@pytest.fixture
def ana(db):
    """A person with an alias, two attributes and a value limited to some years."""
    hair = db.add_attribute("Hair", stash_field="hair_color")
    marks = db.add_attribute("Marks")
    p = db.add_person("Ana Lee", ["Ana L.", "Annie"])
    db.add_person_value(p.id, hair.id, "Red", tag="Red hair", to_year=2019)
    db.add_person_value(p.id, hair.id, "Blonde", tag="Blonde hair", from_year=2020)
    db.add_person_value(p.id, marks.id, "Freckles")
    return db.person(p.id)


def files(tmp_path, media, names, year=None):
    folder = tmp_path / "v"
    folder.mkdir(exist_ok=True)
    out = []
    for name in names:
        shutil.copy2(media["mp4"], folder / name)
        if year:
            save_mp4(folder / name, keywords=[], genre=[], meta=VideoMeta("", year, "", 0))
        out.append(folder / name)
    return out


# --- the database ------------------------------------------------------------------------------------------

def test_names_and_aliases_are_unique_across_people(db):
    a = db.add_person("Ana Lee", ["Annie"])
    with pytest.raises(DuplicateName):
        db.add_person("annie")                           # someone's alias
    with pytest.raises(DuplicateName):
        db.add_person("Bea", ["ANA LEE"])                # someone's name
    assert db.people() == [db.person(a.id)]              # the failed add left nothing behind
    assert db.find_person("ANNIE").id == a.id and db.find_person("nobody") is None
    db.update_person(a.id, name="Annie")                 # promoting an alias to the main name
    assert db.person(a.id).name == "Annie" and db.person(a.id).aliases == []
    assert db.person_names() == {"annie": "Annie"}


def test_values_and_year_ranges(db, ana):
    assert [(v.attribute, v.value, v.tag) for v in ana.values] == [
        ("Hair", "Blonde", "Blonde hair"), ("Hair", "Red", "Red hair"), ("Marks", "Freckles", "Freckles")]
    with pytest.raises(InvalidName):
        db.add_person_value(ana.id, ana.values[0].attribute_id, "X", from_year=2020, to_year=2010)
    db.delete_attribute(db.find_attribute("Marks").id)    # removing a field removes its values
    assert [v.value for v in db.person(ana.id).values] == ["Blonde", "Red"]


def test_upgrading_a_v030_database(tmp_path):
    path = tmp_path / "old.db"
    d = TagDB(path)
    d.record_history(tmp_path / "a.mp4", keywords=["Lake"], genre=[])
    d.close()
    con = sqlite3.connect(path)
    for table in ("people", "person_aliases", "person_values", "attributes", "file_people"):
        con.execute(f"DROP TABLE {table}")
    con.execute("ALTER TABLE file_history DROP COLUMN data")
    con.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    con.commit()
    con.close()
    d = TagDB(path)
    try:
        assert d.history(tmp_path / "a.mp4")[0].keywords == ["Lake"] and d.history(tmp_path / "a.mp4")[0].data is None
        assert d.add_person("Ana").name == "Ana"
    finally:
        d.close()


# --- working out tags (no files) ---------------------------------------------------------------------------

def test_tags_from_a_profile(db, ana):
    entry = P.person_entry(db, ana)
    assert P.entry_tags(entry, 2018) == ["Ana Lee", "Red hair", "Freckles"]
    assert P.entry_tags(entry, 2021) == ["Ana Lee", "Blonde hair", "Freckles"]
    assert P.entry_tags(entry, None) == ["Ana Lee", "Freckles"]          # year-limited values need a year
    entry["aliases_as_tags"] = True
    entry["skip"] = [v.uid for v in ana.values if v.value == "Freckles"]  # "not in this video"
    assert P.entry_tags(entry, 2018) == ["Ana Lee", "Ana L.", "Annie", "Red hair"]


def test_hand_tags_survive_profile_changes():
    old = {"format": P.FORMAT, "profile_tags": ["Ana Lee", "Red hair"]}
    new = {"format": P.FORMAT, "profile_tags": ["Ana Lee", "Blonde hair"]}
    assert P.combine(["Lake", "Ana Lee", "Red hair", "Summer"], old, new) == ["Lake", "Summer", "Ana Lee",
                                                                               "Blonde hair"]
    assert P.combine(["Lake"], None, None) == ["Lake"]


# --- files ---------------------------------------------------------------------------------------------------

def test_saving_people_writes_tags_and_the_data_block(tmp_path, media, db, ana):
    (f,) = files(tmp_path, media, ["a.mp4"], year=2018)
    save_mp4(f, keywords=["Lake"], genre=[])
    assert P.save_people(db, f, [(ana, [])], label="test") == []
    state = read_state(f)
    assert state.keywords == ["Lake", "Ana Lee", "Red hair", "Freckles"]
    assert state.sync().in_sync                                        # Explorer's Tags get them too
    assert state.data["format"] == "runemark-data" and state.data["version"] == 1
    (entry,) = state.data["people"]
    assert entry["id"] == ana.uid and entry["aliases"] == ["Ana L.", "Annie"]
    hair = [v for v in entry["values"] if v["attribute"]["name"] == "Hair"]
    assert {v["attribute"]["stash_field"] for v in hair} == {"hair_color"}
    assert db.files_with_person(ana.id) == [str(f)]
    # saving again with nothing changed doesn't touch the file
    before = f.stat().st_mtime_ns
    P.save_people(db, f, [(ana, [])], label="test")
    assert f.stat().st_mtime_ns == before


def test_profile_change_updates_every_video_and_can_be_undone(tmp_path, media, db, ana):
    a, b, other = files(tmp_path, media, ["a.mp4", "b.mp4", "other.mp4"], year=2018)
    save_mp4(other, keywords=["Freckles"], genre=[])                   # typed by hand, no people
    for f in (a, b):
        P.save_people(db, f, [(ana, [])], label="test")
    save_mp4(b, keywords=read_state(b).keywords + ["Beach"], genre=[])  # a hand tag added later elsewhere

    marks = db.find_attribute("Marks")
    db.add_person_value(ana.id, marks.id, "Star tattoo")                # a new attribute, added later
    db.remove_person_value(next(v.id for v in ana.values if v.value == "Freckles"))
    paths = P.files_needing_update(db, ana.id)
    assert sorted(paths) == sorted([str(a), str(b)])
    assert P.refresh_files(db, paths, label="People: Ana Lee changed") == []
    assert read_state(a).keywords == ["Ana Lee", "Red hair", "Star tattoo"]
    assert read_state(b).keywords == ["Beach", "Ana Lee", "Red hair", "Star tattoo"]
    assert read_state(other).keywords == ["Freckles"]                  # not touched

    assert L.undo_batch(db, db.last_batch()) == []
    assert read_state(a).keywords == ["Ana Lee", "Red hair", "Freckles"]
    assert [v["value"] for v in read_state(a).data["people"][0]["values"]].count("Freckles") == 1


def test_removing_a_person_keeps_hand_tags(tmp_path, media, db, ana):
    (f,) = files(tmp_path, media, ["a.mp4"], year=2018)
    save_mp4(f, keywords=["Lake"], genre=[])
    P.save_people(db, f, [(ana, [])], label="test")
    assert P.refresh_files(db, [f], label="remove", drop={ana.uid}) == []
    state = read_state(f)
    assert state.keywords == ["Lake"] and state.data is None
    assert read_mp4_tags(f).data is None


def test_rename_and_aliases_follow_through(tmp_path, media, db, ana):
    (f,) = files(tmp_path, media, ["a.mp4"], year=2021)
    P.save_people(db, f, [(ana, [])], label="test")
    db.update_person(ana.id, name="Ana Marie Lee", aliases=["Ana Lee", "Annie"], write_aliases=True)
    P.refresh_files(db, [f], label="rename")
    assert read_state(f).keywords == ["Ana Marie Lee", "Ana Lee", "Annie", "Blonde hair", "Freckles"]


def test_scan_rebuilds_people_from_the_files(tmp_path, media, db, ana):
    """Portability: a fresh library (another PC, a lost database) gets the same people back from the videos."""
    a, b = files(tmp_path, media, ["a.mp4", "b.mp4"], year=2018)
    for f in (a, b):
        P.save_people(db, f, [(ana, [])], label="test")
    fresh = TagDB(tmp_path / "fresh.db")
    try:
        L.scan(fresh, [str(tmp_path / "v")])
        (back,) = fresh.people()
        assert (back.uid, back.name, back.aliases) == (ana.uid, "Ana Lee", ["Ana L.", "Annie"])
        assert sorted((v.attribute, v.value, v.tag, v.from_year, v.to_year) for v in back.values) == sorted(
            (v.attribute, v.value, v.tag, v.from_year, v.to_year) for v in ana.values)
        assert fresh.find_attribute("Hair").stash_field == "hair_color"
        assert sorted(fresh.files_with_person(back.id)) == sorted([str(a), str(b)])
    finally:
        fresh.close()


def test_scan_matches_an_existing_person_by_name(tmp_path, media, db, ana):
    (f,) = files(tmp_path, media, ["a.mp4"])
    P.save_people(db, f, [(ana, [])], label="test")
    other = TagDB(tmp_path / "other.db")
    try:
        mine = other.add_person("Annie")                   # same person, made separately (different id)
        L.scan(other, [str(tmp_path / "v")])
        assert [p.name for p in other.people()] == ["Annie"] and other.files_with_person(mine.id) == [str(f)]
    finally:
        other.close()


def test_writing_chapters_keeps_the_data_block(copy_of, db, ana):
    path = copy_of("mp4")
    P.save_people(db, path, [(ana, [])], label="test")
    state = read_state(path)
    marks, _ = CH.add_mark([], 0.0)
    marks, _ = CH.add_mark(marks, 2.0)
    outcome = save_mp4_with_chapters(path, probe(path), marks, keywords=state.keywords, genre=state.genre,
                                     cover=state.cover, meta=state.meta, data=state.data)
    assert outcome.ok, outcome.problems
    assert read_state(path).data == state.data


def test_refresh_adopts_this_librarys_profile_for_a_person_matched_by_name(tmp_path, media, db, ana):
    (f,) = files(tmp_path, media, ["a.mp4"])
    P.save_people(db, f, [(ana, [])], label="test")
    other = TagDB(tmp_path / "other.db")
    try:
        mine = other.add_person("Annie", write_aliases=False)
        L.scan(other, [str(tmp_path / "v")])
        assert P.refresh_files(other, other.files_with_person(mine.id), label="refresh") == []
        state = read_state(f)
        assert state.data["people"][0]["id"] == mine.uid and state.keywords == ["Annie"]
    finally:
        other.close()


def test_removing_one_person_keeps_a_tag_another_still_gives(tmp_path, media, db, ana):
    (f,) = files(tmp_path, media, ["a.mp4"], year=2018)
    bea = db.add_person("Bea Cole")
    db.add_person_value(bea.id, db.find_attribute("Hair").id, "Red", tag="Red hair")
    bea = db.person(bea.id)
    P.save_people(db, f, [(ana, []), (bea, [])], label="test")
    assert read_state(f).keywords == ["Ana Lee", "Red hair", "Freckles", "Bea Cole"]
    P.refresh_files(db, [f], label="remove Ana", drop={ana.uid})
    assert read_state(f).keywords == ["Bea Cole", "Red hair"]          # Bea still has red hair
    P.refresh_files(db, [f], label="remove Bea", drop={bea.uid})
    assert read_state(f).keywords == []


def test_a_hand_typed_tag_that_a_profile_also_gives_survives(tmp_path, media, db, ana):
    (f,) = files(tmp_path, media, ["a.mp4"], year=2018)
    save_mp4(f, keywords=["Freckles", "Lake"], genre=[])                 # typed by hand first
    P.save_people(db, f, [(ana, [])], label="test")
    state = read_state(f)
    assert state.keywords == ["Freckles", "Lake", "Ana Lee", "Red hair"] and state.data["also_hand"] == ["Freckles"]
    db.remove_person_value(next(v.id for v in ana.values if v.value == "Freckles"))
    P.refresh_files(db, [f], label="no more freckles")                  # the profile stops giving it ...
    assert read_state(f).keywords == ["Freckles", "Lake", "Ana Lee", "Red hair"]   # ... but it was typed by hand
    P.refresh_files(db, [f], label="remove Ana", drop={ana.uid})
    assert read_state(f).keywords == ["Freckles", "Lake"]


def test_unknown_keys_from_other_tools_are_kept(tmp_path, media, db, ana):
    (f,) = files(tmp_path, media, ["a.mp4"], year=2018)
    P.save_people(db, f, [(ana, [])], label="test")
    state = read_state(f)
    data = dict(state.data, stash={"scene_id": "42"})
    data["people"] = [dict(state.data["people"][0], extra="x")]
    save_mp4(f, keywords=state.keywords, genre=[], data=data)
    db.add_person_value(ana.id, db.find_attribute("Marks").id, "Star tattoo")
    P.refresh_files(db, [f], label="refresh")
    after = read_state(f).data
    assert after["stash"] == {"scene_id": "42"} and "Star tattoo" in after["profile_tags"]


def test_the_documented_example_reads_as_documented():
    """docs/data-format.md's example block gives the tags the page says it does."""
    import json
    import re
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "docs" / "data-format.md").read_text(encoding="utf-8")
    example = json.loads(re.search(r"```json\n(.*?)```", text, re.S).group(1))
    (entry,) = P.entries_of(example)
    assert P.entry_tags(entry, 2018) == example["profile_tags"]
    assert P.entry_tags(entry, 2021) == ["Ana Lee"]                 # Red hair only until 2019
    assert P.hand_tags(["Lake", "Ana Lee", "Red hair"], example) == ["Lake"]


def test_nfo_lists_people_as_actors(tmp_path, media, db, ana):
    (f,) = files(tmp_path, media, ["a.mp4"], year=2018)
    P.save_people(db, f, [(ana, [])], label="test")
    L.reindex(db, f)
    (record,) = db.library()
    text = L.nfo_text(record)
    assert "<actor>\n    <name>Ana Lee</name>\n  </actor>" in text and "<tag>Red hair</tag>" in text
