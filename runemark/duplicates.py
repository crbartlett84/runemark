"""Finding copies of videos from their fingerprints (media/fingerprint.py).

Two videos are compared by lining their frame hashes up, not by looking for equal files. Every frame of B is looked
up among A's frames; a close hash ("B at 0:15 looks like A at 2:15") is a vote for the time offset between them.
Real matches agree on an offset over a stretch of time; chance matches land at random offsets. So the result is a
list of **runs**: stretches where B follows A at a steady offset. From the runs:

  * a copy with the credits or opening trimmed is one run with gaps at the ends;
  * a copy with a scene cut out is two runs at different offsets, with the missing stretch between them;
  * scenes reordered or added show up as runs out of order, or gaps on B's side.

Candidates are found without comparing every pair: each hash is split into four 16-bit chunks, and two frames that
differ in only a few bits share at least one chunk exactly (the pigeonhole principle), so only videos sharing chunks
are compared properly.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .media.fingerprint import Fingerprint

MAX_BITS = 8            # two frames "look alike" when their 64-bit hashes differ in at most this many bits
MIN_RUN_SECONDS = 4.0   # a stretch must last this long to count (shorter agreement is chance)
MAX_GAP_SECONDS = 2.0   # gaps inside a run (flat frames, a missed frame) up to this long are bridged
OFFSET_SLACK = 1        # samples of drift allowed inside a run (timing jitter between encodes)

DUPLICATE = "duplicate"
CUT = "cut"             # one is a cut of the other: almost all of the shorter one is in the longer one
SHARED = "shared"       # they share a section


@dataclass
class Run:
    a_start: int        # sample index in A
    b_start: int        # sample index in B
    length: int         # samples

    @property
    def offset(self) -> int:
        return self.a_start - self.b_start


@dataclass
class Match:
    runs: list[Run]
    fps: float
    a_samples: int
    b_samples: int
    kind: str = ""
    coverage_a: float = 0.0   # share of A found in B
    coverage_b: float = 0.0
    a_gaps: list[tuple[float, float]] = field(default_factory=list)   # (start s, end s) of A missing from B
    b_gaps: list[tuple[float, float]] = field(default_factory=list)
    reordered: bool = False

    def seconds(self, samples: int) -> float:
        return samples / self.fps


def _chunks(h: int) -> tuple[int, int, int, int]:
    return (h >> 48) & 0xFFFF, (h >> 32) & 0xFFFF, (h >> 16) & 0xFFFF, h & 0xFFFF


class _Index:
    """Frame hashes of one video, findable by chunk."""

    def __init__(self, fp: Fingerprint):
        self.fp = fp
        self.by_chunk: list[dict[int, list[int]]] = [defaultdict(list) for _ in range(4)]
        for i, (h, flat) in enumerate(zip(fp.hashes, fp.flat)):
            if not flat:
                for k, c in enumerate(_chunks(h)):
                    self.by_chunk[k][c].append(i)

    def near(self, h: int) -> set[int]:
        found: set[int] = set()
        for k, c in enumerate(_chunks(h)):
            for j in self.by_chunk[k].get(c, ()):
                if j not in found and (self.fp.hashes[j] ^ h).bit_count() <= MAX_BITS:
                    found.add(j)
        return found


def compare(a: Fingerprint, b: Fingerprint, *, a_index: _Index | None = None) -> Match | None:
    """Line B up against A. None when they share nothing worth reporting."""
    if abs(a.fps - b.fps) > 1e-6:
        raise ValueError("fingerprints must use the same sampling rate")
    fps = a.fps
    min_run = max(2, round(MIN_RUN_SECONDS * fps))
    max_gap = max(1, round(MAX_GAP_SECONDS * fps))
    index = a_index or _Index(a)

    # votes: B sample -> the offsets (a - b) of A frames that look like it
    pairs: dict[int, list[int]] = {}
    offset_votes: dict[int, int] = defaultdict(int)
    for i, (h, flat) in enumerate(zip(b.hashes, b.flat)):
        if flat:
            continue
        offs = sorted({j - i for j in index.near(h)})
        if offs:
            pairs[i] = offs
            for d in offs:
                offset_votes[d] += 1
    if not pairs:
        return None

    # candidate offsets: enough votes nearby to make a run possible
    candidates = sorted((d for d in offset_votes
                         if sum(offset_votes.get(d + e, 0) for e in range(-OFFSET_SLACK, OFFSET_SLACK + 1))
                         >= min_run), key=lambda d: -offset_votes[d])

    runs: list[Run] = []
    for d in candidates:
        hits = sorted(i for i, offs in pairs.items() if any(abs(o - d) <= OFFSET_SLACK for o in offs))
        start = prev = None
        for i in hits + [None]:
            if i is not None and prev is not None and i - prev <= max_gap + 1:
                prev = i
                continue
            if start is not None and prev - start + 1 >= min_run:
                runs.append(Run(start + d, start, prev - start + 1))
            start = prev = i
    runs = _best_runs(runs)
    if not runs:
        return None
    return _describe(Match(runs, fps, len(a.hashes), len(b.hashes)), a, b)


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def _best_runs(runs: list[Run]) -> list[Run]:
    """Keep the longest runs that don't cover the same stretch of A or B twice (repeated content, drift)."""
    chosen: list[Run] = []
    for r in sorted(runs, key=lambda r: -r.length):
        clash = False
        for c in chosen:
            if _overlap(r.b_start, r.b_start + r.length, c.b_start, c.b_start + c.length) > r.length // 2 or \
                    _overlap(r.a_start, r.a_start + r.length, c.a_start, c.a_start + c.length) > r.length // 2:
                clash = True
                break
        if not clash:
            chosen.append(r)
    return sorted(chosen, key=lambda r: r.b_start)


