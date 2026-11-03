"""Plugins: extra features you add to Runemark yourself, without changing Runemark.

A plugin is a folder in Runemark's `plugins` folder (⋯ → Settings → Plugins → Open plugins folder) holding
`plugin.json` and `plugin.py`:

    plugin.json   {"name": "My plugin", "version": "1.0", "description": "...", "api": 1}
    plugin.py     def register(api): ...      (api is a PluginAPI, below)

`register` runs once when Runemark starts. It can add a pane beside Tags/Chapters/Thumbnail/Details, menu actions
in the main window and in the Library, and listen for files being opened and saved. Everything it changes in the
open video goes through the same editor as your own edits: nothing is written until you save.

Plugins are ordinary Python code running inside Runemark, with the same access to your files as Runemark itself.
Only install plugins you trust. A plugin that fails to load is listed with its error and the rest of Runemark
carries on. See docs/plugins.md.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import shutil
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .paths import app_data_dir

log = logging.getLogger(__name__)

API_VERSION = 1
EVENTS = ("file_opened", "file_saved", "file_closed")


def plugins_dir() -> Path:
    folder = app_data_dir() / "plugins"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("couldn't make the plugins folder %s: %s", folder, exc)
    return folder


@dataclass
class Plugin:
    folder: Path
    name: str
    version: str = ""
    description: str = ""
    api: int = API_VERSION
    enabled: bool = True
    loaded: bool = False
    error: str = ""
    module: Any = None

    @property
    def key(self) -> str:
        return self.folder.name


def discover(folder: Path | None = None) -> list[Plugin]:
    """Every plugin folder (one with plugin.json), sorted by name. Unreadable ones are listed with their error."""
    folder = folder or plugins_dir()
    found: list[Plugin] = []
    if not folder.is_dir():
        return found
    for sub in sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
        manifest = sub / "plugin.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            plugin = Plugin(sub, str(data.get("name") or sub.name), str(data.get("version", "")),
                            str(data.get("description", "")), int(data.get("api", API_VERSION)))
        except (OSError, ValueError, TypeError) as exc:
            plugin = Plugin(sub, sub.name, error=f"plugin.json can't be read: {exc}")
        if not (sub / "plugin.py").is_file() and not plugin.error:
            plugin.error = "plugin.py is missing"
        if plugin.api > API_VERSION and not plugin.error:
            plugin.error = f"needs a newer Runemark (plugin API {plugin.api}; this Runemark has {API_VERSION})"
        found.append(plugin)
    return found


@dataclass
class _Action:
    plugin: str
    text: str
    callback: Callable
    shortcut: str | None = None


@dataclass
class PluginHost:
    """What the plugins added, for the windows to show. Filled in by load_all()."""
    plugins: list[Plugin] = field(default_factory=list)
    panes: list[tuple[str, str, Any]] = field(default_factory=list)          # (plugin, title, widget)
    actions: list[_Action] = field(default_factory=list)                    # main window ⋯ → Plugins
    library_actions: list[_Action] = field(default_factory=list)            # Library → More → plugin actions
    listeners: dict[str, list[tuple[str, Callable]]] = field(default_factory=lambda: {e: [] for e in EVENTS})

    def emit(self, event: str, *args) -> None:
        """Tell the plugins something happened. A plugin that raises is logged; the others still hear about it."""
        for plugin, callback in list(self.listeners.get(event, [])):
            try:
                callback(*args)
            except Exception:  # noqa: BLE001
                log.exception("plugin %s failed handling %s", plugin, event)


def load_all(window, disabled: list[str], folder: Path | None = None) -> PluginHost:
    """Load every enabled plugin and let it register what it adds. `window` is the main window."""
    host = PluginHost(discover(folder))
    for plugin in host.plugins:
        plugin.enabled = plugin.key not in disabled
        if plugin.error or not plugin.enabled:
            continue
        try:
            name = f"runemark_plugin_{plugin.key.replace('-', '_').replace(' ', '_')}"
            spec = importlib.util.spec_from_file_location(name, plugin.folder / "plugin.py",
                                                          submodule_search_locations=[str(plugin.folder)])
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            if not callable(getattr(module, "register", None)):
                raise RuntimeError("plugin.py has no register(api) function")
            module.register(PluginAPI(window, host, plugin))
            plugin.module, plugin.loaded = module, True
            log.info("loaded plugin %s %s", plugin.name, plugin.version)
        except Exception as exc:  # noqa: BLE001
            plugin.error = f"{exc}"
            log.warning("plugin %s failed to load:\n%s", plugin.name, traceback.format_exc())
    return host


# ------------------------------------------------------------------ what a plugin can use
class PluginAPI:
    """Everything a plugin may use. Stays the same within one API version (plugin.json "api")."""

    api_version = API_VERSION

    def __init__(self, window, host: PluginHost, plugin: Plugin):
        self._w = window
        self._host = host
        self._plugin = plugin

    # ---- about Runemark
    @property
    def runemark_version(self) -> str:
        from . import __version__
        return __version__

    @property
    def folder(self) -> Path:
        """The plugin's own folder (for images or files it ships with)."""
        return self._plugin.folder

    # ---- adding things
    def add_pane(self, title: str, widget) -> None:
        """A tab beside Tags, Chapters, Thumbnail and Details, showing `widget` (a QWidget the plugin made)."""
        self._host.panes.append((self._plugin.name, title, widget))
        self._w.add_plugin_pane(title, widget)

    def add_action(self, text: str, callback: Callable[[], Any], shortcut: str | None = None) -> None:
        """An entry in ⋯ → Plugins (and Go to anything…). shortcut: "Ctrl+Alt+A" style, optional."""
        self._host.actions.append(_Action(self._plugin.name, text, callback, shortcut))
        self._w.add_plugin_action(text, callback, shortcut)

    def add_library_action(self, text: str, callback: Callable[[list[Path]], Any]) -> None:
        """An entry in the Library's More menu; the callback gets the selected videos' paths."""
        self._host.library_actions.append(_Action(self._plugin.name, text, callback))

    def on(self, event: str, callback: Callable) -> None:
        """file_opened(path): a video was opened (or reopened after a save). file_saved(path): a video was saved,
        in the editor or by a Library change. file_closed(path): the open video is about to move (move_file)."""
        if event not in EVENTS:
            raise ValueError(f"unknown event {event!r}; use one of {', '.join(EVENTS)}")
        self._host.listeners[event].append((self._plugin.name, callback))

    # ---- the open video
    def current_file(self) -> Path | None:
        doc = self._w.doc
        return doc.path if doc is not None else None

    def current_details(self) -> dict | None:
        """What the editor shows for the open video right now (including unsaved edits), or None."""
        return self._w.plugin_details()

    def apply_details(self, **changes) -> list[str]:
        """Change the open video in the editor, as if typed in: nothing is written until it's saved.
        Accepts title, year, date ("YYYY-MM-DD"), studio, description, rating (0-5), links (list, replaces),
        add_links, add_tags (list of tag names), people (list of names, added to the video; new names are added to
        People), cover (JPEG/PNG bytes). Returns what changed, for a status line."""
        return self._w.plugin_apply(changes)

    def save(self, then: Callable[[], Any] | None = None) -> None:
        """Save the open video (the same as Ctrl+S), then call `then`."""
        self._w.save(after=then)

    # ---- your tag database and people
    def tags(self) -> list[dict]:
        """[{"name", "category": ["Parent", "Child"]}] for every tag."""
        db = self._w.db
        return [{"name": t.name, "category": db.category_path(t.category_id)} for t in db.tags()]

    def find_tag(self, name: str) -> str | None:
        """The tag's name as spelled in the database (matching ignores case), or None."""
        tag = self._w.db.find_tag(name)
        return tag.name if tag else None

    def categories(self) -> list[list[str]]:
        db = self._w.db
        return [db.category_path(c.id) for c in db.categories()]

    def add_tag(self, name: str, category: list[str] | None = None) -> str:
        """Add a tag to the database (in an existing category path, or uncategorised). Returns its name."""
        db = self._w.db
        tag = db.find_tag(name)
        if tag is None:
            cid = None
            if category:
                cid = next((c.id for c in db.categories() if db.category_path(c.id) == list(category)), None)
            tag = db.add_tag(name, cid)
            self._w.tag_editor.refresh_database()
        return tag.name

    def rename_tag(self, old: str, new: str) -> str | None:
        """Rename a tag in the database (or merge it into `new` if that exists), and offer to update the videos that
        carry it, exactly like renaming in the Tag database window. Returns the tag's new name, or None."""
        return self._w.plugin_rename_tag(old, new)

    def find_person(self, name: str) -> dict | None:
        """{"name", "aliases", "links", "has_photo"} for the person with this name or alias, or None."""
        db = self._w.db
        p = db.find_person(name)
        return None if p is None else {"name": p.name, "aliases": list(p.aliases), "links": list(p.links),
                                       "has_photo": db.person_photo(p.id) is not None}

    def add_person(self, name: str, aliases: list[str] = (), links: list[str] = (), photo: bytes | None = None,
                   values: dict | None = None) -> str:
        """Add someone to People, or fill in what an existing profile lacks (new aliases, links, a photo when they
        have none, attribute values they don't have yet). values: {"Hair": "Red"}, or {"Hair": ("Red", "Red hair")}
        to choose the tag the value becomes (attributes are created if missing). Returns their main name."""
        db = self._w.db
        person = db.find_person(name)
        if person is None:
            free = [a for a in aliases if db.find_person(a) is None and a.casefold() != name.casefold()]
            person = db.add_person(name, free)
        else:
            extra = [a for a in aliases if db.find_person(a) is None]
            if extra:
                db.update_person(person.id, aliases=list(person.aliases) + extra)
        if links:
            merged = list(person.links) + [x for x in links if x not in person.links]
            if merged != person.links:
                db.set_person_links(person.id, merged)
        if photo and db.person_photo(person.id) is None:
            jpeg = square_photo(photo)
            if jpeg:
                db.set_person_photo(person.id, jpeg)
        for attr_name, value in (values or {}).items():
            value, tag = (value if isinstance(value, (tuple, list)) else (value, None))
            if not value:
                continue
            attr = db.find_attribute(attr_name) or db.add_attribute(attr_name)
            if not any(v.attribute_id == attr.id for v in db.person(person.id).values):
                db.add_person_value(person.id, attr.id, str(value), tag=str(tag) if tag else None)
        self._w.tag_editor.refresh_database()
        return db.person(person.id).name

    def add_attribute(self, name: str, stash_field: str | None = None, as_tag: bool = True) -> str:
        """Add a people attribute field ("Hair"), optionally linked to a Stash performer field ("hair_color") and
        written to videos as tags or not. Does nothing if it exists. Returns its name."""
        db = self._w.db
        attr = db.find_attribute(name) or db.add_attribute(name, stash_field=stash_field, write_tags=as_tag)
        return attr.name

    def attributes(self) -> list[dict]:
        """People's attribute fields: [{"name", "stash_field", "as_tag"}]."""
        return [{"name": a.name, "stash_field": a.stash_field, "as_tag": a.write_tags} for a in self._w.db.attributes()]

    # ---- libraries and files
    def libraries(self) -> list[dict]:
        """[{"name", "folders", "skip"}] as set up in the Library (a copy: changing it changes nothing)."""
        return json.loads(json.dumps(self._w.settings.libraries))

    def move_file(self, path: Path | str, folder: Path | str, new_name: str | None = None,
                  then: Callable[[Path | None, str], Any] | None = None, open_after: bool = True) -> None:
        """Move a video (and files beside it with the same name: .nfo, subtitles…) into `folder`, optionally
        renamed (new_name without extension). Runemark lets go of it first if it's open (an open video with unsaved
        changes isn't moved), and keeps its history and library entry. If it was open it's opened from the new
        place, or with open_after=False the next open video is shown instead. then(new_path, "") when done, or
        then(None, reason) if it couldn't."""
        self._w.plugin_move(Path(path), Path(folder), new_name, then, open_after)

    def upgrade_copy(self, curated: Path | str, better: Path | str, old_to: Path | str | None = None,
                     then: Callable[[Path | None, str], Any] | None = None) -> None:
        """Put `better` (a sharper or more complete copy of the same video) in `curated`'s place: same folder and
        name, with all of curated's tags, people, details, thumbnail and chapters (moved to the matching moments).
        Runemark compares the two, shows the user what will happen and asks first. The curated copy goes to
        `old_to` (a folder), or the Recycle Bin when None. then(final_path, "") when done, or then(None, reason)."""
        self._w.upgrade_copy(Path(curated), Path(better), Path(old_to) if old_to else None, then)

    def video_info(self, path: Path | str) -> dict:
        """{"width", "height", "duration", "bit_rate", "size", "container"} of any video (read with ffprobe)."""
        from .duplicates import describe_copy
        from .tagsync import container_of
        c = describe_copy(str(path))
        return {"width": c.width, "height": c.height, "duration": c.duration, "bit_rate": c.bit_rate,
                "size": c.size, "container": container_of(path)}

    # ---- the plugin's own settings and messages
    @property
    def settings(self) -> dict:
        """The plugin's own settings, kept with Runemark's (call save_settings after changing them)."""
        return self._w.settings.plugin_settings.setdefault(self._plugin.key, {})

    def save_settings(self) -> None:
        self._w.settings.save()

    def status(self, text: str) -> None:
        self._w.set_status(text)

    def run_in_background(self, work: Callable[[], Any], done: Callable[[Any], Any] | None = None,
                          failed: Callable[[Exception], Any] | None = None) -> None:
        """Run work() off the window's thread (network calls, file reads); done(result) or failed(exc) run on the
        window's thread afterwards, so they can update widgets."""
        from .ui import workers
        workers.start(lambda _job: work(), on_done=(lambda r: done(r)) if done else None,
                      on_error=(lambda exc, _tb: failed(exc)) if failed else
                      (lambda exc, _tb: log.warning("plugin %s background work failed: %s", self._plugin.name, exc)))


