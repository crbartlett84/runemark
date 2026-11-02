"""Reading details from .nfo files (the Kodi format; Jellyfin, Emby, Radarr, Sonarr and tinyMediaManager write it).

A .nfo beside a video, `Movie.nfo` for `Movie.mp4`, can hold its title, year, studio, description and the people in
it (as actors). Runemark reads those into empty fields: what's already filled in stays, unless asked to replace it.
`movie.nfo` is also used when it's the only video in its folder (Radarr's "use movie.nfo" option).
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .media.mp4meta import parse_year
from .media.probe import VIDEO_EXTENSIONS

log = logging.getLogger(__name__)

MAX_NFO_BYTES = 2 * 1024 * 1024
ROOTS = ("movie", "episodedetails", "musicvideo")


@dataclass
class NfoDetails:
    path: Path
    title: str = ""
    year: int | None = None
    studio: str = ""
    description: str = ""
    people: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.title or self.year or self.studio or self.description or self.people)


def find_nfo(video: str | Path) -> Path | None:
    video = Path(video)
    same = video.with_suffix(".nfo")
    if same.is_file():
        return same
    try:
        movie = next((p for p in video.parent.iterdir() if p.name.casefold() == "movie.nfo" and p.is_file()), None)
        if movie is not None:
            videos = [p for p in video.parent.iterdir() if p.suffix.lower() in VIDEO_EXTENSIONS
                      and ".runemark-" not in p.name]
            if len(videos) == 1:
                return movie
    except OSError:
        pass
    return None


def _text(el) -> str:
    return " ".join((el.text or "").split()) if el is not None else ""


def parse_nfo(path: str | Path) -> NfoDetails | None:
    """The details in a .nfo, or None when it isn't a readable Kodi .nfo (some are just a link to a web page)."""
    path = Path(path)
    try:
        if path.stat().st_size > MAX_NFO_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    text = raw.decode("utf-8-sig", "replace")
    # Kodi allows a web address after the XML; keep only the XML itself
    m = re.search(r"<(%s)\b.*</\1\s*>" % "|".join(ROOTS), text, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    try:
        root = ET.fromstring(m.group(0))
    except ET.ParseError as exc:
        log.info("couldn't read %s: %s", path, exc)
        return None
    details = NfoDetails(path)
    details.title = _text(root.find("title"))
    for key in ("year", "premiered", "releasedate", "aired"):
        details.year = parse_year(_text(root.find(key)))
        if details.year:
            break
    details.studio = _text(root.find("studio"))
    # keep paragraph breaks in the description
    plot = root.findtext("plot") or root.findtext("outline") or ""
    details.description = "\n\n".join(" ".join(p.split()) for p in re.split(r"\n\s*\n", plot.strip()) if p.strip())
    seen = set()
    for actor in root.findall("actor"):
        name = _text(actor.find("name"))
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            details.people.append(name)
    return details


def read_for(video: str | Path) -> NfoDetails | None:
    nfo = find_nfo(video)
    if nfo is None:
        return None
    details = parse_nfo(nfo)
    return details if details is not None and not details.empty else None


def plan_nfo(paths: list[str]):
    """One ReadPlan per video (as for file names), filled from its .nfo; matched=False when there's none."""
    from .names import ReadPlan
    plans = []
    for p in paths:
        plan = ReadPlan(p)
        details = read_for(p)
        if details is not None:
            plan.matched = True
            plan.title = details.title or None
            plan.year = details.year
            plan.studio = details.studio or None
            plan.description = details.description or None
            plan.people = list(details.people)
            plan.source = details.path.name
        plans.append(plan)
    return plans
