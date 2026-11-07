"""Runemark → Stash: import the tags, details, people and chapters Runemark wrote into your videos.

Runs as a Stash plugin (tasks "Import from Runemark", "Import changed videos", "Fill in studios from StashDB", and
a hook on new scenes). Standard library only.

0.4.0: Stash follows Runemark. Each scene remembers which version of its file it last imported (custom field
runemark_file = modified time|size); "Import changed videos" re-imports scenes whose file changed since, and Runemark's
title, details, date, rating, studio, tags, people and cover replace Stash's (setting followRunemark, default on).
New studios are filled in from StashDB (logo, web address, parent network, StashDB id) when Stash has StashDB as a
metadata provider (setting studiosFromStashDB, default on).
What goes where: README.md beside this file. Format of the data: docs/data-format.md in the Runemark repository.
"""
from __future__ import annotations

import base64
import json
import re
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runemark_read as R  # noqa: E402

PLUGIN_ID = "runemark"
DEFAULTS = {"overwrite": False, "yearOnlyDates": False, "importCover": True,
            "createPerformers": True, "followRunemark": True, "studiosFromStashDB": True}
STAMP = "runemark_file"          # scene custom field: the file version last imported
STUDIO_CHECKED = "stashdb_checked"   # studio custom field: looked up on StashDB (date), so it's asked once
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
        self._box: str | None = None
        self.enrich_studios = True

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
        if self.enrich_studios:
            try:
                enrich_studio(self, studio_id)
            except Exception as exc:  # noqa: BLE001
                log("w", f"Runemark: StashDB lookup for studio {name}: {exc}")
        return studio_id

    def stashdb(self) -> str | None:
        """The StashDB endpoint Stash is set up with (Settings > Metadata Providers), or None."""
        if self._box is None:
            boxes = self.q("{ configuration { general { stashBoxes { endpoint } } } }")
            boxes = boxes["configuration"]["general"]["stashBoxes"] or []
            self._box = next((b["endpoint"] for b in boxes if "stashdb.org" in b["endpoint"]),
                             boxes[0]["endpoint"] if boxes else "")
        return self._box or None

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


SCENE_FIELDS = ("id title details date rating100 urls stash_ids { endpoint stash_id } files { path mod_time size } "
                "tags { id name } performers { id name } studio { id } custom_fields "
                "scene_markers { id seconds end_seconds title primary_tag { id name } } paths { screenshot }")


# ------------------------------------------------------------------ studios from StashDB
STUDIO_FIELDS = "id name urls aliases details image_path stash_ids { endpoint stash_id } parent_studio { id } custom_fields"


def _same(a: str, b: str) -> bool:
    return " ".join((a or "").split()).casefold() == " ".join((b or "").split()).casefold()


def enrich_studio(stash: Stash, studio_id: str, depth: int = 0) -> str:
    """Fill in a studio from StashDB once: logo, web addresses, details, StashDB id and parent network. Only blanks
    are filled, only on an exact name (or alias) match. Returns what changed ("" = nothing)."""
    box = stash.stashdb()
    if not box:
        return ""
    studio = stash.q("query($id: ID!) { findStudio(id: $id) { %s } }" % STUDIO_FIELDS, {"id": studio_id})["findStudio"]
    if not studio or (studio.get("custom_fields") or {}).get(STUDIO_CHECKED) \
            or any(x["endpoint"] == box for x in studio.get("stash_ids") or []):
        return ""
    found = stash.q("query($s: ScraperSourceInput!, $i: ScrapeSingleStudioInput!) { scrapeSingleStudio(source: $s, "
                    "input: $i) { name urls image details aliases remote_site_id parent { name remote_site_id } } }",
                    {"s": {"stash_box_endpoint": box}, "i": {"query": studio["name"]}})["scrapeSingleStudio"] or []
    names = [studio["name"], *(studio.get("aliases") or [])]
    match = next((f for f in found if any(_same(f.get("name"), n) for n in names)), None)
    update: dict = {"id": studio_id, "custom_fields": {"partial": {STUDIO_CHECKED: _today()}}}
    changed = []
    if match and match.get("remote_site_id"):
        update["stash_ids"] = _ids(studio) + [{"endpoint": box, "stash_id": match["remote_site_id"]}]
        changed.append("StashDB id")
        if match.get("image") and "default=true" in (studio.get("image_path") or "default=true"):
            update["image"] = match["image"]
            changed.append("logo")
        urls = [u for u in match.get("urls") or [] if u not in (studio.get("urls") or [])]
        if urls:
            update["urls"] = list(studio.get("urls") or []) + urls
            changed.append("web address")
        if match.get("details") and not studio.get("details"):
            update["details"] = match["details"]
            changed.append("details")
        parent = match.get("parent") or {}
        if parent.get("name") and not studio.get("parent_studio") and depth == 0 and not _same(parent["name"],
                                                                                             studio["name"]):
            was = stash.enrich_studios
            stash.enrich_studios = False                    # the parent is enriched below, once
            try:
                update["parent_id"] = stash.studio_id(parent["name"])
            finally:
                stash.enrich_studios = was
            enrich_studio(stash, update["parent_id"], depth + 1)
            changed.append(f"network {parent['name']}")
    stash.q("mutation($i: StudioUpdateInput!) { studioUpdate(input: $i) { id } }", {"i": update})
    if changed:
        log("i", f"Runemark: studio {studio['name']} from StashDB: {', '.join(changed)}")
    return ", ".join(changed)