def _gaps(spans: list[tuple[int, int]], total: int, useful: list[bool], fps: float, min_gap: int) -> list:
    """Stretches of a video not covered by any run, ignoring flat frames at the edges of a gap."""
    covered = [False] * total
    for s, e in spans:
        for k in range(max(0, s), min(total, e)):
            covered[k] = True
    gaps, k = [], 0
    while k < total:
        if covered[k]:
            k += 1
            continue
        g0 = k
        while k < total and not covered[k]:
            k += 1
        g1 = k
        lo, hi = g0, g1
        while lo < hi and not useful[lo]:
            lo += 1
        while hi > lo and not useful[hi - 1]:
            hi -= 1
        if hi - lo >= min_gap:
            gaps.append((round(g0 / fps, 2), round(g1 / fps, 2)))
    return gaps


def _jumps(runs: list[Run], side: str, min_gap: int, fps: float) -> list[tuple[float, float]]:
    """Time removed between two runs, found from the jump in offset rather than from the frames, so a cut that
    was dark or plain (frames too flat to match) is still reported."""
    key = (lambda r: r.a_start) if side == "a" else (lambda r: r.b_start)
    other = (lambda r: r.b_start) if side == "a" else (lambda r: r.a_start)
    ordered = sorted(runs, key=key)
    out = []
    for prev, nxt in zip(ordered, ordered[1:]):
        here = key(nxt) - (key(prev) + prev.length)            # gap on this side
        there = other(nxt) - (other(prev) + prev.length)       # gap on the other side at the same place
        if here - max(0, there) >= min_gap and there >= -OFFSET_SLACK:
            start = key(prev) + prev.length + max(0, there)
            out.append((round(start / fps, 2), round(key(nxt) / fps, 2)))
    return out


