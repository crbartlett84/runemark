"""Runemark → Stash: import the tags, details, people and chapters Runemark wrote into your videos.

Runs as a Stash plugin (task "Import from Runemark", and a hook on new scenes). Standard library only.
What goes where: README.md beside this file. Format of the data: docs/data-format.md in the Runemark repository.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runemark_read as R  # noqa: E402

PLUGIN_ID = "runemark"
DEFAULTS = {"overwrite": False, "yearOnlyDates": False, "importCover": True,
            "createPerformers": True}
# Stash performer fields Runemark attributes can map to, and how to convert a value
STRING_FIELDS = {"disambiguation", "birthdate", "ethnicity", "country", "eye_color", "hair_color", "measurements",
                 "tattoos", "piercings", "career_length", "details", "death_date"}
INT_FIELDS = {"height_cm", "weight"}
GENDERS = {"FEMALE", "MALE", "INTERSEX", "NON_BINARY", "TRANSGENDER_FEMALE", "TRANSGENDER_MALE"}


# ------------------------------------------------------------------ talking to Stash
def log(level: str, message: str) -> None:
    """Stash's plugin log protocol: \\x01 + level (t d i w e p) + \\x02 + text, on stderr."""
    sys.stderr.write(f"\x01{level}\x02{message}\n")
    sys.stderr.flush()


