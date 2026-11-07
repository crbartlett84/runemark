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
        self.enrich_studios = False          # 0.4.0: no StashDB in these tests
        self.studios = {"50": {"id": "50", "name": "Old Studio", "aliases": ["Sunny"]}}
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
        if "findPerformers" in query and "stash_id_endpoint" in query:
            hits = [p for p in self.performers.values()
                    if {"endpoint": v["e"], "stash_id": v["i"]} in (p.get("stash_ids") or [])]
            return {"findPerformers": {"performers": hits}}
        if "findPerformers" in query:
            field = "aliases" if "aliases:" in query else "name"
            hits = [p for p in self.performers.values()
                    if (p["name"].casefold() == v["n"].casefold() if field == "name"
                        else v["n"].casefold() in [a.casefold() for a in p.get("alias_list", [])])]
            return {"findPerformers": {"performers": hits}}
        if "findStudios" in query:
            hits = [{"id": i} for i, st in self.studios.items()
                    if (st["name"].casefold() == v["n"].casefold() if "name:" in query
                        else v["n"].casefold() in [a.casefold() for a in st["aliases"]])]
            return {"findStudios": {"studios": hits}}
        if "studioCreate" in query:
            new = str(60 + len(self.studios))
            self.studios[new] = {"id": new, "name": v["n"], "aliases": []}
            return {"studioCreate": {"id": new}}
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
                elif k == "studio_id":
                    self.scene["studio"] = {"id": val}
                elif k == "custom_fields":
                    self.scene.setdefault("custom_fields", {}).update(val["partial"])
                elif k == "performer_ids":
                    self.scene["performers"] = [{"id": p, "name": self.performers[p]["name"]} for p in val]
                elif k != "id":
                    self.scene[k] = val
            return {"sceneUpdate": {"id": "1"}}
        if "sceneMarkerCreate" in query:
            self.markers.append(v["i"])
            self.scene["scene_markers"].append({"id": str(len(self.markers)), "seconds": v["i"]["seconds"],
                                                "title": v["i"]["title"],
                                                "primary_tag": {"name": self.tags[v["i"]["primary_tag_id"]]}})
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
    # Each chapter's name is its marker's tag (no generic "Chapter" tag, no title)
    assert [stash.tags[m["primary_tag_id"]] for m in stash.markers] == ["Chapter 1", "Swimming"]
    assert all(m["title"] == "" for m in stash.markers)
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
    assert [t["defaultArgs"]["mode"] for t in manifest["tasks"]] == ["all", "changed", "studios"]


def test_chapters_from_the_old_version_are_not_added_again(tagged):
    """Markers made before (tag "Chapter", name in the title) count as already there."""
    stash = FakeStash(tagged)
    stash._tags = {}
    stash.tag_id = I.Stash.tag_id.__get__(stash)
    stash.scene["scene_markers"] = [
        {"id": "a", "seconds": 0.0, "title": "Chapter 1", "primary_tag": {"name": "Chapter"}},
        {"id": "b", "seconds": 2.0, "title": "Swimming", "primary_tag": {"name": "Chapter"}},
    ]
    assert I.import_chapters(stash, stash.scene, str(tagged), dict(I.DEFAULTS)) == 0


def test_studio_is_imported_matching_names_and_aliases(tmp_path, media):
    path = tmp_path / "s.mp4"
    shutil.copy2(media["mp4"], path)
    save_mp4(path, keywords=["Lake"], genre=[], meta=VideoMeta("", None, "", 0, "Sunny Films"))
    assert R.read(str(path))["studio"] == "Sunny Films"
    stash = FakeStash(path)
    stash._tags, stash._studios = {}, {}
    stash.tag_id = I.Stash.tag_id.__get__(stash)
    stash.studio_id = I.Stash.studio_id.__get__(stash)
    I.import_scene(stash, stash.scene, dict(I.DEFAULTS))
    assert stash.scene["studio"]["id"] == "61" and stash.studios["61"]["name"] == "Sunny Films"   # created
    assert I.import_scene(stash, stash.scene, dict(I.DEFAULTS)) == ""                            # once

    save_mp4(path, keywords=["Lake"], genre=[], meta=VideoMeta("", None, "", 0, "sunny"))
    other = FakeStash(path, {"studio": {"id": "9"}})
    other._tags, other._studios = {}, {}
    other.tag_id = I.Stash.tag_id.__get__(other)
    other.studio_id = I.Stash.studio_id.__get__(other)
    I.import_scene(other, other.scene, dict(I.DEFAULTS))
    assert other.scene["studio"] == {"id": "9"}                      # Stash's own studio kept
    I.import_scene(other, other.scene, {**I.DEFAULTS, "overwrite": True})
    assert other.scene["studio"] == {"id": "50"}                     # matched by alias, not created


