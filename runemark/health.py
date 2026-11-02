"""The health check: things in the library that have drifted or been left half done, each with a fix where there is
one. Checking only reads files; fixes are ordinary saves (undoable as one bulk change)."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .media.probe import MP4_EXTENSIONS
from .tagdb import FileRecord, TagDB

OUT_OF_SYNC = "sync"          # Explorer's Tags and the file's own tags differ
PEOPLE_STALE = "people"       # a person's profile changed since the file was saved
NFO_UNUSED = "nfo"            # a .nfo beside it has details the video doesn't
NO_COVER = "cover"            # no thumbnail (nothing to fix automatically)
MISSING = "missing"           # the file has gone

KIND_TEXT = {
    OUT_OF_SYNC: "Explorer's Tags and the file's own tags differ",
    PEOPLE_STALE: "People's tags are out of date",
    NFO_UNUSED: "Its .nfo has details the video doesn't",
    NO_COVER: "No thumbnail",
    MISSING: "Moved or deleted",
}
FIX_TEXT = {
    OUT_OF_SYNC: "Combine both sides and save",
    PEOPLE_STALE: "Update the people's tags",
    NFO_UNUSED: "Fill in the empty details from the .nfo",
    MISSING: "Remove from the library",
}


@dataclass
class Issue:
    path: str
    kind: str
    detail: str = ""

    @property
    def fixable(self) -> bool:
        return self.kind in FIX_TEXT


def check(db: TagDB, records: list[FileRecord], *, progress: Callable[[int, str], None] | None = None,
          cancel: threading.Event | None = None) -> list[Issue]:
    from . import nfo
    from . import people as P
    from .tagsync import read_state
    issues: list[Issue] = []
    for n, rec in enumerate(records):
        if cancel is not None and cancel.is_set():
            break
        if progress and n % 5 == 0:
            progress(int(n * 100 / max(1, len(records))), f"Checking {Path(rec.path).name}…")
        if rec.missing:
            issues.append(Issue(rec.path, MISSING))
            continue
        if not rec.has_cover:
            issues.append(Issue(rec.path, NO_COVER))
        if Path(rec.path).suffix.lower() not in MP4_EXTENSIONS:
            continue
        try:
            state = read_state(rec.path)
        except Exception as exc:  # noqa: BLE001
            issues.append(Issue(rec.path, MISSING, f"couldn't be read: {exc}"))
            continue
        report = state.sync()
        if not report.in_sync:
            bits = []
            if report.only_in_keywords:
                bits.append("only in the file: " + ", ".join(report.only_in_keywords))
            if report.only_in_explorer:
                bits.append("only in Explorer: " + ", ".join(report.only_in_explorer))
            issues.append(Issue(rec.path, OUT_OF_SYNC, "; ".join(bits)))
        if state.data:
            people = P.current_people(db, state.data)
            fresh = P.block_for(db, people, state.meta.year)
            had = {t.casefold(): t for t in P.profile_tags_of(state.data)}
            now = {t.casefold(): t for t in P.profile_tags_of(fresh)}
            if had.keys() != now.keys():
                gained = sorted(now[k] for k in now.keys() - had.keys())
                lost = sorted(had[k] for k in had.keys() - now.keys())
                detail = "; ".join(x for x in ((f"adds {', '.join(gained)}" if gained else ""),
                                               (f"drops {', '.join(lost)}" if lost else "")) if x)
                issues.append(Issue(rec.path, PEOPLE_STALE, detail))
        details = nfo.read_for(rec.path)
        if details is not None:
            missing = []
            if details.title and not state.meta.title:
                missing.append("title")
            if details.year and not state.meta.year:
                missing.append("year")
            if details.studio and not state.meta.studio:
                missing.append("studio")
            if details.description and not state.meta.description:
                missing.append("description")
            have = {(p["id"] if isinstance(p, dict) else p.uid) for p, _ in P.current_people(db, state.data)}
            for name in details.people:
                person = db.find_person(name)
                if person is None or person.uid not in have:
                    missing.append("people")
                    break
            if missing:
                issues.append(Issue(rec.path, NFO_UNUSED, ", ".join(missing)))
    return issues


def fix(db: TagDB, issues: list[Issue], *, explorer_mode=None, preserve_mtime: bool = False,
        progress: Callable[[int, str], None] | None = None,
        cancel: threading.Event | None = None) -> list[tuple[Path, str]]:
    """Fix what can be fixed. Returns (file, problem) for anything that didn't work."""
    from . import explorer
    from . import names as N
    from . import nfo
    from . import people as P
    from .library import history_fields, new_batch_id, reindex
    from .tagsync import merge_unique, read_state, route_fields, save_mp4
    mode = explorer_mode or explorer.MODE_AUTO
    batch = new_batch_id()
    problems: list[tuple[Path, str]] = []
    by_kind: dict[str, list[str]] = {}
    for i in issues:
        if i.fixable:
            by_kind.setdefault(i.kind, []).append(i.path)
    total = sum(len(v) for v in by_kind.values())
    done = 0

    def step(path):
        nonlocal done
        done += 1
        if progress:
            progress(int(done * 100 / max(1, total)), f"Fixing {Path(path).name}…")

    for path in by_kind.get(MISSING, []):
        step(path)
        db.forget_file(path)
    for path in by_kind.get(OUT_OF_SYNC, []):
        if cancel is not None and cancel.is_set():
            break
        step(path)
        try:
            state = read_state(path)
            tags = [db.canonical(t) for t in merge_unique(state.keywords, state.explorer or [])]
            keywords, genre = route_fields(tags, is_genre=db.is_genre_tag,
                                           is_known=lambda n: db.find_tag(n) is not None, original_genre=state.genre)
            outcome = save_mp4(path, keywords=keywords, genre=genre, explorer_mode=mode, preserve_mtime=preserve_mtime)
            db.record_history(path, **history_fields(state), label="Health check: tags put back in sync", batch=batch)
            db.remember_file(path, keywords)
            reindex(db, path)
            if outcome.problems:
                problems.append((Path(path), "; ".join(outcome.problems)))
        except Exception as exc:  # noqa: BLE001
            problems.append((Path(path), str(exc)))
    if by_kind.get(PEOPLE_STALE) and not (cancel is not None and cancel.is_set()):
        paths = by_kind[PEOPLE_STALE]
        for p in paths:
            step(p)
        problems += P.refresh_files(db, paths, label="Health check: people's tags updated", batch=batch,
                                    explorer_mode=mode, preserve_mtime=preserve_mtime, cancel=cancel)
    if by_kind.get(NFO_UNUSED) and not (cancel is not None and cancel.is_set()):
        paths = by_kind[NFO_UNUSED]
        for p in paths:
            step(p)
        problems += [(Path(p), why) for p, why in
                     N.apply_read(db, nfo.plan_nfo(paths), label="Health check: details from .nfo", cancel=cancel)]
    return problems
