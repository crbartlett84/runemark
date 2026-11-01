"""The Stash import plugin (integrations/stash/runemark): its own MP4 reader agrees with Runemark, and the import
fills Stash in without removing anything. Stash itself is replaced by a small fake that records the calls."""
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

from runemark import people as P
from runemark.media import chapters as CH
from runemark.media.mp4meta import VideoMeta
from runemark.media.probe import probe
from runemark.tagdb import TagDB
from runemark.tagsync import read_state, save_mp4, save_mp4_with_chapters

PLUGIN = Path(__file__).resolve().parents[1] / "integrations" / "stash" / "runemark"


def load(name):
    spec = importlib.util.spec_from_file_location(name, PLUGIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


R = load("runemark_read")
I = load("runemark_import")


@pytest.fixture
def tagged(tmp_path, media):
    """A video tagged by Runemark: hand tags, details, rating, cover, a person with a mapped and an unmapped
    attribute, and two chapters."""
    path = tmp_path / "beach.mp4"
    shutil.copy2(media["mp4_cover"], path)
    db = TagDB(tmp_path / "t.db")
    hair = db.add_attribute("Hair", stash_field="hair_color")
    marks = db.add_attribute("Marks")
    ana = db.add_person("Ana Lee", ["Annie"])
    db.add_person_value(ana.id, hair.id, "Red", tag="Red hair")
    db.add_person_value(ana.id, marks.id, "Freckles")
    save_mp4(path, keywords=["Lake", "Summer"], genre=[], cover=media["cover_jpg"].read_bytes(),
             meta=VideoMeta("Beach Day", 2019, "A day at the beach.", 4))
    P.save_people(db, path, [(db.person(ana.id), [])], label="t")
    state = read_state(path)
    marks_, _ = CH.add_mark([], 0.0)
    marks_, second = CH.add_mark(marks_, 2.0)
    second.name, second.auto = "Swimming", False
    save_mp4_with_chapters(path, probe(path), marks_, keywords=state.keywords, genre=state.genre, cover=state.cover,
                           meta=state.meta, data=state.data)
    db.close()
    return path


def test_reader_agrees_with_runemark(tagged, media):
    info = R.read(str(tagged))
    assert info["title"] == "Beach Day" and info["date"].startswith("2019") and info["rating"] == 4
    assert info["description"] == "A day at the beach."
    assert info["keywords"] == ["Lake", "Summer", "Ana Lee", "Red hair", "Freckles"]
    assert info["cover"][0] == media["cover_jpg"].read_bytes() and info["cover"][1] == "image/jpeg"
    hand, profile = R.hand_and_profile_tags(info)
    assert hand == ["Lake", "Summer"] and profile == ["Ana Lee", "Red hair", "Freckles"]
    (person,) = R.people(info)
    assert person["name"] == "Ana Lee" and person["aliases"] == ["Annie"]
    chapters = R.chapters(str(tagged))
    assert [c["title"] for c in chapters] == ["Chapter 1", "Swimming"] and abs(chapters[1]["start"] - 2.0) < 0.05
    assert R.date_of("2019") == "" and R.date_of("2019", True) == "2019-01-01"
    assert R.date_of("2019-07-14T10:00:00Z") == "2019-07-14"
    assert R.read(str(tagged.with_suffix(".mkv"))) is None


def test_reader_on_an_untouched_file(media):
    info = R.read(str(media["mp4"]))
    assert info["keywords"] == [] and info["data"] is None and info["rating"] == 0


class FakeStash:
    """Just enough of Stash's GraphQL API to run the plugin: tags, performers, one scene, markers."""

    def __init__(self, scene_path, scene=None):
        self.tags, self.performers, self.markers, self.updates = {}, {}, [], []
        self.scene = {"id": "1", "title": "", "details": "", "date": None, "rating100": None,
                      "files": [{"path": str(scene_path)}], "tags": [], "performers": [], "scene_markers": [],
                      **(scene or {})}

    def q(self, query, variables=None):
        v = variables or {}
        if "configuration" in query:
            return {"configuration": {"plugins": {}}}
        if "findTags" in query:
            hits = [{"id": i, "name": n} for i, n in self.tags.items()
                    if "name:" in query and n.casefold() == v["n"].casefold()]
            return {"findTags": {"tags": hits}}
        if "tagCreate" in query:
            new = str(100 + len(self.tags))
            self.tags[new] = v["n"]
            return {"tagCreate": {"id": new}}
        if "findPerformers" in query:
            field = "aliases" if "aliases:" in query else "name"
            hits = [p for p in self.performers.values()
                    if (p["name"].casefold() == v["n"].casefold() if field == "name"
                        else v["n"].casefold() in [a.casefold() for a in p.get("alias_list", [])])]
            return {"findPerformers": {"performers": hits}}
        if "performerCreate" in query:
            new = str(200 + len(self.performers))
            self.performers[new] = {"id": new, **v["i"]}
            return {"performerCreate": {"id": new}}
        if "performerUpdate" in query:
            p = self.performers[v["i"]["id"]]
            fields = dict(v["i"])
            if "custom_fields" in fields:
                p.setdefault("custom_fields", {}).update(fields.pop("custom_fields")["partial"])
            p.update(fields)
            return {"performerUpdate": {"id": p["id"]}}
        if "sceneUpdate" in query:
            self.updates.append(v["i"])
            for k, val in v["i"].items():
                if k == "tag_ids":
                    self.scene["tags"] = [{"id": t, "name": self.tags.get(t, t)} for t in val]
                elif k == "performer_ids":
                    self.scene["performers"] = [{"id": p, "name": self.performers[p]["name"]} for p in val]
                elif k != "id":
                    self.scene[k] = val
            return {"sceneUpdate": {"id": "1"}}
        if "sceneMarkerCreate" in query:
            self.markers.append(v["i"])
            self.scene["scene_markers"].append({"id": str(len(self.markers)), "seconds": v["i"]["seconds"],
                                                "title": v["i"]["title"]})
            return {"sceneMarkerCreate": {"id": "m"}}
        raise AssertionError(f"unexpected query: {query[:80]}")


def test_import_fills_stash_and_is_safe_to_run_again(tagged):
    stash = FakeStash(tagged)
    stash._tags = {}
    stash.tag_id = I.Stash.tag_id.__get__(stash)
    settings = dict(I.DEFAULTS)
    summary = I.import_scene(stash, stash.scene, settings)
    s = stash.scene
    assert (s["title"], s["details"], s["rating100"]) == ("Beach Day", "A day at the beach.", 80)
    assert s["date"] is None                                       # year only: left blank by default
    assert sorted(t["name"] for t in s["tags"]) == ["Freckles", "Lake", "Red hair", "Summer"]   # no person names
    (perf,) = stash.performers.values()
    assert perf["name"] == "Ana Lee" and perf["alias_list"] == ["Annie"] and perf["hair_color"] == "Red"
    assert perf["custom_fields"] == {"Marks": "Freckles"}           # unmapped attribute: a custom field
    assert [m["title"] for m in stash.markers] == ["Chapter 1", "Swimming"]
    assert all(stash.tags[m["primary_tag_id"]] == "Chapter" for m in stash.markers)
    assert s["cover_image"].startswith("data:image/jpeg;base64,") and "marker" in summary

    # again: nothing new
    assert I.import_scene(stash, stash.scene, settings) == ""
    assert len(stash.markers) == 2 and len(stash.performers) == 1


def test_import_fills_blanks_only_and_matches_performers_by_alias(tagged):
    stash = FakeStash(tagged, {"title": "Scraped title", "rating100": 60})
    stash.performers["300"] = {"id": "300", "name": "Annie", "alias_list": [], "hair_color": "Black"}
    stash._tags = {}
    stash.tag_id = I.Stash.tag_id.__get__(stash)
    I.import_scene(stash, stash.scene, dict(I.DEFAULTS))
    assert stash.scene["title"] == "Scraped title" and stash.scene["rating100"] == 60      # kept
    assert [p["id"] for p in stash.scene["performers"]] == ["300"] and len(stash.performers) == 1
    perf = stash.performers["300"]
    assert perf["hair_color"] == "Black"                            # Stash's value kept (fill blanks)
    assert perf["custom_fields"] == {"Marks": "Freckles"}
    assert "Ana Lee" not in perf.get("alias_list", [])              # Runemark's main name isn't forced in

    stash2 = FakeStash(tagged, {"title": "Scraped title"})
    stash2._tags = {}
    stash2.tag_id = I.Stash.tag_id.__get__(stash2)
    I.import_scene(stash2, stash2.scene, {**I.DEFAULTS, "overwrite": True, "yearOnlyDates": True})
    assert stash2.scene["title"] == "Beach Day" and stash2.scene["date"] == "2019-01-01"


def test_plugin_manifest_is_valid_yaml():
    yaml = pytest.importorskip("yaml")
    manifest = yaml.safe_load((PLUGIN / "runemark.yml").read_text(encoding="utf-8"))
    assert manifest["exec"][1] == "{pluginDir}/runemark_import.py" and manifest["interface"] == "raw"
    assert manifest["hooks"][0]["triggeredBy"] == ["Scene.Create.Post"]
    assert set(manifest["settings"]) == set(I.DEFAULTS)