class Stash:
    def __init__(self, conn: dict):
        scheme = conn.get("Scheme", "http")
        host = conn.get("Host") or "localhost"
        if host in ("0.0.0.0", "::"):
            host = "localhost"
        self.url = f"{scheme}://{host}:{conn.get('Port', 9999)}/graphql"
        cookie = conn.get("SessionCookie") or {}
        self.headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if cookie.get("Name"):
            self.headers["Cookie"] = f"{cookie['Name']}={cookie.get('Value', '')}"
        if conn.get("ApiKey"):
            self.headers["ApiKey"] = conn["ApiKey"]
        self._tags: dict[str, str] = {}
        self._studios: dict[str, str] = {}

    def q(self, query: str, variables: dict | None = None) -> dict:
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request(self.url, body, self.headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            out = json.loads(resp.read().decode())
        if out.get("errors"):
            raise RuntimeError("; ".join(e.get("message", str(e)) for e in out["errors"]))
        return out["data"]

    def settings(self) -> dict:
        data = self.q("{ configuration { plugins } }")
        mine = (data["configuration"].get("plugins") or {}).get(PLUGIN_ID) or {}
        return {**DEFAULTS, **{k: v for k, v in mine.items() if v is not None}}

    def studio_id(self, name: str) -> str:
        """The studio with this name (or alias), created if Stash doesn't have it."""
        key = name.casefold()
        if key in self._studios:
            return self._studios[key]
        found = []
        for field in ("name", "aliases"):
            found = self.q("query($n: String!) { findStudios(studio_filter: {%s: {value: $n, modifier: EQUALS}}) "
                           "{ studios { id } } }" % field, {"n": name})["findStudios"]["studios"]
            if found:
                break
        studio_id = found[0]["id"] if found else self.q(
            "mutation($n: String!) { studioCreate(input: {name: $n}) { id } }", {"n": name})["studioCreate"]["id"]
        self._studios[key] = studio_id
        return studio_id

    def tag_id(self, name: str) -> str:
        key = name.casefold()
        if key in self._tags:
            return self._tags[key]
        found = self.q("query($n: String!) { findTags(tag_filter: {name: {value: $n, modifier: EQUALS}}) "
                       "{ tags { id name } } }", {"n": name})["findTags"]["tags"]
        if not found:   # an alias of an existing tag?
            found = self.q("query($n: String!) { findTags(tag_filter: {aliases: {value: $n, modifier: EQUALS}}) "
                           "{ tags { id name } } }", {"n": name})["findTags"]["tags"]
        tag_id = found[0]["id"] if found else self.q("mutation($n: String!) { tagCreate(input: {name: $n}) { id } }",
                                                     {"n": name})["tagCreate"]["id"]
        self._tags[key] = tag_id
        return tag_id


SCENE_FIELDS = ("id title details date rating100 urls stash_ids { endpoint stash_id } files { path } tags { id name } "
                "performers { id name } studio { id } "
                "scene_markers { id seconds end_seconds title primary_tag { id name } } paths { screenshot }")


# ------------------------------------------------------------------ people → performers
PERFORMER_FIELDS = ("id name alias_list custom_fields hair_color ethnicity eye_color country height_cm weight measurements "
                    "tattoos piercings career_length details birthdate disambiguation gender death_date image_path "
                    "urls stash_ids { endpoint stash_id }")


def find_performer(stash: Stash, person: dict) -> dict | None:
    """By a database id in their links (StashDB and others), then by name, then by any alias (Runemark's aliases may
    be Stash's name, or listed as Stash aliases)."""
    for sid in R.stash_ids(person.get("links") or [], "performers"):
        found = stash.q("query($e: String!, $i: String!) { findPerformers(performer_filter: {stash_id_endpoint: "
                        "{endpoint: $e, stash_id: $i, modifier: EQUALS}}) { performers { %s } } }" % PERFORMER_FIELDS,
                        {"e": sid["endpoint"], "i": sid["stash_id"]})["findPerformers"]["performers"]
        if found:
            return found[0]
    for name in [person["name"], *person.get("aliases", [])]:
        for field in ("name", "aliases"):
            found = stash.q("query($n: String!) { findPerformers(performer_filter: {%s: {value: $n, modifier: EQUALS}}) "
                            "{ performers { %s } } }" % (field, PERFORMER_FIELDS), {"n": name})["findPerformers"]["performers"]
            if found:
                return found[0]
    return None


def performer_fields(person: dict, existing: dict | None, overwrite: bool) -> dict:
    """Stash fields for a person: aliases, mapped attributes, and the rest as custom fields ("Hair": "Red").
    Fill-blanks unless overwrite; aliases and custom fields are always merged, never removed."""
    out: dict = {}
    have = existing or {}
    aliases = list(have.get("alias_list") or [])
    for a in person.get("aliases", []):
        if a.casefold() not in {x.casefold() for x in aliases} and a.casefold() != (have.get("name") or "").casefold():
            aliases.append(a)
    if aliases != list(have.get("alias_list") or []):
        out["alias_list"] = aliases
    custom = dict(have.get("custom_fields") or {})
    custom_changed = False
    by_field: dict[str, list[str]] = {}
    for v in person.get("values", []):
        attr = v.get("attribute") or {}
        field = (attr.get("stash_field") or "").strip()
        if field:
            by_field.setdefault(field, []).append(v["value"])
        elif attr.get("name"):
            key = attr["name"]
            joined = "; ".join(x["value"] for x in person["values"] if (x.get("attribute") or {}).get("name") == key)
            if custom.get(key) != joined and (overwrite or key not in custom):
                custom[key] = joined
                custom_changed = True
    for field, values in by_field.items():
        value = "; ".join(values)
        if field in INT_FIELDS:
            try:
                value = int(float(values[0]))
            except ValueError:
                continue
        elif field == "gender":
            value = values[0].upper().replace(" ", "_").replace("-", "_")
            if value not in GENDERS:
                continue
        elif field not in STRING_FIELDS:
            continue
        if overwrite or not have.get(field):
            if have.get(field) != value:
                out[field] = value
    if custom_changed:
        out["custom_fields"] = custom
    # their links: web pages, and database entries (StashDB and others) as stash ids. Added, never removed.
    links = [x for x in person.get("links") or [] if x not in (have.get("urls") or [])]
    if links:
        out["urls"] = list(have.get("urls") or []) + links
    ids = [x for x in R.stash_ids(person.get("links") or [], "performers") if x not in _ids(have)]
    if ids:
        out["stash_ids"] = _ids(have) + ids
    # the photo Runemark carries, when Stash shows its placeholder (a performer without an image of their own)
    photo = person.get("photo")
    if photo and (existing is None or "default=true" in (have.get("image_path") or "") or not have.get("image_path")):
        out["image"] = "data:image/jpeg;base64," + photo
    return out


def _ids(record: dict) -> list[dict]:
    return [{"endpoint": x["endpoint"], "stash_id": x["stash_id"]} for x in record.get("stash_ids") or []]


def performer_id(stash: Stash, person: dict, settings: dict) -> str | None:
    existing = find_performer(stash, person)
    fields = performer_fields(person, existing, settings["overwrite"])
    if existing:
        if fields:
            if "custom_fields" in fields:
                fields["custom_fields"] = {"partial": fields["custom_fields"]}
            stash.q("mutation($i: PerformerUpdateInput!) { performerUpdate(input: $i) { id } }",
                    {"i": {"id": existing["id"], **fields}})
        return existing["id"]
    if not settings["createPerformers"]:
        return None
    created = stash.q("mutation($i: PerformerCreateInput!) { performerCreate(input: $i) { id } }",
                      {"i": {"name": person["name"], **fields}})["performerCreate"]["id"]
    log("i", f"Runemark: added performer {person['name']}")
    return created


# ------------------------------------------------------------------ one scene
def import_scene(stash: Stash, scene: dict, settings: dict) -> str:
    """Bring one scene up to date with what Runemark wrote in its file. Returns a short summary ("" = nothing)."""
    path = next((f["path"] for f in scene.get("files") or [] if R.read(f["path"]) is not None), None)
    if not path:
        return ""
    info = R.read(path)
    hand, profile = R.hand_and_profile_tags(info)
    people = R.people(info)
    if not (info["keywords"] or info["title"] or info["data"] or info["rating"] or info["description"]
            or info.get("studio") or info.get("links")):
        return ""
    overwrite = settings["overwrite"]
    update: dict = {"id": scene["id"]}
    if info["title"] and (overwrite or not scene.get("title")):
        update["title"] = info["title"]
    if info["description"] and (overwrite or not scene.get("details")):
        update["details"] = info["description"]
    date = R.date_of(info["date"], settings["yearOnlyDates"])
    if date and (overwrite or not scene.get("date")):
        update["date"] = date
    if info.get("studio") and (overwrite or not scene.get("studio")):
        sid = stash.studio_id(info["studio"])
        if sid != (scene.get("studio") or {}).get("id"):
            update["studio_id"] = sid
    if info["rating"] and (overwrite or not scene.get("rating100")):
        update["rating100"] = info["rating"] * 20
    # links: web addresses become the scene's URLs; database pages (StashDB…) also link it there. Added, never removed.
    new_urls = [x for x in info.get("links") or [] if x not in (scene.get("urls") or [])]
    if new_urls:
        update["urls"] = list(scene.get("urls") or []) + new_urls
    new_ids = [x for x in R.stash_ids(info.get("links") or [], "scenes") if x not in _ids(scene)]
    if new_ids:
        update["stash_ids"] = _ids(scene) + new_ids
    # people who are (or become) performers aren't tags; people Stash doesn't have as performers (with "Add
    # performers" off) keep their name as a tag, so they're still findable. Everything else is a tag.
    current_people = {p["id"] for p in scene.get("performers") or []}
    performer_ids = set(current_people)
    names: set[str] = set()
    for person in people:
        pid = performer_id(stash, person, settings)
        if pid:
            performer_ids.add(pid)
            names.add(person["name"].casefold())
            if person.get("aliases_as_tags"):
                names |= {a.casefold() for a in person.get("aliases", [])}
    if performer_ids != current_people:
        update["performer_ids"] = sorted(performer_ids)
    wanted = [t for t in dict.fromkeys(hand + profile + info["genre"]) if t.casefold() not in names]
    current = {t["id"] for t in scene.get("tags") or []}
    tag_ids = list(current | {stash.tag_id(t) for t in wanted})
    if set(tag_ids) != current:
        update["tag_ids"] = tag_ids
    if settings["importCover"] and info["cover"] and (overwrite or not _has_custom_cover(scene)):
        data, mime = info["cover"]
        update["cover_image"] = f"data:{mime};base64," + base64.b64encode(data).decode()
    changed = [k for k in update if k != "id"]
    if changed:
        stash.q("mutation($i: SceneUpdateInput!) { sceneUpdate(input: $i) { id } }", {"i": update})
    markers = import_chapters(stash, scene, path, settings, R.chapter_ends(info))
    bits = changed + ([f"{markers} marker(s)"] if markers else [])
    return ", ".join(bits)


def _has_custom_cover(scene: dict) -> bool:
    # Stash always has a generated screenshot; we can't tell it from a chosen cover, so only fill when overwrite is
    # off and the scene has never been touched by a scraper or by hand (no title and no details).
    return bool(scene.get("title") or scene.get("details"))


def import_chapters(stash: Stash, scene: dict, path: str, settings: dict, ends: dict | None = None) -> int:
    """Chapters become scene markers (start, end) whose tag is the chapter's name ("Missionary"), so Stash's marker
    wall and RuneStash's chapter wall group them by name. Ones already there (same start, same name) are skipped,
    except that a recorded end is brought up to date on them.

    The end is the chapter's own end when Runemark recorded one ("End here": the action stops and filler follows,
    kept in the data block, never in the chapter track), else the next chapter's start."""
    chapters = R.chapters(path)
    if not chapters:
        return 0
    ends = ends or {}
    existing = {}
    for m in scene.get("scene_markers") or []:
        name = (m.get("primary_tag") or {}).get("name") or ""
        if name.casefold() == "chapter":       # made by an earlier version: the name was in the title
            name = m.get("title") or ""
        existing[(round(float(m["seconds"]), 1), name.strip().casefold())] = m
    added = 0
    chapters = sorted(chapters, key=lambda c: c["start"])
    for n, c in enumerate(chapters):
        name = c["title"].strip()
        if not name:
            continue
        if n == 0 and len(chapters) > 1 and c["start"] <= 0.0005 and name == "Start":
            continue        # Runemark 0.11.0's hidden lead-in (MP4 chapter lists start at 0:00): not a moment
        recorded = ends.get(f"{c['start']:.3f}")
        if recorded is None:
            recorded = R.end_near(ends, c["start"])
        if recorded is not None and recorded <= c["start"]:
            recorded = None
        end = recorded if recorded is not None else c["end"]
        key = (round(c["start"], 1), name.casefold())
        found = existing.get(key)
        if found is not None:
            had = found.get("end_seconds")
            if recorded is not None and (had is None or abs(float(had) - end) > 0.05):
                stash.q("mutation($i: SceneMarkerUpdateInput!) { sceneMarkerUpdate(input: $i) { id } }",
                        {"i": {"id": found["id"], "scene_id": scene["id"], "seconds": float(found["seconds"]),
                               "end_seconds": end, "title": found.get("title") or "",
                               "primary_tag_id": (found.get("primary_tag") or {}).get("id")
                               or stash.tag_id(name)}})
                added += 1
            continue
        stash.q("mutation($i: SceneMarkerCreateInput!) { sceneMarkerCreate(input: $i) { id } }",
                {"i": {"scene_id": scene["id"], "seconds": c["start"], "end_seconds": end,
                       "title": "", "primary_tag_id": stash.tag_id(name)}})
        existing[key] = {"id": None, "seconds": c["start"], "end_seconds": end}
        added += 1
    return added


# ------------------------------------------------------------------ entry points
def run_all(stash: Stash, settings: dict) -> str:
    page, done, changed, problems = 1, 0, 0, 0
    while True:
        result = stash.q("query($p: Int!) { findScenes(filter: {per_page: 100, page: $p, sort: \"path\"}) "
                         "{ count scenes { %s } } }" % SCENE_FIELDS, {"p": page})["findScenes"]
        for scene in result["scenes"]:
            done += 1
            try:
                summary = import_scene(stash, scene, settings)
                if summary:
                    changed += 1
                    log("d", f"Runemark: scene {scene['id']}: {summary}")
            except Exception as exc:  # noqa: BLE001
                problems += 1
                log("w", f"Runemark: scene {scene['id']}: {exc}")
            log("p", str(done / max(1, result["count"])))
        if page * 100 >= result["count"]:
            break
        page += 1
    return f"Runemark: {done} scene(s) checked, {changed} updated" + (f", {problems} problem(s)" if problems else "")


def run_hook(stash: Stash, settings: dict, context: dict) -> str:
    scene_id = str(context.get("id") or "")
    if not scene_id:
        return "no scene"
    scene = stash.q("query($id: ID!) { findScene(id: $id) { %s } }" % SCENE_FIELDS, {"id": scene_id})["findScene"]
    if scene is None:
        return "scene not found"
    return import_scene(stash, scene, settings) or "nothing from Runemark"


def main() -> None:
    payload = json.loads(sys.stdin.read() or "{}")
    stash = Stash(payload.get("server_connection") or {})
    args = payload.get("args") or {}
    try:
        settings = stash.settings()
        context = args.get("hookContext")
        message = run_hook(stash, settings, context) if context else run_all(stash, settings)
        log("i", message)
        print(json.dumps({"output": message}))
    except Exception as exc:  # noqa: BLE001
        log("e", f"Runemark: {exc}")
        print(json.dumps({"error": str(exc)}))


if __name__ == "__main__":
    main()
