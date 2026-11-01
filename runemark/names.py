"""File names in both directions.

* **Read details from names:** a pattern such as `{People} - {Title} ({Year})` fills in title, year and people
  from file names that already follow a standard.
* **Rename from details:** the same kind of pattern builds new names (and optionally folders, with `\\`) from
  what Runemark knows about each video.

Both are planned first and shown as a preview; nothing changes until the plan is applied, and renames can be undone.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

FIELDS = ("Title", "Year", "People", "Rating", "Name")     # Name = the current file name (without extension)
PEOPLE_SEPARATORS = r"\s*(?:,|&|;|\band\b)\s*"
WINDOWS_ILLEGAL = '<>:"/\\|?*'
# the usual stand-ins for characters Windows doesn't allow in names
ILLEGAL_MAP = str.maketrans({":": " -", '"': "'", "/": "-", "\\": "-", "|": "-", "?": "", "*": "", "<": "", ">": ""})
MAX_NAME = 180          # characters for the file name itself; Windows' path limit is 260 without long paths
SIDECAR_EXTENSIONS = (".nfo", ".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx")


class PatternError(ValueError):
    pass


def fields_in(pattern: str) -> list[str]:
    found = re.findall(r"\{(\w+)\}", pattern)
    unknown = [f for f in found if f not in FIELDS]
    if unknown:
        raise PatternError(f"Unknown field {{{unknown[0]}}}. Use: " + ", ".join("{" + f + "}" for f in FIELDS))
    return found


# ------------------------------------------------------------------ reading details from names
def name_regex(pattern: str) -> re.Pattern:
    """{Title} - {Year} -> a regex with a group per field; the text between fields must match literally
    (spaces are flexible)."""
    fields = fields_in(pattern)
    if not fields:
        raise PatternError("The pattern needs at least one field, such as {Title}.")
    if len(set(fields)) != len(fields):
        raise PatternError("Each field can appear only once.")
    out, pos = "^", 0
    for m in re.finditer(r"\{(\w+)\}", pattern):
        literal = pattern[pos:m.start()]
        out += r"\s*".join(re.escape(part) for part in literal.split(" ")) if literal.strip() else (
            r"\s*" if literal else "")
        name = m.group(1)
        out += {"Year": r"(?P<Year>(?:1[89]|2\d)\d\d)", "Rating": r"(?P<Rating>[0-5])"}.get(name, f"(?P<{name}>.+?)")
        pos = m.end()
    tail = pattern[pos:]
    out += (r"\s*".join(re.escape(part) for part in tail.split(" ")) if tail.strip() else r"\s*") + "$"
    return re.compile(out, re.IGNORECASE)


@dataclass
class ReadPlan:
    path: str
    title: str | None = None
    year: int | None = None
    people: list[str] = field(default_factory=list)
    rating: int | None = None
    matched: bool = False


def plan_read(paths: list[str], pattern: str) -> list[ReadPlan]:
    regex = name_regex(pattern)
    out = []
    for p in paths:
        plan = ReadPlan(p)
        m = regex.match(Path(p).stem)
        if m:
            plan.matched = True
            groups = m.groupdict()
            if groups.get("Title"):
                plan.title = " ".join(groups["Title"].split())
            if groups.get("Year"):
                plan.year = int(groups["Year"])
            if groups.get("Rating"):
                plan.rating = int(groups["Rating"])
            if groups.get("People"):
                plan.people = [" ".join(x.split()) for x in re.split(PEOPLE_SEPARATORS, groups["People"]) if x.strip()]
        out.append(plan)
    return out


# ------------------------------------------------------------------ renaming from details
def clean_part(text: str) -> str:
    """Make text safe as (part of) a Windows file name."""
    text = text.translate(ILLEGAL_MAP)
    text = re.sub(r"[\x00-\x1f]", "", text)
    text = " ".join(text.split())
    return text.strip(" .")


def build_name(pattern: str, values: dict[str, str]) -> str:
    """Fill the pattern. Empty fields leave no stray brackets or separators behind:
    '{Title} ({Year})' with no year gives 'Title', not 'Title ()'."""
    fields_in(pattern)
    parts = re.split(r"(\\|/)", pattern)            # folders: each part is cleaned on its own
    out = []
    for part in parts:
        if part in ("\\", "/"):
            out.append(os.sep if os.sep == "\\" else "/")
            continue
        text = re.sub(r"\{(\w+)\}", lambda m: clean_part(values.get(m.group(1), "") or "").replace("\x00", ""), part)
        text = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", "", text)          # brackets left empty
        text = re.sub(r"\s*([-–_,·])\s*(?=\1|$)", "", text)           # a separator with nothing after it
        text = re.sub(r"^\s*[-–_,·]\s*", "", text)                    # ... or before it
        out.append(clean_part(text))
    joined = "".join(out)
    joined = re.sub(r"(^|[\\/])[\\/]+", r"\1", joined).strip("\\/")  # empty folder levels
    return joined


@dataclass
class RenamePlan:
    old: str
    new: str
    problem: str = ""            # why it won't be renamed ("" = fine)

    @property
    def changes(self) -> bool:
        # exact comparison: on Windows "beach day" -> "Beach Day" is a real change (only the letter case)
        return not self.problem and self.old != self.new


def values_for(record) -> dict[str, str]:
    return {"Title": record.title or "", "Year": str(record.year or ""), "People": ", ".join(record.people),
            "Rating": str(record.rating or ""), "Name": Path(record.path).stem}


def plan_renames(records, pattern: str, *, base: str | None = None) -> list[RenamePlan]:
    """New paths for these videos. `base`: the folder new names (and any folders in the pattern) go under;
    by default each video stays in its own folder. Clashes and unusable names are flagged, not renamed."""
    plans = []
    needs_title = "Title" in fields_in(pattern)
    for r in records:
        old = Path(r.path)
        if needs_title and not (r.title or "").strip():
            plans.append(RenamePlan(r.path, r.path, "no title yet (renaming it would lose its name)"))
            continue
        stem = build_name(pattern, values_for(r))
        if not stem.replace(os.sep, "").strip():
            plans.append(RenamePlan(r.path, r.path, "the pattern gives an empty name (fill in its details first)"))
            continue
        leaf = Path(stem).name
        if len(leaf) > MAX_NAME:
            leaf = leaf[:MAX_NAME].rstrip(" .-")
            stem = str(Path(stem).parent / leaf) if Path(stem).parent != Path(".") else leaf
        new = Path(base or old.parent) / (stem + old.suffix.lower() if old.suffix.isupper() else stem + old.suffix)
        plans.append(RenamePlan(r.path, str(new)))
    seen: dict[str, RenamePlan] = {}
    for p in plans:
        if p.problem:
            continue
        key = os.path.normcase(p.new)
        if key in seen:
            p.problem = f"same new name as {Path(seen[key].old).name}"
            continue
        seen[key] = p
        if os.path.normcase(p.old) != key and os.path.exists(p.new):
            p.problem = "a file with that name is already there"
        elif len(p.new) >= 259:
            p.problem = "the full path would be too long for Windows"
    return plans


def sidecars(path: str | Path) -> list[Path]:
    """Files that belong with a video and should move with it (same name: .nfo, subtitles)."""
    path = Path(path)
    out = []
    try:
        for f in path.parent.iterdir():
            if f == path or not f.is_file():
                continue
            if f.stem.casefold() == path.stem.casefold() and f.suffix.lower() in SIDECAR_EXTENSIONS:
                out.append(f)
            elif f.name.casefold().startswith(path.stem.casefold() + ".") and f.suffix.lower() in SIDECAR_EXTENSIONS:
                out.append(f)       # name.en.srt
    except OSError:
        pass
    return out


def apply_renames(db, plans: list[RenamePlan], *, progress=None, cancel=None) -> tuple[str, list[tuple[str, str]]]:
    """Rename (and move, within the same drive) every planned video and its sidecar files, and keep the library,
    undo history, people and fingerprints with them. Returns (batch id for undo, problems)."""
    from .library import new_batch_id
    batch = new_batch_id()
    problems: list[tuple[str, str]] = []
    todo = [p for p in plans if p.changes]
    for n, plan in enumerate(todo):
        if cancel is not None and cancel.is_set():
            problems.append((plan.old, "stopped before this file"))
            continue
        if progress:
            progress(int(n * 100 / max(1, len(todo))), f"Renaming {Path(plan.old).name}…")
        try:
            _move(plan.old, plan.new)
        except OSError as exc:
            problems.append((plan.old, str(exc)))
            continue
        moved = [(str(s), str(Path(plan.new).with_name(Path(plan.new).stem + s.name[len(Path(plan.old).stem):])))
                 for s in sidecars(plan.old)] if os.path.exists(plan.new) else []
        for s_old, s_new in moved:
            try:
                if not os.path.exists(s_new):
                    _move(s_old, s_new)
            except OSError as exc:
                problems.append((s_old, f"left behind: {exc}"))
        db.move_file(plan.old, plan.new)
        db.record_rename(batch, plan.old, plan.new)
    return batch, problems


def _move(old: str, new: str) -> None:
    Path(new).parent.mkdir(parents=True, exist_ok=True)
    if os.path.normcase(old) == os.path.normcase(new) and old != new:
        tmp = old + ".runemark-rename"          # a change of letter case only: Windows needs a step in between
        os.rename(old, tmp)
        os.rename(tmp, new)
    else:
        os.rename(old, new)                     # same drive only: a different drive raises, and nothing moves


def undo_renames(db, batch: str | None = None) -> list[tuple[str, str]]:
    """Put the files of the last rename (or a given batch) back where they were."""
    rows = db.renames(batch)
    problems = []
    for old, new in reversed(rows):
        if not os.path.exists(new):
            problems.append((new, "isn't there any more"))
            continue
        if os.path.exists(old) and os.path.normcase(old) != os.path.normcase(new):
            problems.append((new, "a file with the old name is back in the way"))
            continue
        try:
            moved = [(str(s), str(Path(old).with_name(Path(old).stem + s.name[len(Path(new).stem):])))
                     for s in sidecars(new)]
            _move(new, old)
            for s_new, s_old in moved:
                if not os.path.exists(s_old):
                    _move(s_new, s_old)
            db.move_file(new, old)
            try:
                Path(new).parent.rmdir()        # a folder the rename created, now empty
            except OSError:
                pass
        except OSError as exc:
            problems.append((new, str(exc)))
    if rows:
        db.drop_renames(batch or db.last_rename_batch())
    return problems


def apply_read(db, plans: list[ReadPlan], *, overwrite: bool = False, create_people: bool = True,
               progress=None, cancel=None) -> list[tuple[str, str]]:
    """Write what was read from the names into the files: title, year and rating fill blanks (or replace them,
    with overwrite), and people are added (matched by name or alias; new ones created when create_people).
    One undoable batch. Returns problems."""
    from . import people as P
    from .library import new_batch_id
    from .media.mp4meta import VideoMeta
    from .tagsync import read_state
    batch = new_batch_id()
    problems = []
    todo = [p for p in plans if p.matched]
    for n, plan in enumerate(todo):
        if cancel is not None and cancel.is_set():
            problems.append((plan.path, "stopped before this file"))
            continue
        if progress:
            progress(int(n * 100 / max(1, len(todo))), f"Updating {Path(plan.path).name}…")
        if Path(plan.path).suffix.lower() not in (".mp4", ".m4v"):
            problems.append((plan.path, "MKV: open it and save it to convert it to MP4 first"))
            continue
        try:
            state = read_state(plan.path)
            meta = VideoMeta(**vars(state.meta))
            if plan.title and (overwrite or not meta.title):
                meta.title = plan.title
            if plan.year and (overwrite or not meta.year):
                meta.year = plan.year
            if plan.rating is not None and (overwrite or not meta.rating):
                meta.rating = plan.rating
            people = P.current_people(db, state.data)
            have = {(p["id"] if isinstance(p, dict) else p.uid) for p, _ in people}
            for name in plan.people:
                person = db.find_person(name)
                if person is None and create_people:
                    person = db.add_person(name)
                if person is not None and person.uid not in have:
                    have.add(person.uid)
                    people.append((person, []))
            issues = _save_with_meta(db, plan.path, state, people, meta, batch)
            if issues:
                problems.append((plan.path, "; ".join(issues)))
        except Exception as exc:  # noqa: BLE001
            problems.append((plan.path, str(exc)))
    return problems


def _save_with_meta(db, path, state, people, meta, batch) -> list[str]:
    from . import explorer
    from . import people as P
    from .library import history_fields, reindex
    from .tagsync import merge_unique, route_fields, save_mp4
    block = P.block_for(db, people, meta.year)
    current = merge_unique(state.keywords, state.explorer or [])
    tags = [db.canonical(t) for t in P.combine(current, state.data, block)]
    if (block or None) == (state.data or None) and meta.key() == state.meta.key() and \
            [t.casefold() for t in tags] == [t.casefold() for t in current]:
        return []
    keywords, genre = route_fields(tags, is_genre=db.is_genre_tag, is_known=lambda n: db.find_tag(n) is not None,
                                   original_genre=state.genre)
    outcome = save_mp4(path, keywords=keywords, genre=genre, meta=meta, data=block, explorer_mode=explorer.MODE_AUTO)
    db.record_history(path, **history_fields(state), label="Details read from file names", batch=batch)
    db.remember_file(path, keywords)
    reindex(db, path)
    return outcome.problems
