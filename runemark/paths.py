"""Where Runemark keeps its data, and where it finds ffmpeg/ffprobe."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import APP_NAME

IS_WINDOWS = sys.platform == "win32"
FROZEN = bool(getattr(sys, "frozen", False))


PORTABLE_MARKER = "Runemark-portable.txt"
PORTABLE_FOLDER = "Runemark data"


def app_folder() -> Path:
    """The folder Runemark runs from (beside Runemark.exe, or the project folder)."""
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def portable_data_dir() -> Path:
    return app_folder() / PORTABLE_FOLDER


def is_portable() -> bool:
    return (app_folder() / PORTABLE_MARKER).is_file()


def standard_data_dir() -> Path:
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def app_data_dir() -> Path:
    """Data folder (tag database, settings, backups, logs).

    Windows: %APPDATA%\\Runemark. Elsewhere: ~/.local/share/Runemark. Portable mode (a
    Runemark-portable.txt beside the app) keeps it in "Runemark data" beside the app instead.
    RUNEMARK_HOME overrides all of these (used by tests).
    """
    override = os.environ.get("RUNEMARK_HOME")
    if override:
        path = Path(override)
    elif is_portable():
        path = portable_data_dir()
    else:
        path = standard_data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    path = app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def backup_dir() -> Path:
    path = app_data_dir() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resource_root() -> Path:
    """Folder holding bundled resources: the PyInstaller bundle, or the project root."""
    if FROZEN and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


def find_tool(name: str) -> str | None:
    """Locate ffmpeg or ffprobe: env override, bundled copy, vendor folder, then PATH."""
    exe = name + (".exe" if IS_WINDOWS else "")
    candidates: list[Path] = []
    env_dir = os.environ.get("RUNEMARK_FFMPEG_DIR")
    if env_dir:
        candidates.append(Path(env_dir) / exe)
    root = resource_root()
    candidates += [root / "ffmpeg" / exe, root / "vendor" / "ffmpeg" / exe]
    if FROZEN:
        candidates.append(Path(sys.executable).parent / "ffmpeg" / exe)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)
