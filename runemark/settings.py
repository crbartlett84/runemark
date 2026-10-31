"""User settings, kept as JSON beside the tag database."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .paths import app_data_dir

log = logging.getLogger(__name__)


@dataclass
class Settings:
    theme: str = "system"                 # system, dark, light
    thumbnail_max_edge: int = 1280        # 0 keeps the frame's full size
    jpeg_quality: int = 90
    filmstrip_count: int = 10
    preserve_modified_date: bool = False
    explorer_writer: str = "auto"         # auto (Windows, then built-in) or builtin
    volume: int = 60
    muted: bool = False
    last_folder: str = ""
    recent_files: list[str] = field(default_factory=list)
    last_category_id: int | None = None
    window_geometry: str = ""
    splitter_state: str = ""
    backup_folder: str = ""               # "" = the backups folder inside the data folder
    backup_keep: int = 10
    library_folders: list[str] = field(default_factory=list)
    saved_searches: dict = field(default_factory=dict)   # name -> {"tags": [...], "text": "", "filter": ""}
    library_geometry: str = ""
    library_last_search: dict = field(default_factory=dict)   # the Library reopens with this search/filter
    suggestions_count: int = 8

    @classmethod
    def path(cls) -> Path:
        return app_data_dir() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(cls.path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except Exception as exc:  # noqa: BLE001
            log.warning("settings unreadable, using defaults: %s", exc)
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        try:
            tmp = self.path().with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            tmp.replace(self.path())
        except Exception as exc:  # noqa: BLE001
            log.warning("couldn't save settings: %s", exc)

    def add_recent(self, path: str, limit: int = 12) -> None:
        items = [p for p in self.recent_files if p.casefold() != path.casefold()]
        self.recent_files = [path, *items][:limit]
