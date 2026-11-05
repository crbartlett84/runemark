"""Background saves (0.9.0): saves wait their turn in one queue and are written one at a time, off the window's
thread, with the same safe write and read-back check as before. The window carries on meanwhile (Save and next
goes straight to the next video), and the status bar shows how far the save has got.

The rules that keep files safe:
* one write at a time, so a file is never written twice at once (and a second save of a file waits for the first);
* before a write starts, whoever is reading the file (the player, frame grabs) is asked to let go of it;
* a save that fails changes nothing (the write goes to a working copy first), and is offered again (Retry).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, Signal

from ..tagdb import file_key
from . import workers

log = logging.getLogger(__name__)


@dataclass(eq=False)
class SaveJob:
    """One file to write. `work(job)` runs in a pool thread (it may call job.report); `on_done(outcome)` and
    `on_failed(exc)` run on the window's thread afterwards."""
    path: Path
    work: Callable[[Any], Any]
    on_done: Callable[[Any], Any] | None = None
    on_failed: Callable[[BaseException], Any] | None = None
    label: str = ""
    doc: Any = None                    # the editor's document this save came from (shown read-only if reopened)
    state: str = "queued"              # queued, running, done, failed
    percent: int = -1
    message: str = ""
    position_ms: int = 0               # where the player was when it let go of the file
    waiters: list = field(default_factory=list)   # callbacks(ok: bool) for when this job has finished

    @property
    def key(self) -> str:
        return file_key(self.path)

    def clone(self) -> "SaveJob":
        """The same save again (Retry)."""
        return SaveJob(self.path, self.work, self.on_done, self.on_failed, self.label, self.doc)


class SaveQueue(QObject):
    """Saves, one at a time, in the order they were asked for."""

    changed = Signal()                 # something started, finished or reported progress (for the status bar)
    finished = Signal(object, bool)    # (job, ok)

    def __init__(self, release: Callable[[Path, Callable[[int], None]], None] | None = None, parent=None,
                 start_delay_ms: int = 0):
        super().__init__(parent)
        # release(path, then): make everything reading `path` let go of it, then call then(position_ms)
        self._release = release or (lambda _path, then: then(0))
        self._jobs: list[SaveJob] = []
        self._running: SaveJob | None = None
        self._idle_waiters: list[Callable[[], Any]] = []
        self._start_delay_ms = start_delay_ms
        self._kick_pending = False

    # ---------------------------------------------------------------- asking
    def add(self, job: SaveJob) -> SaveJob:
        job.state = "queued"
        self._jobs.append(job)
        self.changed.emit()
        self._kick()
        return job

    def pending(self, path) -> SaveJob | None:
        """The save waiting for or being written to this file, if any (the latest one)."""
        key = file_key(path)
        for job in reversed(self.jobs()):
            if job.key == key:
                return job
        return None

    def pending_any(self, paths) -> SaveJob | None:
        keys = {file_key(p) for p in paths}
        return next((j for j in self.jobs() if j.key in keys), None)

    def jobs(self) -> list[SaveJob]:
        """Queued and running jobs, running first."""
        return ([self._running] if self._running is not None else []) + list(self._jobs)

    def idle(self) -> bool:
        return self._running is None and not self._jobs

    def count(self) -> int:
        return len(self.jobs())

    @property
    def running(self) -> SaveJob | None:
        return self._running

    def when_done(self, path, callback: Callable[[bool], Any]) -> bool:
        """Call callback(ok) once the save of this file finishes (ok=False when it failed). False when there's no
        save of it waiting (the caller can go ahead now)."""
        job = self.pending(path)
        if job is None:
            return False
        job.waiters.append(callback)
        return True

    def when_idle(self, callback: Callable[[], Any]) -> None:
        """Call back once every save has finished (straight away if none is waiting)."""
        if self.idle():
            callback()
        else:
            self._idle_waiters.append(callback)

    def summary(self) -> str:
        """For the status bar: "Saving a.mp4… 42% · 2 more waiting"."""
        job = self._running
        if job is None:
            return f"{len(self._jobs)} save(s) waiting" if self._jobs else ""
        text = f"Saving {job.path.name}…"
        if job.percent >= 0:
            text += f" {job.percent}%"
        elif job.message:
            text += f" {job.message.rstrip('…')}…" if not job.message.startswith("Saving") else ""
        if self._jobs:
            text += f" · {len(self._jobs)} more waiting"
        return text

    # ---------------------------------------------------------------- running
    def _kick(self):
        if self._kick_pending or self._running is not None:
            return
        self._kick_pending = True
        # on the next turn of the event loop: Save and next opens the next video first, so the player has already
        # moved off this file by the time the write starts
        QTimer.singleShot(self._start_delay_ms, self._next)

    def _next(self):
        self._kick_pending = False
        if self._running is not None or not self._jobs:
            if self.idle():
                self._call_idle_waiters()
            return
        job = self._jobs.pop(0)
        self._running = job
        job.state = "running"
        job.message = "waiting for the file"
        self.changed.emit()

        def go(position_ms: int = 0):
            job.position_ms = position_ms or 0
            job.message = ""
            workers.start(lambda w: job.work(w), on_done=lambda outcome: self._done(job, outcome),
                          on_error=lambda exc, _tb: self._failed(job, exc), on_progress=lambda p, m: self._progress(job, p, m))

        try:
            self._release(job.path, go)
        except Exception as exc:  # noqa: BLE001
            log.exception("couldn't free %s for saving", job.path)
            self._failed(job, exc)

    def _progress(self, job: SaveJob, percent: int, message: str):
        job.percent, job.message = percent, message
        self.changed.emit()

    def _finish(self, job: SaveJob, ok: bool):
        """After the job's own callback: tell whoever was waiting for it, then start the next one."""
        waiters, job.waiters = job.waiters, []
        self.changed.emit()
        self.finished.emit(job, ok)
        for waiter in waiters:
            try:
                waiter(ok)
            except Exception:  # noqa: BLE001
                log.exception("a callback after saving %s failed", job.path)
        if self._jobs:
            self._kick()
        elif self._running is None:
            self._call_idle_waiters()

    def _done(self, job: SaveJob, outcome):
        self._running = None              # no longer pending: its callback may open the file again
        job.state = "done"
        try:
            if job.on_done:
                job.on_done(outcome)
        except Exception:  # noqa: BLE001
            log.exception("after saving %s", job.path)
        finally:
            self._finish(job, True)

    def _failed(self, job: SaveJob, exc: BaseException):
        self._running = None
        job.state = "failed"
        log.warning("save of %s failed: %s", job.path, exc)
        try:
            if job.on_failed:
                job.on_failed(exc)
        except Exception:  # noqa: BLE001
            log.exception("after a failed save of %s", job.path)
        finally:
            self._finish(job, False)

    def _call_idle_waiters(self):
        waiters, self._idle_waiters = self._idle_waiters, []
        for waiter in waiters:
            try:
                waiter()
            except Exception:  # noqa: BLE001
                log.exception("a callback after the saves finished failed")
