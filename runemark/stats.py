"""Library stats: how much is done, what's used most, and how the inbox is going."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .tagdb import FileRecord


@dataclass
class Stats:
    total: int = 0
    tagged: int = 0
    with_people: int = 0
    with_cover: int = 0
    with_chapters: int = 0
    rated: int = 0
    with_studio: int = 0
    inbox: int = 0
    missing: int = 0
    hours: float = 0.0
    top_tags: list = field(default_factory=list)
    top_people: list = field(default_factory=list)
    top_studios: list = field(default_factory=list)
    ratings: dict = field(default_factory=dict)
    added_by_month: list = field(default_factory=list)       # [(YYYY-MM, videos first seen that month)]


def compute(records: list[FileRecord], top: int = 10, months: int = 12) -> Stats:
    s = Stats()
    tags, people, studios, months_seen = Counter(), Counter(), Counter(), Counter()
    for r in records:
        if r.missing:
            s.missing += 1
            continue
        s.total += 1
        s.tagged += bool(r.tags)
        s.with_people += bool(r.people)
        s.with_cover += bool(r.has_cover)
        s.with_chapters += bool(r.chapters)
        s.rated += bool(r.rating)
        s.with_studio += bool(r.studio)
        s.inbox += r.in_inbox
        s.hours += (r.duration or 0) / 3600
        person_names = {p.casefold() for p in r.people}
        tags.update(t for t in r.tags if t.casefold() not in person_names)
        people.update(r.people)
        if r.studio:
            studios[r.studio] += 1
        s.ratings[r.rating] = s.ratings.get(r.rating, 0) + 1
        if r.first_seen:
            months_seen[r.first_seen[:7]] += 1
    s.top_tags = tags.most_common(top)
    s.top_people = people.most_common(top)
    s.top_studios = studios.most_common(top)
    s.added_by_month = sorted(months_seen.items())[-months:]
    return s
