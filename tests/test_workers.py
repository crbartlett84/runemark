"""A background job whose window has gone (closed mid-job, or the app quitting) stops quietly."""
import logging

import pytest

from runemark.ui import workers


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_job_stops_quietly_when_its_listeners_are_gone(qapp, caplog):
    seen = []

    def work(job):
        job.emit(1)
        seen.append("after first frame")
        job.signals.deleteLater()
        from shiboken6 import delete
        delete(job.signals)                       # what happens to the signals object when its owner goes
        job.emit(2)                               # must not raise into the pool thread
        seen.append("never reached")

    job = workers.Job(work)
    with caplog.at_level(logging.INFO, logger="runemark.ui.workers"):
        job.run()                                 # run inline: same code path as in the pool
    assert seen == ["after first frame"] and job.cancel.is_set()
    assert "background job failed" not in caplog.text and "window has closed" in caplog.text
