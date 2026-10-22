"""Backups of the tag database, and moving Runemark's data between this PC and portable mode."""
from __future__ import annotations

import datetime as _dt
import json
import logging
import shutil
from pathlib import Path

from .paths import (PORTABLE_MARKER, app_data_dir, app_folder, backup_dir, is_portable, portable_data_dir,
                    standard_data_dir)

log = logging.getLogger(__name__)


def backup_folder(settings) -> Path:
    """Where automatic backups go: the chosen folder (e.g. in OneDrive) or the data folder's backups."""
    if settings.backup_folder:
        folder = Path(settings.backup_folder)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            return folder
        except OSError as exc:
            log.warning("backup folder %s unusable (%s); using the default", folder, exc)
    return backup_dir()


def daily_backup(db, settings) -> Path | None:
    """One automatic backup per day, keeping the newest `backup_keep`."""
    folder = backup_folder(settings)
    stamp = _dt.date.today().strftime("%Y%m%d")
    if any(p.name.startswith(f"tags-{stamp}") for p in folder.glob("tags-*.db")):
        return None
    return db.backup(folder, keep=max(1, settings.backup_keep))


def backup_now(db, target: str | Path) -> Path:
    return db.backup_to(target)


def safety_backup(db) -> Path:
    """A backup taken just before a restore, so the restore can be undone."""
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return db.backup_to(backup_dir() / f"before-restore-{stamp}.db")


def _copy_settings(source_dir: Path, target_dir: Path) -> None:
    src = source_dir / "settings.json"
    if src.is_file():
        shutil.copy2(src, target_dir / "settings.json")


def _copy_into(db, target: Path) -> None:
    """Make `target` a copy of the open database. Works even when `target` is open elsewhere
    (Windows won't rename or replace an open file, but SQLite can write into it)."""
    import sqlite3
    if target.exists():
        try:
            if target.resolve() == Path(db.path).resolve():
                return
        except OSError:
            pass
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        aside = target.with_name(f"tags-replaced-{stamp}.db")
        source = sqlite3.connect(str(target))
        keep = sqlite3.connect(str(aside))
        try:
            source.backup(keep)
        finally:
            keep.close()
            source.close()
    dest = sqlite3.connect(str(target))
    try:
        db.conn.backup(dest)
    finally:
        dest.close()


def make_portable(db) -> Path:
    """Copy the database and settings beside the app and switch to portable mode (after a restart)."""
    target = portable_data_dir()
    target.mkdir(parents=True, exist_ok=True)
    _copy_into(db, target / "tags.db")
    _copy_settings(app_data_dir(), target)
    (app_folder() / PORTABLE_MARKER).write_text(
        "Runemark keeps its tag database and settings in the \"Runemark data\" folder beside this file.\n"
        "Delete this file (or switch it off in Settings) to keep them on this PC instead.\n", encoding="utf-8")
    return target


def make_standard(db) -> Path:
    """Copy the database and settings back to this PC's data folder and leave portable mode."""
    target = standard_data_dir()
    target.mkdir(parents=True, exist_ok=True)
    _copy_into(db, target / "tags.db")
    _copy_settings(app_data_dir(), target)
    marker = app_folder() / PORTABLE_MARKER
    if marker.exists():
        marker.unlink()
    return target


def describe_location() -> str:
    where = app_data_dir()
    if is_portable():
        return f"Portable: the tag database and settings are in {where}, beside Runemark."
    return f"On this PC: the tag database and settings are in {where}."


__all__ = ["backup_folder", "daily_backup", "backup_now", "safety_backup", "make_portable", "make_standard",
           "describe_location", "json"]
