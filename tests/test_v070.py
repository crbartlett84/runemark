"""0.7.0: links and full release dates in files, people's links, named libraries, and plugins."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from runemark import library as L
from runemark import nfo
from runemark import people as P
from runemark import plugins
from runemark.media.mp4meta import VideoMeta, clean_links, parse_date, read_mp4_tags
from runemark.settings import Settings
from runemark.tagdb import TagDB
from runemark.tagsync import read_state, save_mp4

SCENE = "https://stashdb.org/scenes/bffb88f3-726d-4ef8-ab81-703281c19c3f"


# ------------------------------------------------------------------ links and dates
def test_links_and_full_date_round_trip(copy_of):
    path = copy_of("mp4")
    links = ["https://example.com/v/1", SCENE, "https://example.com/v/1", "not a link"]
    outcome = save_mp4(path, keywords=["A"], genre=[], meta=VideoMeta("T", 2022, "", 0, None, links, "2022-12-16"))
    assert outcome.ok and not outcome.problems
    tags = read_mp4_tags(path)
    assert tags.links == ["https://example.com/v/1", SCENE] and tags.date == "2022-12-16" and tags.year == 2022
    meta = read_state(path).meta
    assert meta.links == tags.links and meta.full_date() == "2022-12-16"
    # links=None leaves them alone; [] removes them
    save_mp4(path, keywords=["A"], genre=[], meta=VideoMeta("T", 2022, "", 0, None, None, "2022-12-16"))
    assert read_mp4_tags(path).links == tags.links
    save_mp4(path, keywords=["A"], genre=[], meta=VideoMeta("T", 2022, "", 0, None, [], None))
    after = read_mp4_tags(path)
    assert after.links is None and after.date == "2022-12-16"     # same year: the full date is kept


def test_a_date_in_another_year_gives_way_to_the_year():
    meta = VideoMeta("T", 2020, date="2022-12-16")
    assert meta.full_date() is None
    assert parse_date("2019-07-14T10:00:00Z") == "2019-07-14" and parse_date("2019") is None
    assert clean_links([" https://a.b/c ", "ftp://x", "https://A.b/c"]) == ["https://a.b/c"]


def test_nfo_links_ids_and_date(tmp_path):
    video = tmp_path / "Scene.mp4"
    video.write_bytes(b"")
    (tmp_path / "Scene.nfo").write_text(
        "<movie><title>T</title><premiered>2022-12-16</premiered>"
        "<uniqueid type=\"stash\">bffb88f3-726d-4ef8-ab81-703281c19c3f</uniqueid>"
        "<uniqueid type=\"url\">https://example.com/v/1</uniqueid></movie>", encoding="utf-8")
    d = nfo.read_for(video)
    assert d.date == "2022-12-16" and d.year == 2022
    assert d.ids == {"stash": "bffb88f3-726d-4ef8-ab81-703281c19c3f"}
    assert d.links == [SCENE, "https://example.com/v/1"]


def test_person_links_travel_in_the_file(tmp_path, copy_of):
    path = copy_of("mp4")
    db = TagDB(tmp_path / "a.db")
    ana = db.add_person("Ana Lee")
    db.set_person_links(ana.id, ["https://stashdb.org/performers/779ce296-dcfc-4e83-a5fe-a309f592be2a"])
    P.save_people(db, path, [(db.person(ana.id), [])], label="t")
    db.close()
    fresh = TagDB(tmp_path / "b.db")                 # another PC: the profile comes back from the file
    P.resolve_people(fresh, read_state(path).data)
    assert fresh.find_person("Ana Lee").links == ["https://stashdb.org/performers/779ce296-dcfc-4e83-a5fe-a309f592be2a"]
    fresh.close()


# ------------------------------------------------------------------ libraries
def test_one_folder_list_becomes_a_library_and_skips_work(tmp_path):
    s = Settings(library_folders=[str(tmp_path / "Lib")])
    s.sync_libraries()
    assert s.libraries == [{"name": "Library", "folders": [str(tmp_path / "Lib")], "skip": []}]
    lib, gen = tmp_path / "Lib", tmp_path / "Lib" / "Gen"
    s.libraries = [{"name": "Mine", "folders": [str(lib)], "skip": [str(gen)]},
                   {"name": "Hers", "folders": [str(gen)], "skip": []}]
    s.sync_libraries()
    assert s.library_folders == [str(lib), str(gen)]
    assert L.in_library(str(lib / "a.mp4"), s.libraries[0]) and not L.in_library(str(gen / "b.mp4"), s.libraries[0])
    assert L.libraries_of(s, str(gen / "b.mp4")) == ["Hers"]
    assert L.scan_skips(s) == []            # another library includes Gen, so a scan still goes in
    s.libraries.pop()
    assert L.scan_skips(s) == [str(gen)]
    (gen).mkdir(parents=True)
    (lib / "a.mp4").write_bytes(b"x")
    (gen / "b.mp4").write_bytes(b"x")
    assert [p.name for p in L.find_videos([str(lib)], skip=L.scan_skips(s))] == ["a.mp4"]


# ------------------------------------------------------------------ plugins
class FakeWindow:
    """What load_all and the API need from the main window."""

    def __init__(self, db, settings):
        self.db, self.settings, self.doc = db, settings, None
        self.panes, self.actions, self.statuses = [], [], []

        class _Editor:
            def refresh_database(self):
                pass
        self.tag_editor = _Editor()

    def add_plugin_pane(self, title, widget):
        self.panes.append(title)

    def add_plugin_action(self, text, callback, shortcut=None):
        self.actions.append((text, callback))

    def set_status(self, text):
        self.statuses.append(text)


def _plugin(folder: Path, key: str, code: str, api: int = 1) -> Path:
    d = folder / key
    d.mkdir(parents=True)
    (d / "plugin.json").write_text(json.dumps({"name": key.title(), "version": "1.0", "api": api}), encoding="utf-8")
    (d / "plugin.py").write_text(code, encoding="utf-8")
    return d


def test_plugins_load_register_and_fail_safely(tmp_path):
    folder = tmp_path / "plugins"
    _plugin(folder, "good", "def register(api):\n"
                            "    api.add_action('Hello', lambda: api.status('hi'))\n"
                            "    api.add_library_action('Count', lambda paths: api.status(str(len(paths))))\n"
                            "    api.on('file_saved', lambda p: api.status('saved ' + p.name))\n"
                            "    api.settings['n'] = 1\n")
    _plugin(folder, "broken", "raise RuntimeError('boom')\n")
    _plugin(folder, "future", "def register(api): pass\n", api=99)
    _plugin(folder, "off", "def register(api): raise SystemExit\n")
    db = TagDB(tmp_path / "t.db")
    win = FakeWindow(db, Settings())
    host = plugins.load_all(win, disabled=["off"], folder=folder)
    by = {p.key: p for p in host.plugins}
    assert by["good"].loaded and not by["good"].error
    assert "boom" in by["broken"].error and not by["broken"].loaded
    assert "newer Runemark" in by["future"].error and not by["off"].loaded
    assert [a[0] for a in win.actions] == ["Hello"] and host.library_actions[0].text == "Count"
    win.actions[0][1]()
    host.library_actions[0].callback([Path("a"), Path("b")])
    host.emit("file_saved", Path("x.mp4"))
    assert win.statuses == ["hi", "2", "saved x.mp4"]
    assert win.settings.plugin_settings == {"good": {"n": 1}}
    db.close()


def test_plugin_api_tags_and_people(tmp_path):
    db = TagDB(tmp_path / "t.db")
    places = db.add_category("Places")
    db.add_tag("Beach", places.id)
    api = plugins.PluginAPI(FakeWindow(db, Settings()), plugins.PluginHost(), plugins.Plugin(tmp_path, "p"))
    assert api.find_tag("beach") == "Beach" and api.find_tag("Nope") is None
    assert api.add_tag("Lake", ["Places"]) == "Lake" and {"name": "Lake", "category": ["Places"]} in api.tags()
    name = api.add_person("Ana Lee", aliases=["Annie"], links=["https://stashdb.org/performers/x"],
                          values={"Hair": "Red"})
    assert name == "Ana Lee"
    again = api.add_person("Annie", links=["https://example.com/ana"])      # found by alias: filled in, not doubled
    assert again == "Ana Lee" and len(db.people()) == 1
    info = api.find_person("Ana Lee")
    assert info["aliases"] == ["Annie"] and info["links"] == ["https://stashdb.org/performers/x",
                                                               "https://example.com/ana"]
    assert [v.value for v in db.find_person("Ana Lee").values] == ["Red"]
    db.close()


def test_move_with_sidecars(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    (src / "Scene.mp4").write_bytes(b"v")
    (src / "Scene.nfo").write_text("n")
    (src / "Scene.en.srt").write_text("s")
    (src / "Other.mp4").write_bytes(b"o")
    new, moved = plugins.move_with_sidecars(src / "Scene.mp4", tmp_path / "out" / "Studio", "Renamed")
    assert new == tmp_path / "out" / "Studio" / "Renamed.mp4" and len(moved) == 3
    assert sorted(p.name for p in new.parent.iterdir()) == ["Renamed.en.srt", "Renamed.mp4", "Renamed.nfo"]
    assert [p.name for p in src.iterdir()] == ["Other.mp4"]
    (src / "Again.mp4").write_bytes(b"v")
    (new.parent / "Again.mp4").write_bytes(b"x")
    with pytest.raises(FileExistsError):
        plugins.move_with_sidecars(src / "Again.mp4", new.parent)


def test_plugin_api_attributes_with_tag_wording(tmp_path):
    db = TagDB(tmp_path / "t.db")
    api = plugins.PluginAPI(FakeWindow(db, Settings()), plugins.PluginHost(), plugins.Plugin(tmp_path, "p"))
    assert api.add_attribute("Hair", stash_field="hair_color") == "Hair"
    assert api.add_attribute("Born", stash_field="birthdate", as_tag=False) == "Born"
    assert api.add_attribute("hair") == "Hair"                         # exists: nothing new
    api.add_person("Ana Lee", values={"Hair": ("Black", "Black hair"), "Born": "1992-02-18"})
    person = db.find_person("Ana Lee")
    assert sorted((v.attribute, v.value, v.tag) for v in person.values) == [
        ("Born", "1992-02-18", "1992-02-18"), ("Hair", "Black", "Black hair")]
    assert {a["name"]: (a["stash_field"], a["as_tag"]) for a in api.attributes()} == {
        "Born": ("birthdate", False), "Hair": ("hair_color", True)}
    db.close()


def test_plugin_api_categories_moves_and_rules(tmp_path):
    db = TagDB(tmp_path / "t.db")
    api = plugins.PluginAPI(FakeWindow(db, Settings()), plugins.PluginHost(), plugins.Plugin(tmp_path, "p"))
    assert api.add_category(["Scenes", "Genre"], genre=True) == ["Scenes", "Genre"]
    assert api.add_category(["scenes", "genre"]) == ["Scenes", "Genre"]          # found, not doubled
    genre = next(c for c in db.categories() if c.name == "Genre")
    assert genre.write_genre and len(db.categories()) == 2
    db.add_tag("Blowjob Scene", None)
    assert api.set_tag_category("blowjob scene", ["Scenes", "Genre"]) == "Blowjob Scene"
    assert db.is_genre_tag("Blowjob Scene")
    assert api.set_tag_category("Nope", ["X"]) is None
    db.add_tag("Werewolf", None)
    db.add_tag("Creature", None)
    api.set_tag_rule("Werewolf", ["Creature"])
    assert api.tag_rules() == {"Werewolf": ["Creature"]} and db.with_implied(["Werewolf"]) == ["Werewolf", "Creature"]
    with pytest.raises(ValueError):
        api.set_tag_rule("Werewolf", ["Missing"])
    api.set_tag_rule("Werewolf", [])
    assert api.tag_rules() == {}
    db.close()


def test_plugin_api_clear_tags_keeps_the_named_ones(tmp_path, monkeypatch):
    monkeypatch.setattr("runemark.paths.backup_dir", lambda: tmp_path / "backups")
    db = TagDB(tmp_path / "t.db")
    api = plugins.PluginAPI(FakeWindow(db, Settings()), plugins.PluginHost(), plugins.Plugin(tmp_path, "p"))
    api.add_category(["Acts", "Positions"])
    api.add_category(["Junk", "Deeper"])
    api.add_tag("Missionary", ["Acts", "Positions"])
    api.add_tag("Werewolf", ["Junk", "Deeper"])
    api.add_tag("Creature", ["Junk"])
    api.add_tag("random junk")
    api.set_tag_rule("Werewolf", ["Creature"])
    out = api.clear_tags(["missionary", "Creature"])
    assert out["tags_removed"] == 2 and out["categories_removed"] == 1      # "Deeper" emptied; "Junk" keeps Creature
    assert Path(out["backup"]).exists() and Path(out["backup"]).name.startswith("before-clear-")
    assert sorted(t["name"] for t in api.tags()) == ["Creature", "Missionary"]
    assert api.tag_rules() == {} and sorted(api.categories()) == [["Acts"], ["Acts", "Positions"], ["Junk"]]
    db.close()