def _today() -> str:
    import datetime
    return datetime.date.today().isoformat()


def run_studios(stash: Stash) -> str:
    """Fill in every studio not yet looked up on StashDB."""
    if not stash.stashdb():
        return "Runemark: Stash has no StashDB metadata provider (Settings > Metadata Providers)"
    page, done, filled = 1, 0, 0
    while True:
        res = stash.q("query($p: Int!) { findStudios(filter: {per_page: 100, page: $p, sort: \"name\"}) "
                      "{ count studios { id } } }", {"p": page})["findStudios"]
        for st in res["studios"]:
            done += 1
            try:
                filled += bool(enrich_studio(stash, st["id"]))
            except Exception as exc:  # noqa: BLE001
                log("w", f"Runemark: studio {st['id']}: {exc}")
            log("p", str(done / max(1, res["count"])))
        if page * 100 >= res["count"]:
            break
        page += 1
    return f"Runemark: {done} studio(s) checked, {filled} filled in from StashDB"


# ------------------------------------------------------------------ which file version a scene last imported
def file_stamp(f: dict) -> str:
    return f"{f.get('mod_time') or ''}|{f.get('size') or ''}"


def scene_stamp(scene: dict) -> str:
    """The stamp of the scene's first file (Runemark's file when there's one, see import_scene)."""
    files = scene.get("files") or []
    return file_stamp(files[0]) if files else ""


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
        elif field == "career_length":
            value = career(values[0])
            if not value:
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


def career(text: str) -> str:
    """A career span as Stash parses it: "2013-2021" or "2019-" (StashDB writes an en dash, "2019–", which
    Stash refuses: the whole performer, and so the scene, failed). "" when it isn't a year span."""
    t = re.sub(r"\s*[‐-―−-]\s*", "-", (text or "").strip())
    t = re.sub(r"(?i)\s*(present|now|today)$", "", t)
    m = re.fullmatch(r"(\d{4})(?:-(\d{4})?)?", t)
    if not m:
        return ""
    return m.group(1) + ("-" + (m.group(2) or "") if "-" in t else "")


def _refused_field(exc: Exception, fields: dict) -> str | None:
    """The performer field Stash's error names (it says "could not parse career_length ..."), if it's one we sent."""
    text = str(exc)
    return next((f for f in fields if f in text), None)


def _performer_write(stash: Stash, query: str, base: dict, fields: dict, name: str) -> dict | None:
    """performerUpdate/Create, leaving out a field Stash refuses (logged) rather than failing the scene."""
    fields = dict(fields)
    for _ in range(len(fields) + 1):
        try:
            return stash.q(query, {"i": {**base, **fields}})
        except RuntimeError as exc:
            bad = _refused_field(exc, fields)
            if not bad:
                log("w", f"Runemark: performer {name}: {exc}")
                return None
            log("w", f"Runemark: performer {name}: left out {bad} (Stash refused {fields[bad]!r})")
            fields.pop(bad)
    return None


def _ids(record: dict) -> list[dict]:
    return [{"endpoint": x["endpoint"], "stash_id": x["stash_id"]} for x in record.get("stash_ids") or []]


def performer_id(stash: Stash, person: dict, settings: dict) -> str | None:
    existing = find_performer(stash, person)
    fields = performer_fields(person, existing, settings["overwrite"])
    if existing:
        if fields:
            if "custom_fields" in fields:
                fields["custom_fields"] = {"partial": fields["custom_fields"]}
            _performer_write(stash, "mutation($i: PerformerUpdateInput!) { performerUpdate(input: $i) { id } }",
                             {"id": existing["id"]}, fields, person["name"])
        return existing["id"]
    if not settings["createPerformers"]:
        return None
    done = _performer_write(stash, "mutation($i: PerformerCreateInput!) { performerCreate(input: $i) { id } }",
                            {"name": person["name"]}, fields, person["name"])
    if not done:
        return None
    created = done["performerCreate"]["id"]
    log("i", f"Runemark: added performer {person['name']}")
    return created