def square_photo(data: bytes) -> bytes | None:
    """A person's photo as People keeps it: the top-centre square of the picture (heads are near the top)."""
    from PySide6.QtGui import QImage
    from .ui.photo_crop import to_jpeg
    image = QImage.fromData(data)
    if image.isNull():
        return None
    side = min(image.width(), image.height())
    return to_jpeg(image.copy((image.width() - side) // 2, 0, side, side))


def move_with_sidecars(path: Path, folder: Path, new_name: str | None = None) -> tuple[Path, list[tuple[Path, Path]]]:
    """Move a video and the files beside it that share its name (Movie.nfo, Movie.en.srt) into folder.
    Returns (new video path, [(old, new) for every file moved]). Refuses to overwrite anything."""
    folder.mkdir(parents=True, exist_ok=True)
    stem = new_name or path.stem
    target = folder / f"{stem}{path.suffix}"
    if target.exists() and target.resolve() != path.resolve():
        raise FileExistsError(f"{target.name} is already in {folder}")
    moves = [(path, target)]
    for side in path.parent.iterdir():
        if side == path or not side.is_file():
            continue
        if side.name.startswith(path.stem + "."):
            dest = folder / (stem + side.name[len(path.stem):])
            if not dest.exists():
                moves.append((side, dest))
    done: list[tuple[Path, Path]] = []
    for old, new in moves:
        shutil.move(str(old), str(new))
        done.append((old, new))
    return target, done
