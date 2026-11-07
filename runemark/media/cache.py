"""0.12.0: a local copy of videos that live on a network share, so scrubbing and frame grabs read the local disk.

When a video on a network drive or UNC path opens, it is copied into a cache folder on this computer in the
background (and the next few in the open list after it). The player switches to the copy when it's complete; saves
still write the original on the share. Copies are named from the original's path and its size and modified time, so
a changed original gets a fresh copy; until then the older copy of the same video is played (the picture is the same:
a save changes tags and details, not the video). The cache keeps under its size limit by dropping the copies used
longest ago, never one being played.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

PART = ".part"
CHUNK = 8 * 1024 * 1024


def local_cache_dir() -> Path:
    """%LOCALAPPDATA%\\Runemark\\VideoCache on Windows (not the roaming profile), else ~/.cache/runemark/videos.
    RUNEMARK_HOME (tests) puts it under that folder."""
    home = os.environ.get("RUNEMARK_HOME")
    if home:
        return Path(home) / "VideoCache"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Runemark" / "VideoCache"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "runemark" / "videos"


def is_network_path(path: str | Path) -> bool:
    """A UNC path (\\\\server\\share\\...) or a file on a mapped network drive (Windows)."""
    text = str(path)
    if text.startswith(("\\\\", "//")):
        return not text.startswith(("\\\\?\\", "\\\\.\\")) or text.upper().startswith("\\\\?\\UNC\\")
    if os.name != "nt" or len(text) < 2 or text[1] != ":":
        return False
    try:
        import ctypes
        return ctypes.windll.kernel32.GetDriveTypeW(text[:2] + "\\") == 4      # DRIVE_REMOTE
    except Exception:  # noqa: BLE001
        return False


def _path_key(path: Path) -> str:
    return hashlib.sha1(os.path.normcase(os.path.abspath(str(path))).encode("utf-8")).hexdigest()[:16]


def _version_key(size: int, mtime_ns: int) -> str:
    return hashlib.sha1(f"{size}:{mtime_ns}".encode()).hexdigest()[:8]


class VideoCache:
    """The cache folder and one background copier. ready(original) is called (from the copier's thread) when a copy
    is complete. is_remote is replaceable (tests)."""

    def __init__(self, root: Path | None = None, limit_bytes: int = 20 * 1024 ** 3, ahead: int = 3,
                 ready: Callable[[Path], None] | None = None, is_remote: Callable[[Path], bool] = is_network_path):
        self.root = Path(root or local_cache_dir())
        self.limit, self.ahead, self.ready, self.is_remote = int(limit_bytes), int(ahead), ready, is_remote
        self.playing: set[str] = set()              # copies in use (never dropped)
        self._queue: list[Path] = []
        self._wanted: set[str] = set()              # 0.12.1: a copy under way stops when it's no longer wanted
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._stop = False
        self.copying: Path | None = None

    # ---------------------------------------------------------------- what's there
    def _name(self, original: Path, st: os.stat_result) -> str:
        return f"{_path_key(original)}-{_version_key(st.st_size, st.st_mtime_ns)}{original.suffix.lower()}"

    def fresh_copy(self, original: Path) -> Path | None:
        """The complete copy of the original as it is now, or None."""
        try:
            st = os.stat(original)
        except OSError:
            return None
        local = self.root / self._name(Path(original), st)
        try:
            if local.is_file() and local.stat().st_size == st.st_size:
                return local
        except OSError:
            pass
        return None

    def any_copy(self, original: Path) -> Path | None:
        """The copy of the original as it is now, else the newest older copy of the same video (to play while a new
        one is made: a save changes the details, not the picture). None when there's none."""
        fresh = self.fresh_copy(original)
        if fresh is not None:
            return fresh
        prefix = _path_key(Path(original)) + "-"
        try:
            older = [p for p in self.root.iterdir() if p.name.startswith(prefix) and not p.name.endswith(PART)]
        except OSError:
            return None
        return max(older, key=lambda p: p.stat().st_mtime, default=None)

    def source_for(self, original: Path) -> Path:
        """What to play for this video: its local copy when there is one, else the original."""
        original = Path(original)
        if not self.is_remote(original):
            return original
        local = self.any_copy(original)
        if local is None:
            return original
        self.use(local)
        return local

    def use(self, local: Path) -> None:
        """Mark a copy as used now (the cache drops the copies used longest ago first)."""
        try:
            os.utime(local)
        except OSError:
            pass

    def size(self) -> int:
        try:
            return sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())
        except OSError:
            return 0

    # ---------------------------------------------------------------- copying
    def want(self, paths: list[Path]) -> None:
        """Copy these (the open video first, then the next ones) unless they're copied already. Replaces what was
        waiting; the copy under way carries on when it's one of these."""
        wanted = []
        for p in paths[: 1 + self.ahead]:
            p = Path(p)
            if self.is_remote(p) and self.fresh_copy(p) is None and p not in wanted:
                wanted.append(p)
        with self._lock:
            self._queue = list(wanted)
            self._wanted = {_path_key(p) for p in wanted}
        if wanted:
            self._start()
            self._wake.set()

    def _start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop = False
            self._thread = threading.Thread(target=self._run, name="runemark-video-cache", daemon=True)
            self._thread.start()

    def cancel(self, original: Path) -> None:
        """0.12.1: don't copy this video (now): it's about to be saved, moved or rejected, and an open read of it on
        the share would get in the way (Windows can't replace or move a file another program has open)."""
        key = _path_key(Path(original))
        with self._lock:
            self._queue = [p for p in self._queue if _path_key(p) != key]
            self._wanted.discard(key)

    def stop(self):
        self._stop = True
        self._wake.set()

    def _next(self) -> Path | None:
        with self._lock:
            return self._queue.pop(0) if self._queue else None

    def _run(self):
        while not self._stop:
            original = self._next()
            if original is None:
                self._wake.clear()
                if not self._wake.wait(30):
                    return                           # idle: the thread ends, want() starts another
                continue
            try:
                local = self.copy(original)
            except (Exception, InterruptedError) as exc:  # noqa: BLE001
                log.info("video cache: couldn't copy %s: %s", original.name, exc)
                continue
            if local is not None and self.ready is not None:
                try:
                    self.ready(original)
                except Exception:  # noqa: BLE001
                    log.exception("video cache: ready callback")

    def copy(self, original: Path) -> Path | None:
        """Copy one video now (on this thread). None when it's gone, or too big for the cache."""
        st = os.stat(original)
        if st.st_size > self.limit // 2:
            return None                              # one video may not crowd out the rest
        local = self.root / self._name(original, st)
        if local.is_file() and local.stat().st_size == st.st_size:
            return local
        self.root.mkdir(parents=True, exist_ok=True)
        self.make_room(st.st_size)
        part = local.with_name(local.name + PART)
        self.copying = original
        key = _path_key(original)
        started = time.monotonic()
        log.info("video cache: copying %s (%d MB)", original.name, st.st_size // 2 ** 20)
        try:
            with open(original, "rb") as src, open(part, "wb") as dst:
                while True:
                    if self._stop:
                        raise InterruptedError("stopped")
                    if self.ready is not None and key not in self._wanted:
                        raise InterruptedError("no longer wanted")      # 0.12.1: moved on, or it's being saved
                    block = src.read(CHUNK)
                    if not block:
                        break
                    dst.write(block)
            if os.stat(original).st_mtime_ns != st.st_mtime_ns:
                raise OSError("it changed while it was being copied")
            os.replace(part, local)
            secs = max(0.001, time.monotonic() - started)
            log.info("video cache: copied %s in %.0f s (%.0f MB/s)", original.name, secs, st.st_size / 2 ** 20 / secs)
        except BaseException as exc:
            log.info("video cache: %s not copied: %s", original.name, exc)
            try:
                part.unlink()
            except OSError:
                pass
            raise
        finally:
            self.copying = None
        self._drop_older(original, local)
        return local

    def _drop_older(self, original: Path, keep: Path):
        prefix = _path_key(original) + "-"
        for p in self.root.iterdir():
            if p.name.startswith(prefix) and p != keep and str(p) not in self.playing and not p.name.endswith(PART):
                try:
                    p.unlink()
                except OSError:
                    pass

    def make_room(self, needed: int) -> None:
        """Drop the copies used longest ago until `needed` more bytes fit under the limit."""
        try:
            files = [p for p in self.root.iterdir() if p.is_file()]
        except OSError:
            return
        now = time.time()
        for p in files:                              # leftovers of a copy that never finished
            if p.name.endswith(PART) and now - p.stat().st_mtime > 3600:
                try:
                    p.unlink()
                except OSError:
                    pass
        files = [p for p in files if p.exists() and not p.name.endswith(PART)]
        total = sum(p.stat().st_size for p in files)
        for p in sorted(files, key=lambda p: p.stat().st_mtime):
            if total + needed <= self.limit:
                break
            if str(p) in self.playing:
                continue
            try:
                size = p.stat().st_size
                p.unlink()
                total -= size
            except OSError:
                pass

    def clear(self) -> int:
        """Empty the cache (not the copies in use). Returns the bytes freed."""
        freed = 0
        try:
            files = list(self.root.iterdir())
        except OSError:
            return 0
        for p in files:
            if str(p) in self.playing:
                continue
            try:
                size = p.stat().st_size
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                freed += size
            except OSError:
                pass
        return freed