def test_links_become_urls_and_stash_ids_and_unlinked_people_stay_tags(tmp_path, media):
    """0.7.0: a video's links become the scene's URLs (database pages also become stash ids), a full date is used,
    a person's StashDB link finds their performer; with "Add performers" off, someone Stash doesn't have stays a tag."""
    path = tmp_path / "scene.mp4"
    shutil.copy2(media["mp4_cover"], path)
    db = TagDB(tmp_path / "t.db")
    star = db.add_person("Star One")
    db.set_person_links(star.id, ["https://stashdb.org/performers/779ce296-dcfc-4e83-a5fe-a309f592be2a"])
    costar = db.add_person("Co Star")
    scene_id = "bffb88f3-726d-4ef8-ab81-703281c19c3f"
    links = ["https://example.com/videos/123", f"https://stashdb.org/scenes/{scene_id}"]
    save_mp4(path, keywords=["Beach"], genre=[], meta=VideoMeta("T", 2022, "", 0, None, links, "2022-12-16"))
    P.save_people(db, path, [(db.person(star.id), []), (db.person(costar.id), [])], label="t")
    db.close()
    info = R.read(str(path))
    assert info["links"] == links and info["date"].startswith("2022-12-16")

    stash = FakeStash(path)
    stash._tags = {}
    stash.tag_id = I.Stash.tag_id.__get__(stash)
    stash.performers["7"] = {"id": "7", "name": "Stage Name", "alias_list": [],
                             "stash_ids": [{"endpoint": "https://stashdb.org/graphql",
                                            "stash_id": "779ce296-dcfc-4e83-a5fe-a309f592be2a"}]}
    settings = dict(I.DEFAULTS, createPerformers=False)
    I.import_scene(stash, stash.scene, settings)
    s = stash.scene
    assert s["date"] == "2022-12-16" and s["urls"] == links
    assert s["stash_ids"] == [{"endpoint": "https://stashdb.org/graphql", "stash_id": scene_id}]
    assert [p["id"] for p in s["performers"]] == ["7"]               # found by their StashDB id, not by name
    assert sorted(t["name"] for t in s["tags"]) == ["Beach", "Co Star"]   # not a performer here: still a tag
    assert len(stash.performers) == 1
    assert I.import_scene(stash, stash.scene, settings) == ""        # again: nothing new


# ------------------------------------------------------------------ 0.4.0: follow Runemark, studios from StashDB
def _fake(tagged, scene=None):
    stash = FakeStash(tagged, scene)
    stash._tags = {}
    stash.tag_id = I.Stash.tag_id.__get__(stash)
    return stash


def test_a_changed_file_brings_runemarks_values_back(tagged):
    stash = _fake(tagged)
    stash.scene["files"] = [{"path": str(tagged), "mod_time": "2026-11-07T10:00:00Z", "size": 100}]
    I.import_scene(stash, stash.scene, dict(I.DEFAULTS))
    assert stash.scene["custom_fields"][I.STAMP] == "2026-11-07T10:00:00Z|100"      # the version it imported
    stash.scene["title"] = "Edited in Stash"
    extra = I.Stash.tag_id.__get__(stash)("Stash only")
    stash.scene["tags"].append({"id": extra, "name": "Stash only"})
    I.import_scene(stash, stash.scene, dict(I.DEFAULTS))                                # same file: blanks only
    assert stash.scene["title"] == "Edited in Stash"
    stash.scene["files"][0]["mod_time"] = "2026-11-07T11:00:00Z"                       # edited in Runemark
    summary = I.import_scene(stash, stash.scene, dict(I.DEFAULTS), follow=True)
    assert stash.scene["title"] == "Beach Day" and "title" in summary
    assert "Stash only" not in [t["name"] for t in stash.scene["tags"]]                 # Runemark's tags are the list
    assert stash.scene["custom_fields"][I.STAMP] == "2026-11-07T11:00:00Z|100"


class ChangedStash(FakeStash):
    """FakeStash plus findScenes / findScene for run_changed."""

    def q(self, query, variables=None):
        if "findScenes" in query:
            return {"findScenes": {"count": 1, "scenes": [self.scene]}}
        if "findScene(" in query:
            return {"findScene": self.scene}
        return super().q(query, variables)


