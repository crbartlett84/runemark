"""Changing a video with no window (0.15.0).

What a plugin's change_file does in the app (open the video out of sight, change its details, tags, people and
thumbnail, and save it), as plain functions with no Qt, so the same change can be worked out anywhere Runemark's
file code runs: a service beside the files, a script. The app itself uses apply_changes(), so both give the same
result.

    edit = open_edit(path, db)                       # read the video (probe, tags, people, cover)
    done = apply_changes(edit, db, {"title": ..., "add_tags": [...], "people": [...]})
    request = edit.request(db)                       # a saving.SaveRequest, or None when nothing changed
    saving.write_copy(request, path, other_file)     # or hand request.to_dict() to whatever saves it

Nothing here writes a video. The database is used as given: add_person() and ensure_tag() add to it, so pass a
working copy when the real one mustn't change.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Callable

from . import people as people_io
from . import saving
from .media import chapters as chapter_io
from .media import frames
from .media.mp4meta import UNCHANGED, VideoMeta, clean_links, parse_date
from .media.probe import MediaInfo, probe
from .tagdb import TagDB
from .tagsync import FileState, merge_unique, read_state, route_fields, same_tags

log = logging.getLogger(__name__)

PHOTO_SIZE = 240          # a person's photo, pixels square (as People saves it: ui/photo_crop.py)
PHOTO_QUALITY = 85


def people_key(people) -> list[tuple[str, tuple]]:
    return [((p["id"] if isinstance(p, dict) else p.uid), tuple(sorted(skip))) for p, skip in people]


class Edit:
    """A video read from disk and the changes waiting to be saved (the editor's Document without the window)."""

    def __init__(self, path: Path, info: MediaInfo, state: FileState, db: TagDB, stamp=None):
        self.path = Path(path)
        self.info = info
        self.state = state
        self.tags = people_io.hand_tags([db.canonical(t) for t in state.all_tags()], state.data)
        self.saved_tags = list(self.tags)
        self.people = people_io.current_people(db, state.data)
        self.saved_people = list(self.people)
        self.cover = UNCHANGED
        self.cover_note = ""
        self.meta = VideoMeta(**vars(state.meta))
        self.saved_meta = VideoMeta(**vars(state.meta))
        self.chapters = chapter_io.apply_ends(chapter_io.marks_from_info(info), state.data)
        self.stamp = stamp

    @property
    def container(self) -> str:
        return self.state.container

    def whole_cover(self) -> bytes | None:
        if self.cover is UNCHANGED:
            return self.state.cover
        return self.cover or None

    def meta_changed(self) -> bool:
        return self.meta.key() != self.saved_meta.key()

    def people_changed(self) -> bool:
        return people_key(self.people) != people_key(self.saved_people)

    def dirty(self) -> bool:
        return (not same_tags(self.tags, self.saved_tags)) or self.cover is not UNCHANGED \
            or self.meta_changed() or self.people_changed()

    def compose(self, db: TagDB) -> tuple[list[str], dict | None]:
        """The file's full tag list (hand-typed + from its people) and its data block, as they'd be saved."""
        block = people_io.carry(people_io.block_for(db, self.people, self.meta.year), self.state.data,
                                drop=(chapter_io.ENDS_KEY,))
        ends = chapter_io.ends_block(self.chapters)
        if ends:
            block = block or {"format": people_io.FORMAT, "version": people_io.VERSION}
            block[chapter_io.ENDS_KEY] = ends
        return people_io.combine(self.tags, None, block), block

    def request(self, db: TagDB, preserve_mtime: bool = False) -> saving.SaveRequest | None:
        """The save of these changes (chapters are left alone), or None when there's nothing to save or it isn't
        an MP4 (an MKV is converted first)."""
        if not self.dirty() or self.container != "mp4":
            return None
        all_tags, block = self.compose(db)
        keywords, genre = route_fields(all_tags, is_genre=db.is_genre_tag,
                                       is_known=lambda n: db.find_tag(n) is not None,
                                       original_genre=self.state.genre)
        meta = self.meta if self.meta_changed() else UNCHANGED
        stamp = tuple(self.stamp or (None, None)) + (None,)
        return saving.SaveRequest(self.path, keywords, genre, self.cover, meta, block, None, preserve_mtime,
                                  stamp[0], stamp[1], expected_fp=stamp[2])


def open_edit(path, db: TagDB) -> Edit:
    """Read a video for changing: its stamp first (size, modified time, fingerprint), so a save can tell whether
    the file changed in between."""
    path = Path(path)
    stamp = saving.open_stamp(path)
    info = probe(path)
    sources = info.cover_sources
    state = read_state(path, info, lambda i: frames.dump_attachment(i.path, sources[0]) if sources else None)
    return Edit(path, info, state, db, stamp)


def apply_changes(doc, db: TagDB, changes: dict, *, person_ok: Callable[[str], bool | None] | None = None,
                  max_edge: int | None = 1280, quality: int = 90) -> list[str]:
    """Changes to a document (the editor's, or an Edit): details, links, tags, people, a thumbnail. Unsaved.
    changes: title, studio, description, date or year, rating, links (replace) or add_links, add_tags, people
    (names: added to the database when missing, unless person_ok(name) is False), cover (bytes) or cover_if_missing.
    Returns what changed."""
    done: list[str] = []
    meta = VideoMeta(**vars(doc.meta))
    for key in ("title", "studio", "description"):
        if key in changes and changes[key] is not None:
            setattr(meta, key, " ".join(str(changes[key]).split()) if key != "description"
                    else str(changes[key]).strip())
            done.append(key)
    if changes.get("date") and parse_date(changes["date"]):
        meta.date = parse_date(changes["date"])
        meta.year = int(meta.date[:4])
        done.append("date")
    elif changes.get("year"):
        meta.year = int(changes["year"])
        done.append("year")
    if changes.get("rating") is not None:
        meta.rating = max(0, min(5, int(changes["rating"])))
        done.append("rating")
    if changes.get("links") is not None:
        meta.links = clean_links(changes["links"])
        done.append("links")
    if changes.get("add_links"):
        meta.links = clean_links(list(meta.links or []) + list(changes["add_links"]))
        done.append("links")
    doc.meta = meta
    if changes.get("add_tags"):
        tags = merge_unique(doc.tags, [db.canonical(t) for t in changes["add_tags"]])
        if tags != doc.tags:
            doc.tags = tags
            done.append("tags")
    added = 0
    for name in changes.get("people") or []:
        person = db.find_person(name)
        if person is None and person_ok is not None and person_ok(name) is False:
            continue                           # 0.13.0: a plugin's check rules them out of People
        person = person or db.add_person(name)
        if not any((p["id"] if isinstance(p, dict) else p.uid) == person.uid for p, _s in doc.people):
            doc.people = doc.people + [(person, [])]
            added += 1
    if added:
        done.append("people")
    data = changes.get("cover")
    if not data and changes.get("cover_if_missing") and not doc.whole_cover():
        data = changes["cover_if_missing"]
    if data:
        doc.cover = data if data[:3] == b"\xff\xd8\xff" else frames.image_bytes_to_jpeg(
            data, max_edge=max_edge or None, quality=quality)
        doc.cover_note = "From a plugin"
        done.append("thumbnail")
    return done


def change(path, db: TagDB, changes: dict, *, person_ok=None, max_edge: int | None = 1280, quality: int = 90,
           preserve_mtime: bool = False) -> tuple[saving.SaveRequest | None, list[str], Edit]:
    """Read the video, apply the changes and return (the save request or None, what changed, the Edit)."""
    edit = open_edit(path, db)
    done = apply_changes(edit, db, changes, person_ok=person_ok, max_edge=max_edge, quality=quality)
    return edit.request(db, preserve_mtime), done, edit


# ------------------------------------------------------------------ the database, as plugins fill it
def square_photo(data: bytes) -> bytes | None:
    """A person's photo as People keeps it: the top-centre square of the picture, 240 px (heads are near the top)."""
    from PIL import Image
    try:
        image = frames.image_from_bytes(data)
    except Exception:  # noqa: BLE001  (not a picture)
        return None
    with image:
        side = min(image.size)
        left = (image.width - side) // 2
        square = image.crop((left, 0, left + side, side)).convert("RGB").resize(
            (PHOTO_SIZE, PHOTO_SIZE), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        square.save(out, "JPEG", quality=PHOTO_QUALITY)
        return out.getvalue()


def add_person(db: TagDB, name: str, aliases=(), links=(), photo: bytes | None = None,
               values: dict | None = None, uid: str | None = None) -> str:
    """Add someone to People, or fill in what an existing profile lacks (new aliases, links, a photo when they have
    none, attribute values they don't have yet), as a plugin's add_person does. values: {"Hair": "Red"} or
    {"Hair": ("Red", "Red Hair")}. uid: the permanent id a new person gets (so the same person made in two places
    is one person). Returns their main name."""
    person = db.find_person(name)
    if person is None:
        free = [a for a in aliases if db.find_person(a) is None and a.casefold() != name.casefold()]
        person = db.add_person(name, free, uid=uid) if uid else db.add_person(name, free)
    else:
        extra = [a for a in aliases if db.find_person(a) is None]
        if extra:
            db.update_person(person.id, aliases=list(person.aliases) + extra)
    if links:
        merged = list(person.links) + [x for x in links if x not in person.links]
        if merged != person.links:
            db.set_person_links(person.id, merged)
    if photo and db.person_photo(person.id) is None:
        jpeg = square_photo(photo)
        if jpeg:
            db.set_person_photo(person.id, jpeg)
    for attr_name, value in (values or {}).items():
        value, tag = (value if isinstance(value, (tuple, list)) else (value, None))
        if not value:
            continue
        attr = db.find_attribute(attr_name) or db.add_attribute(attr_name)
        if not any(v.attribute_id == attr.id for v in db.person(person.id).values):
            db.add_person_value(person.id, attr.id, str(value), tag=str(tag) if tag else None)
    return db.person(person.id).name


def ensure_category(db: TagDB, path: list[str], genre: bool | None = None):
    """Make sure a category path exists (["Acts", "Positions"]); genre=True makes the last one a genre category.
    Returns the last category."""
    parent, found = None, None
    for name in path:
        found = next((c for c in db.categories() if c.parent_id == parent and c.name.casefold() == name.casefold()),
                     None)
        if found is None:
            found = db.add_category(name, parent)
        parent = found.id
    if found is not None and genre is not None and bool(found.write_genre) != bool(genre):
        db.set_write_genre(found.id, bool(genre))
        found = db.category(found.id)
    return found


def ensure_tag(db: TagDB, name: str, category: list[str] | None = None, genre: bool | None = None) -> str:
    """The tag's name as the database spells it, added (in that category path) when it isn't there yet. A tag
    that exists keeps the category it has."""
    tag = db.find_tag(name)
    if tag is None:
        cat = ensure_category(db, list(category), genre) if category else None
        tag = db.add_tag(name, cat.id if cat else None)
    return tag.name
