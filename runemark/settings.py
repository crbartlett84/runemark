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
    library_folders: list[str] = field(default_factory=list)  # every library's folders together (kept in sync)
    libraries: list[dict] = field(default_factory=list)       # [{"name", "folders": [...], "skip": [...]}] (0.7.0)
    library_shown: str = ""                                   # the library the Library window shows ("" = all)
    saved_searches: dict = field(default_factory=dict)   # name -> {"tags": [...], "text": "", "filter": ""}
    library_geometry: str = ""
    library_last_search: dict = field(default_factory=dict)   # the Library reopens with this search/filter
    name_patterns: dict = field(default_factory=dict)         # "read" / "rename" -> the last pattern used
    suggestions_count: int = 8
    read_nfo: bool = True                 # fill empty details from a .nfo beside a video the first time it opens
    chapter_keys: dict = field(default_factory=dict)          # "1".."9" -> the chapter name that key adds
    tag_keys: dict = field(default_factory=dict)              # "1".."9" -> the tag Alt+that key adds or removes
    scan_on_start: bool = True            # look for new and changed videos in the library folders on start
    check_updates: bool = True            # ask GitHub once a day whether there's a newer Runemark
    last_update_check: str = ""           # ISO date of the last check
    skipped_version: str = ""             # "don't tell me about this one"
    library_view: str = "list"            # "list" or "grid"
    plugins_disabled: list[str] = field(default_factory=list)   # plugin folder names turned off (0.7.0)
    plugin_settings: dict = field(default_factory=dict)         # plugin folder name -> that plugin's own settings

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
        settings = cls(**{k: v for k, v in data.items() if k in known})
        settings.sync_libraries()
        return settings

    def sync_libraries(self) -> None:
        """Before 0.7.0 there was one list of folders: it becomes a library called "Library". Afterwards
        library_folders is always every library's folders together (what a scan walks)."""
        if not self.libraries and self.library_folders:
            self.libraries = [{"name": "Library", "folders": list(self.library_folders), "skip": []}]
        all_folders: list[str] = []
        for lib in self.libraries:
            lib.setdefault("folders", [])
            lib.setdefault("skip", [])
            for folder in lib["folders"]:
                if folder.casefold() not in {f.casefold() for f in all_folders}:
                    all_folders.append(folder)
        self.library_folders = all_folders

    def save(self) -> None:
        self.sync_libraries()
        try:
            tmp = self.path().with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            tmp.replace(self.path())
        except Exception as exc:  # noqa: BLE001
            log.warning("couldn't save settings: %s", exc)

    def add_recent(self, path: str, limit: int = 12) -> None:
        items = [p for p in self.recent_files if p.casefold() != path.casefold()]
        self.recent_files = [path, *items][:limit]