def test_run_changed_reads_only_files_that_moved(tagged, monkeypatch):
    stash = ChangedStash(tagged)
    stash._tags = {}
    stash.tag_id = I.Stash.tag_id.__get__(stash)
    stash.scene["files"] = [{"path": str(tagged), "mod_time": "t1", "size": 5}]
    reads = []
    real = I.R.read
    monkeypatch.setattr(I.R, "read", lambda p: (reads.append(p), real(p))[1])
    assert "1 updated" in I.run_changed(stash, dict(I.DEFAULTS))                       # first time: imported
    assert stash.scene["title"] == "Beach Day"
    reads.clear()
    stash.scene["title"] = "Edited in Stash"
    assert "0 changed file(s) read" in I.run_changed(stash, dict(I.DEFAULTS))           # unchanged: not even read
    assert reads == [] and stash.scene["title"] == "Edited in Stash"
    stash.scene["files"][0]["mod_time"] = "t2"
    I.run_changed(stash, dict(I.DEFAULTS))
    assert stash.scene["title"] == "Beach Day"                                          # followed Runemark
    stash.scene["files"][0]["mod_time"] = "t3"
    stash.scene["title"] = "Edited again"
    I.run_changed(stash, {**I.DEFAULTS, "followRunemark": False})
    assert stash.scene["title"] == "Edited again"                                       # off: blanks only


class BoxStash:
    """StashDB lookups through Stash: one studio, a StashDB match with a parent network."""

    def __init__(self, box=True, name="Sunny Studio"):
        self.box = box
        self.studios = {"1": {"id": "1", "name": name, "aliases": [], "urls": [], "details": "",
                              "image_path": "/studio/1/image?default=true", "stash_ids": [],
                              "parent_studio": None, "custom_fields": {}}}
        self.updates, self.scrapes = [], 0
        self._studios, self._box, self.enrich_studios = {}, None, False

    def q(self, query, variables=None):
        v = variables or {}
        if "stashBoxes" in query:
            boxes = [{"endpoint": "https://stashdb.org/graphql"}] if self.box else []
            return {"configuration": {"general": {"stashBoxes": boxes}}}
        if "findStudio(" in query:
            return {"findStudio": self.studios.get(v["id"])}
        if "scrapeSingleStudio" in query:
            self.scrapes += 1
            return {"scrapeSingleStudio": [
                {"name": "sunny studio", "urls": ["https://sunny.example"], "image": "https://img/sunny.png",
                 "details": "A studio.", "aliases": [], "remote_site_id": "abc",
                 "parent": {"name": "Big Network", "remote_site_id": "net"}},
                {"name": "Sunny Studios Other", "remote_site_id": "zzz"}] if v["i"]["query"] != "Big Network"
                else [{"name": "Big Network", "image": "https://img/net.png", "remote_site_id": "net", "urls": []}]}
        if "findStudios" in query:
            hits = [{"id": i} for i, st in self.studios.items() if st["name"].casefold() == v["n"].casefold()]
            return {"findStudios": {"studios": hits if "name:" in query else []}}
        if "studioCreate" in query:
            new = str(len(self.studios) + 1)
            self.studios[new] = {"id": new, "name": v["n"], "aliases": [], "urls": [], "details": "",
                                 "image_path": "?default=true", "stash_ids": [], "parent_studio": None,
                                 "custom_fields": {}}
            return {"studioCreate": {"id": new}}
        if "studioUpdate" in query:
            u = dict(v["i"])
            st = self.studios[u.pop("id")]
            st["custom_fields"].update(u.pop("custom_fields")["partial"])
            if "parent_id" in u:
                st["parent_studio"] = {"id": u.pop("parent_id")}
            st.update(u)
            self.updates.append(v["i"])
            return {"studioUpdate": {"id": st["id"]}}
        raise AssertionError(query[:80])

    studio_id = I.Stash.studio_id
    stashdb = I.Stash.stashdb


def test_a_studio_is_filled_in_from_stashdb_once():
    stash = BoxStash()
    assert I.enrich_studio(stash, "1") == "StashDB id, logo, web address, details, network Big Network"
    st = stash.studios["1"]
    assert st["image"] == "https://img/sunny.png" and st["urls"] == ["https://sunny.example"]
    assert st["stash_ids"] == [{"endpoint": "https://stashdb.org/graphql", "stash_id": "abc"}]
    parent = stash.studios[st["parent_studio"]["id"]]
    assert parent["name"] == "Big Network" and parent["image"] == "https://img/net.png"
    scrapes = stash.scrapes
    assert I.enrich_studio(stash, "1") == "" and stash.scrapes == scrapes                 # asked once


def test_no_exact_match_or_no_stashdb_changes_nothing_but_the_check():
    stash = BoxStash(name="Sunny")                                                       # only near matches
    assert I.enrich_studio(stash, "1") == ""
    st = stash.studios["1"]
    assert st["stash_ids"] == [] and "image" not in st and st["custom_fields"][I.STUDIO_CHECKED]
    none = BoxStash(box=False)
    assert I.enrich_studio(none, "1") == "" and none.updates == []
    assert "no StashDB" in I.run_studios(none)
