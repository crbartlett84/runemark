"""Reading a pasted list of times ("0:00 Intro", "4:32 - Interview") into chapter suggestions (0.14.0)."""
from __future__ import annotations

import re

# 4:32 · 04:32 · 1:04:32 · 4:32.5 · 1:04:32,250 (not part of a longer number, so "12:30:45:10" isn't half-read)
_TIME = re.compile(r"(?<![\d:.])(?:(\d{1,3}):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?(?![\d:])")
_EDGES = " \t-–—:|·•>*#()[]{}\"'"
_NUMBERING = re.compile(r"^\s*(?:\d{1,3}[.)]|[-*•])\s+")


def parse_timestamps(text: str, duration: float | None = None) -> list[tuple[float, str]]:
    """[(seconds, name)] from a list with one time per line, in time order; name is "" when the line has none.

    The first time on a line is the chapter's start ("0:00 - 4:31 Intro" starts at 0:00). Lines without a time,
    times past `duration` and repeated times are left out.
    """
    found: dict[float, str] = {}
    for line in (text or "").splitlines():
        line = _NUMBERING.sub("", line, count=1)
        m = _TIME.search(line)
        if m is None:
            continue
        hours, minutes, seconds, frac = m.groups()
        if hours is None and int(minutes) > 59 or int(seconds) > 59 or hours is not None and int(minutes) > 59:
            continue
        at = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + (float("0." + frac) if frac else 0.0)
        if duration is not None and at > duration:
            continue
        rest = (line[:m.start()] + " " + line[m.end():])
        rest = _TIME.sub(" ", rest)                       # an end time on the same line isn't part of the name
        name = " ".join(rest.split()).strip(_EDGES).replace(";", ",")
        name = " ".join(name.split())
        at = round(at, 3)
        if at not in found or (name and not found[at]):
            found[at] = name
    return sorted(found.items())
