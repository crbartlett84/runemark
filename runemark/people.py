"""People in videos: who is in a file, and the tags that follow from their profiles.

Each MP4 Runemark saves with people carries a small JSON record (the "data block", see docs/data-format.md):
the people in the video with their aliases and attribute values as they were when it was saved, and the list of
tags that came from those profiles. That record is what lets Runemark

  * tell hand-typed tags from profile tags, so a profile change only ever touches the tags it added,
  * update every video with a person when their profile changes,
  * rebuild its people list from the files alone (a new PC, a lost database, another library), and
  * hand the same information to other programs (Explorer sees the tags; tools can read the JSON).

The tags themselves are always written as ordinary tags too (Keywords and Explorer's Tags).
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Callable

from . import explorer
from .media.probe import MP4_EXTENSIONS
from .tagdb import Person, TagDB, TagDBError
from .tagsync import merge_unique, read_state, route_fields, save_mp4

log = logging.getLogger(__name__)

FORMAT = "runemark-data"
VERSION = 1

Progress = Callable[[int, str], None]


# ------------------------------------------------------------------ the data block (pure functions)
def person_entry(db: TagDB, person: Person, skip: list[str] | tuple = ()) -> dict:
    """A person as recorded in a file: everything needed to work out their tags without the database.
    skip: uids of this person's values that don't apply to this video."""
    attrs = {a.id: a for a in db.attributes()}
    values = []
    for v in person.values:
        attr = attrs.get(v.attribute_id)
        if attr is None:
            continue
        values.append({"id": v.uid, "attribute": {"id": attr.uid, "name": attr.name, "stash_field": attr.stash_field},
                       "value": v.value, "tag": v.tag, "as_tag": attr.write_tags,
                       "from_year": v.from_year, "to_year": v.to_year})
    known = {v["id"] for v in values}
    return {"id": person.uid, "name": person.name, "aliases": list(person.aliases),
            "aliases_as_tags": person.write_aliases, "values": values,
            "skip": [s for s in skip if s in known]}


def value_applies(value: dict, year: int | None) -> bool:
    lo, hi = value.get("from_year"), value.get("to_year")
    if lo is None and hi is None:
        return True
    if year is None:
        return False          # a value limited to some years needs the video's year to apply
    return (lo is None or year >= lo) and (hi is None or year <= hi)


def entry_tags(entry: dict, year: int | None) -> list[str]:
    tags = [entry["name"]]
    if entry.get("aliases_as_tags"):
        tags += entry.get("aliases", [])
    skip = set(entry.get("skip", []))
    for value in entry.get("values", []):
        if value.get("as_tag", True) and value["id"] not in skip and value_applies(value, year):
            tags.append(value["tag"])
    return merge_unique(tags)


def build_block(entries: list[dict], year: int | None) -> dict | None:
    """The data block for a file with these people (None when there are none: no block is written)."""
    if not entries:
        return None
    tags: list[str] = []
    for entry in entries:
        tags = merge_unique(tags, entry_tags(entry, year))
    return {"format": FORMAT, "version": VERSION, "people": entries, "profile_tags": tags}


def entries_of(data: dict | None) -> list[dict]:
    if not data or data.get("format") != FORMAT:
        return []
    return [e for e in data.get("people", []) if isinstance(e, dict) and e.get("id") and e.get("name")]


def profile_tags_of(data: dict | None) -> list[str]:
    if not data or data.get("format") != FORMAT:
        return []
    return list(data.get("profile_tags", []))


def hand_tags(keywords: list[str], data: dict | None) -> list[str]:
    """The tags that weren't added by a profile, including ones typed by hand that a profile also gives
    (the block lists those under "also_hand", so removing the person doesn't take them away)."""
    also = {t.casefold() for t in (data or {}).get("also_hand", [])} if profile_tags_of(data) else set()
    added = {t.casefold() for t in profile_tags_of(data)} - also
    return [k for k in keywords if k.casefold() not in added]


def combine(keywords: list[str], old: dict | None, new: dict | None) -> list[str]:
    """A file's tags after its people change: its hand-typed tags, then the tags from its people now.
    Profile tags are worked out again from everyone still in the video, so a tag two people share
    (both have "Red hair") stays when one of them is removed. Updates `new` with "also_hand"."""
    hand = hand_tags(keywords, old)
    if new is not None:
        given = {t.casefold() for t in profile_tags_of(new)}
        overlap = [t for t in hand if t.casefold() in given]
        if overlap:
            new["also_hand"] = overlap
        else:
            new.pop("also_hand", None)
    return merge_unique(hand, profile_tags_of(new))


# ------------------------------------------------------------------ reading people back from files
def resolve_people(db: TagDB, data: dict | None, *, create: bool = True) -> list[int]:
    """Person ids for the people recorded in a file. People the database doesn't know (another library, a lost
    database) are added from the file: matched first by permanent id, then by name or alias, else created with
    the aliases and values the file recorded. Known people keep their profile here: files never overwrite it."""
    ids: list[int] = []
    for entry in entries_of(data):
        person = db.person_by_uid(entry["id"]) or _match_by_name(db, entry)
        if person is None and create:
            person = _import_person(db, entry)
        if person is not None and person.id not in ids:
            ids.append(person.id)
    return ids


def _match_by_name(db: TagDB, entry: dict) -> Person | None:
    for name in [entry["name"], *entry.get("aliases", [])]:
        person = db.find_person(name)
        if person is not None:
            return person
    return None


def _import_person(db: TagDB, entry: dict) -> Person | None:
    free = []
    for alias in entry.get("aliases", []):
        if db.find_person(alias) is None:
            free.append(alias)
    try:
        person = db.add_person(entry["name"], free, write_aliases=bool(entry.get("aliases_as_tags")),
                               uid=entry["id"])
    except (TagDBError, ValueError) as exc:
        log.info("couldn't add %r from a file: %s", entry.get("name"), exc)
        return None
    for value in entry.get("values", []):
        a = value.get("attribute") or {}
        try:
            attr = (db.attribute_by_uid(a.get("id", "")) or db.find_attribute(a.get("name", ""))
                    or db.add_attribute(a["name"], stash_field=a.get("stash_field"),
                                        write_tags=bool(value.get("as_tag", True)), uid=a.get("id")))
            db.add_person_value(person.id, attr.id, value["value"], tag=value.get("tag"),
                                from_year=value.get("from_year"), to_year=value.get("to_year"),
                                uid=value.get("id") if not _value_uid_taken(db, value.get("id")) else None)
        except (TagDBError, KeyError, ValueError) as exc:
            log.info("skipped a value for %s from a file: %s", person.name, exc)
    return db.person(person.id)


def _value_uid_taken(db: TagDB, uid: str | None) -> bool:
    if not uid:
        return True
    return db.conn.execute("SELECT 1 FROM person_values WHERE uid = ?", (uid,)).fetchone() is not None


# ------------------------------------------------------------------ changing files
def _fields_for(db: TagDB, tags: list[str], original_genre: list[str]) -> tuple[list[str], list[str]]:
    return route_fields(tags, is_genre=db.is_genre_tag, is_known=lambda n: db.find_tag(n) is not None,
                        original_genre=original_genre)


def block_for(db: TagDB, people: list[tuple[Person | dict, list[str]]], year: int | None) -> dict | None:
    """people: (Person from the database, or an entry kept as-is from the file; value uids to skip)."""
    entries = []
    for person, skip in people:
        if isinstance(person, dict):
            entry = dict(person)
            entry["skip"] = list(skip)
        else:
            entry = person_entry(db, person, skip)
        entries.append(entry)
    return build_block(entries, year)


def save_people(db: TagDB, path: str | os.PathLike, people: list[tuple[Person | dict, list[str]]], *,
                label: str, batch: str | None = None, explorer_mode: str = explorer.MODE_AUTO,
                preserve_mtime: bool = False, state=None) -> list[str]:
    """Set the people in one MP4 and rewrite its profile tags (hand-typed tags are kept).
    Returns problems that came up reading the file back; raises if it couldn't be saved.
    Nothing is written when the file already matches."""
    from .library import history_fields, reindex
    path = Path(path)
    if path.suffix.lower() not in MP4_EXTENSIONS:
        raise TagDBError("People can only be saved in MP4 files; open an MKV and save it to convert it first.")
    state = state or read_state(path)
    new = block_for(db, people, state.meta.year)
    current_tags = merge_unique(state.keywords, state.explorer or [])
    tags = [db.canonical(t) for t in combine(current_tags, state.data, new)]
    if (new or None) == (state.data or None) and [t.casefold() for t in tags] == \
            [t.casefold() for t in current_tags]:
        return []
    keywords, genre = _fields_for(db, tags, state.genre)
    outcome = save_mp4(path, keywords=keywords, genre=genre, explorer_mode=explorer_mode,
                       preserve_mtime=preserve_mtime, data=new)
    db.record_history(path, **history_fields(state), label=label, batch=batch)
    db.remember_file(path, keywords)
    reindex(db, path)
    return outcome.problems


def current_people(db: TagDB, data: dict | None) -> list[tuple[Person | dict, list[str]]]:
    """The people recorded in a file, each as the database's current profile (or the file's own copy when the
    database doesn't know them), with that file's skipped values."""
    out = []
    for entry in entries_of(data):
        person = db.person_by_uid(entry["id"]) or _match_by_name(db, entry)   # by name: made in another library
        out.append((person if person is not None else entry, list(entry.get("skip", []))))
    return out


def refresh_files(db: TagDB, paths: list[str | os.PathLike], *, label: str, batch: str | None = None,
                  drop: set[str] | None = None, explorer_mode: str = explorer.MODE_AUTO,
                  preserve_mtime: bool = False, progress: Progress | None = None,
                  cancel: threading.Event | None = None) -> list[tuple[Path, str]]:
    """Bring files up to date with their people's current profiles (after a profile changed).
    drop: person uids to take out of the files altogether. One undoable batch."""
    from .library import new_batch_id
    batch = batch or new_batch_id()
    problems: list[tuple[Path, str]] = []
    for n, raw in enumerate(paths):
        path = Path(raw)
        if cancel is not None and cancel.is_set():
            problems.append((path, "stopped before this file"))
            continue
        if progress:
            progress(int(n * 100 / max(1, len(paths))), f"Updating {path.name}…")
        try:
            if not path.exists():
                problems.append((path, "isn't there any more (moved or deleted?)"))
                continue
            state = read_state(path)
            people = [(p, s) for p, s in current_people(db, state.data)
                      if not drop or (p["id"] if isinstance(p, dict) else p.uid) not in drop]
            issues = save_people(db, path, people, label=label, batch=batch, explorer_mode=explorer_mode,
                                 preserve_mtime=preserve_mtime, state=state)
            if issues:
                problems.append((path, "; ".join(issues)))
        except Exception as exc:  # noqa: BLE001
            problems.append((path, str(exc)))
    return problems


def files_needing_update(db: TagDB, person_id: int) -> list[str]:
    """Files with this person (as the library knows them), for "update these N videos?"."""
    return db.files_with_person(person_id)

