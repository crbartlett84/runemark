"""Run slow work (ffmpeg, file writes) off the UI thread and report back with signals."""
from __future__ import annotations

import logging
import threading
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

log = logging.getLogger(__name__)


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(object, str)        # exception, traceback text
    progress = Signal(int, str)          # percent (-1 = busy), message
    item = Signal(object)                # incremental results (e.g. filmstrip frames)


class Job(QRunnable):
    """fn(job, *args) runs in a pool thread. It may call job.report(...) and job.emit(...)."""

    def __init__(self, fn: Callable[..., Any], *args, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.signals = _Signals()
        self.cancel = threading.Event()
        self.setAutoDelete(False)

    def _send(self, signal, *values) -> None:
        """Emit, unless whoever was listening has gone (a window closed mid-job, or the app quitting): then the
        job is cancelled quietly instead of raising "Signal source has been deleted" from a pool thread."""
        try:
            signal.emit(*values)
        except RuntimeError as exc:
            if "deleted" not in str(exc):
                raise
            self.cancel.set()
            raise _Orphaned from None

    def report(self, percent: int, message: str = "") -> None:
        self._send(self.signals.progress, percent, message)

    def emit(self, value: Any) -> None:
        self._send(self.signals.item, value)

    def run(self) -> None:
        try:
            try:
                result = self.fn(self, *self.args, **self.kwargs)
            except _Orphaned:
                raise
            except BaseException as exc:  # noqa: BLE001
                text = traceback.format_exc()
                log.warning("background job failed: %s", text)
                self._send(self.signals.failed, exc, text)
            else:
                self._send(self.signals.done, result)
        except _Orphaned:
            _alive.discard(self)
            log.info("background job stopped: its window has closed")


class _Orphaned(BaseException):
    """Internal: the job's listeners are gone. A BaseException, so a job's own `except Exception` doesn't
    swallow it and carry on working for nobody."""


_alive: set[Job] = set()


def start(fn: Callable[..., Any], *args, on_done=None, on_error=None, on_progress=None, on_item=None,
          pool: QThreadPool | None = None, **kwargs) -> Job:
    job = Job(fn, *args, **kwargs)
    _alive.add(job)

    def finish(*_):
        _alive.discard(job)

    if on_done:
        job.signals.done.connect(on_done)
    if on_error:
        job.signals.failed.connect(on_error)
    if on_progress:
        job.signals.progress.connect(on_progress)
    if on_item:
        job.signals.item.connect(on_item)
    job.signals.done.connect(finish)
    job.signals.failed.connect(finish)
    (pool or QThreadPool.globalInstance()).start(job)
    return job