def _merge(gaps: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for s, e in sorted(gaps):
        if out and s <= out[-1][1] + 0.5:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _coverage(spans: list[tuple[int, int]], useful: list[bool]) -> float:
    total = sum(useful) or 1
    covered = set()
    for s, e in spans:
        covered.update(k for k in range(max(0, s), min(len(useful), e)) if useful[k])
    return len(covered) / total


def _describe(m: Match, a: Fingerprint, b: Fingerprint) -> Match:
    a_spans = [(r.a_start, r.a_start + r.length) for r in m.runs]
    b_spans = [(r.b_start, r.b_start + r.length) for r in m.runs]
    a_useful = [not f for f in a.flat]
    b_useful = [not f for f in b.flat]
    m.coverage_a = _coverage(a_spans, a_useful)
    m.coverage_b = _coverage(b_spans, b_useful)
    min_gap = max(2, round(MIN_RUN_SECONDS * m.fps))
    m.a_gaps = _merge(_gaps(a_spans, m.a_samples, a_useful, m.fps, min_gap) + _jumps(m.runs, "a", min_gap, m.fps))
    m.b_gaps = _merge(_gaps(b_spans, m.b_samples, b_useful, m.fps, min_gap) + _jumps(m.runs, "b", min_gap, m.fps))
    a_order = [r.a_start for r in m.runs]                 # runs are in B's order
    m.reordered = a_order != sorted(a_order)
    big, small = max(m.coverage_a, m.coverage_b), min(m.coverage_a, m.coverage_b)
    if small >= 0.9 and not m.a_gaps and not m.b_gaps:
        m.kind = DUPLICATE
    elif big >= 0.9:
        m.kind = CUT
    else:
        m.kind = SHARED
    return m


# ------------------------------------------------------------------ many videos
def _signature_chunks(fp: Fingerprint) -> set[tuple[int, int]]:
    return {(k, c) for h, flat in zip(fp.hashes, fp.flat) if not flat for k, c in enumerate(_chunks(h))}


def candidate_pairs(prints: dict[str, Fingerprint], min_shared: int = 8) -> list[tuple[str, str]]:
    """Pairs of videos worth comparing: they share at least `min_shared` exact hash chunks."""
    owners: dict[tuple[int, int], list[str]] = defaultdict(list)
    for key, fp in prints.items():
        for chunk in _signature_chunks(fp):
            owners[chunk].append(key)
    shared: dict[tuple[str, str], int] = defaultdict(int)
    for keys in owners.values():
        if len(keys) > 50:          # a chunk half the library has (a common studio card) says nothing
            continue
        for x in range(len(keys)):
            for y in range(x + 1, len(keys)):
                pair = (keys[x], keys[y]) if keys[x] < keys[y] else (keys[y], keys[x])
                shared[pair] += 1
    return [pair for pair, n in shared.items() if n >= min_shared]


@dataclass
class Found:
    a: str
    b: str
    match: Match


def find_copies(prints: dict[str, Fingerprint], *, include_shared: bool = True,
                progress=None, cancel=None) -> list[Found]:
    """Every pair of videos that are copies, cuts of each other, or (optionally) share a section.
    A is the longer of the two, so "B is a cut of A" reads naturally."""
    pairs = candidate_pairs(prints)
    out: list[Found] = []
    indexes: dict[str, _Index] = {}
    for n, (x, y) in enumerate(pairs):
        if cancel is not None and cancel.is_set():
            break
        if progress and n % 20 == 0:
            progress(int(n * 100 / max(1, len(pairs))), f"Comparing {n + 1} of {len(pairs)}…")
        a, b = (x, y) if len(prints[x].hashes) >= len(prints[y].hashes) else (y, x)
        index = indexes.get(a) or indexes.setdefault(a, _Index(prints[a]))
        match = compare(prints[a], prints[b], a_index=index)
        if match is None or (match.kind == SHARED and not include_shared):
            continue
        out.append(Found(a, b, match))
    return out


def groups(found: list[Found]) -> list[list[str]]:
    """Videos that are copies or cuts of each other, grouped (a copy of a copy is in the same group)."""
    parent: dict[str, str] = {}

    def root(k: str) -> str:
        while parent.setdefault(k, k) != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for f in found:
        if f.match.kind in (DUPLICATE, CUT):
            parent[root(f.a)] = root(f.b)
    members: dict[str, list[str]] = defaultdict(list)
    for k in parent:
        members[root(k)].append(k)
    return sorted((sorted(v) for v in members.values() if len(v) > 1), key=lambda g: g[0])


# ------------------------------------------------------------------ the library
@dataclass
class LibraryResult:
    prints: dict[str, Fingerprint]          # path -> fingerprint
    found: list[Found]
    read: int = 0                           # videos fingerprinted this time (the rest came from the database)
    problems: list[str] = field(default_factory=list)
    stopped: bool = False


def scan_library(db, *, include_shared: bool = True, progress=None, cancel=None) -> LibraryResult:
    """Fingerprint every video in the library that isn't already (each is read once; a changed file is read
    again), then compare them. Fingerprinting can be stopped and picks up where it left off next time."""
    import os
    from .media import fingerprint as F
    from .tagdb import file_key
    records = [r for r in db.library() if not r.missing]
    prints: dict[str, Fingerprint] = {}
    result = LibraryResult(prints, [])
    todo = []
    for r in records:
        try:
            st = os.stat(r.path)
        except OSError:
            continue
        fp = db.fingerprint(r.path, st.st_size, st.st_mtime, F.VERSION)
        if fp is not None:
            prints[r.path] = fp
        else:
            todo.append((r, st))
    for n, (r, st) in enumerate(todo):
        if cancel is not None and cancel.is_set():
            result.stopped = True
            break
        if progress:
            progress(int(n * 90 / max(1, len(todo))), f"Reading {os.path.basename(r.path)} ({n + 1} of {len(todo)})…")
        try:
            fp = F.fingerprint(r.path, duration=r.duration, cancel=cancel)
        except F.Cancelled:
            result.stopped = True
            break
        except Exception as exc:  # noqa: BLE001
            result.problems.append(f"{os.path.basename(r.path)}: {exc}")
            continue
        db.save_fingerprint(r.path, st.st_size, st.st_mtime, fp)
        prints[r.path] = fp
        result.read += 1
    db.forget_fingerprints({file_key(r.path) for r in records})
    if not result.stopped:
        if progress:
            progress(92, "Comparing videos…")
        result.found = find_copies(prints, include_shared=include_shared, cancel=cancel)
    if progress:
        progress(100, "Done")
    return result