# ------------------------------------------------------------------ one scene
def import_scene(stash: Stash, scene: dict, settings: dict, follow: bool = False) -> str:
    """Bring one scene up to date with what Runemark wrote in its file. Returns a short summary ("" = nothing).
    follow (0.4.0): the file changed since the last import, so Runemark's values replace Stash's."""
    rm_file = next((f for f in scene.get("files") or [] if R.read(f["path"]) is not None), None)
    stamp = file_stamp(rm_file) if rm_file else scene_stamp(scene)
    stamped = (scene.get("custom_fields") or {}).get(STAMP) == stamp
    if not rm_file:
        if not stamped:
            _stamp(stash, scene["id"], stamp)
        return ""
    path = rm_file["path"]
    info = R.read(path)
    hand, profile = R.hand_and_profile_tags(info)
    people = R.people(info)
    if not (info["keywords"] or info["title"] or info["data"] or info["rating"] or info["description"]
            or info.get("studio") or info.get("links")):
        if not stamped:
            _stamp(stash, scene["id"], stamp)
        return ""
    overwrite = settings["overwrite"] or follow
    update: dict = {"id": scene["id"]}
    if not stamped:
        update["custom_fields"] = {"partial": {STAMP: stamp}}
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
    resolved: set[str] = set()
    names: set[str] = set()
    for person in people:
        pid = performer_id(stash, person, settings)
        if pid:
            performer_ids.add(pid)
            resolved.add(pid)
            names.add(person["name"].casefold())
            if person.get("aliases_as_tags"):
                names |= {a.casefold() for a in person.get("aliases", [])}
    if follow and info["data"]:
        # Runemark's people are the whole list: performers it no longer has come off (only when its data block is
        # there, so a file Runemark never curated doesn't empty Stash's list)
        performer_ids = resolved
    if performer_ids != current_people:
        update["performer_ids"] = sorted(performer_ids)
    wanted = [t for t in dict.fromkeys(hand + profile + info["genre"]) if t.casefold() not in names]
    current = {t["id"] for t in scene.get("tags") or []}
    wanted_ids = {stash.tag_id(t) for t in wanted}
    tag_ids = list(wanted_ids if follow else current | wanted_ids)   # following: Runemark's tags are the list
    if set(tag_ids) != current:
        update["tag_ids"] = tag_ids
    if settings["importCover"] and info["cover"] and (overwrite or not _has_custom_cover(scene)):
        data, mime = info["cover"]
        update["cover_image"] = f"data:{mime};base64," + base64.b64encode(data).decode()
    changed = [k for k in update if k not in ("id", "custom_fields")]
    if len(update) > 1:
        stash.q("mutation($i: SceneUpdateInput!) { sceneUpdate(input: $i) { id } }", {"i": update})
    markers = import_chapters(stash, scene, path, settings, R.chapter_ends(info))
    bits = changed + ([f"{markers} marker(s)"] if markers else [])
    return ", ".join(bits)


def _stamp(stash: Stash, scene_id: str, stamp: str) -> None:
    stash.q("mutation($i: SceneUpdateInput!) { sceneUpdate(input: $i) { id } }",
            {"i": {"id": scene_id, "custom_fields": {"partial": {STAMP: stamp}}}})


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


def run_changed(stash: Stash, settings: dict) -> str:
    """0.4.0: re-import the scenes whose file changed since their last import (a cheap check on Stash's own file
    records first; the file is only read when its modified time or size moved)."""
    follow = bool(settings["followRunemark"])
    page, done, looked, changed, problems = 1, 0, 0, 0, 0
    while True:
        result = stash.q("query($p: Int!) { findScenes(filter: {per_page: 100, page: $p, sort: \"path\"}) "
                         "{ count scenes { id custom_fields files { path mod_time size } } } }",
                         {"p": page})["findScenes"]
        for brief in result["scenes"]:
            done += 1
            stamps = {file_stamp(f) for f in brief.get("files") or []}
            if (brief.get("custom_fields") or {}).get(STAMP) in stamps:
                continue
            looked += 1
            try:
                scene = stash.q("query($id: ID!) { findScene(id: $id) { %s } }" % SCENE_FIELDS,
                                {"id": brief["id"]})["findScene"]
                summary = import_scene(stash, scene, settings, follow=follow) if scene else ""
                if summary:
                    changed += 1
                    log("d", f"Runemark: scene {brief['id']}: {summary}")
            except Exception as exc:  # noqa: BLE001
                problems += 1
                log("w", f"Runemark: scene {brief['id']}: {exc}")
            log("p", str(done / max(1, result["count"])))
        if page * 100 >= result["count"]:
            break
        page += 1
    return (f"Runemark: {done} scene(s), {looked} changed file(s) read, {changed} updated"
            + (f", {problems} problem(s)" if problems else ""))


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
        stash.enrich_studios = bool(settings["studiosFromStashDB"])
        context = args.get("hookContext")
        mode = args.get("mode") or "all"
        if context:
            message = run_hook(stash, settings, context)
        elif mode == "changed":
            message = run_changed(stash, settings)
        elif mode == "studios":
            message = run_studios(stash)
        else:
            message = run_all(stash, settings)
        log("i", message)
        print(json.dumps({"output": message}))
    except Exception as exc:  # noqa: BLE001
        log("e", f"Runemark: {exc}")
        print(json.dumps({"error": str(exc)}))


if __name__ == "__main__":
    main()
