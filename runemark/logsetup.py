"""Logging to a rotating file in the user's Runemark folder (Settings → Open log folder)."""
from __future__ import annotations

import logging
import logging.handlers
import platform
import sys

from . import __version__
from .paths import log_dir


def setup(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if any(getattr(h, "_runemark", False) for h in root.handlers):
        return
    root.setLevel(level)
    handler = logging.handlers.RotatingFileHandler(
        log_dir() / "runemark.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler._runemark = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    if sys.stderr is not None and not getattr(sys, "frozen", False):
        console = logging.StreamHandler()
        console.setLevel(logging.WARNING)
        console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(console)
    logging.getLogger(__name__).info("Runemark %s on %s, Python %s", __version__, platform.platform(),
                                     platform.python_version())
