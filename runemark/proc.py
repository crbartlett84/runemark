"""Running ffmpeg/ffprobe without console windows flashing up on Windows."""
from __future__ import annotations

import logging
import subprocess
from typing import Sequence

from .paths import IS_WINDOWS, find_tool

log = logging.getLogger(__name__)

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0


class ToolMissing(RuntimeError):
    pass


class ToolFailed(RuntimeError):
    def __init__(self, args: Sequence[str], returncode: int, stderr: str):
        self.args_list = list(args)
        self.returncode = returncode
        self.stderr = stderr
        tail = stderr.strip().splitlines()[-6:]
        super().__init__(f"{_short(args)} failed (exit {returncode}): " + " | ".join(tail))


def _short(args: Sequence[str]) -> str:
    return " ".join(str(a) for a in args[:1]) if args else "?"


def tool(name: str) -> str:
    path = find_tool(name)
    if not path:
        raise ToolMissing(
            f"{name} wasn't found. Put {name} in the 'ffmpeg' folder next to Runemark, or on your PATH."
        )
    return path


def ffmpeg() -> str:
    return tool("ffmpeg")


def ffprobe() -> str:
    return tool("ffprobe")


def run(args: Sequence[str], *, timeout: float | None = 120, input: bytes | None = None,
        check: bool = True) -> subprocess.CompletedProcess:
    """Run a tool, capture output as bytes, raise ToolFailed on a non-zero exit."""
    log.debug("run: %s", " ".join(str(a) for a in args))
    proc = subprocess.run(
        list(args), input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, creationflags=NO_WINDOW,
        stdin=None if input is not None else subprocess.DEVNULL,
    )
    if check and proc.returncode != 0:
        raise ToolFailed(args, proc.returncode, proc.stderr.decode("utf-8", "replace"))
    return proc


def popen(args: Sequence[str], **kwargs) -> subprocess.Popen:
    log.debug("popen: %s", " ".join(str(a) for a in args))
    return subprocess.Popen(list(args), creationflags=NO_WINDOW, **kwargs)
