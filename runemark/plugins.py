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

# API history:          2 (0.9.0): Tags-tab strips, header buttons, Library columns, keys for one video, toasts,
                         # opening files, names from details, changing a video that isn't open; everything in 1 stays
                         # 3 (0.10.0): save handlers and scan providers; everything in 2 stays
                         # 4 (0.11.0): attribute choices (allowed values with their tags), People buttons, a
                         # person's values read and set, video updates for changed people; everything in 3 stays
API_VERSION = 5          # 5 (0.12.0): Library filters (entries in the Library's filter list); everything in 4 stays
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
class LibraryColumn:
    """A column a plugin adds to the Library (0.9.0). cell(path) gives the text (None: not known yet, so fetch is
    asked for it); fetch(paths) is called, a few at a time, for the rows on show that have no value yet."""
    plugin: str
    title: str
    cell: Callable[[str], str | None]
    fetch: Callable[[list[str]], Any] | None = None
    tooltip: Callable[[str], str | None] | None = None
    sort_key: Callable[[str], Any] | None = None


@dataclass
class LibraryFilter:
    """An entry a plugin adds to the Library's filter list (0.12.0). test(path) -> True (shown), False, or None (not
    known yet: not shown). prepare(paths) is called with every video in view when the filter is picked (and after a
    rescan), so the plugin can find out about all of them; it calls library_column_changed() once it has."""
    plugin: str
    label: str
    test: Callable[[str], bool | None]
    prepare: Callable[[list[str]], Any] | None = None

    @property
    def key(self) -> str:
        return f"plugin:{self.plugin}:{self.label}"


@dataclass
class PluginHost:
    """What the plugins added, for the windows to show. Filled in by load_all()."""
    plugins: list[Plugin] = field(default_factory=list)
    panes: list[tuple[str, str, Any]] = field(default_factory=list)          # (plugin, title, widget)
    actions: list[_Action] = field(default_factory=list)                    # main window ⋯ → Plugins
    library_actions: list[_Action] = field(default_factory=list)            # Library → More → plugin actions
    library_columns: list[LibraryColumn] = field(default_factory=list)      # extra Library columns (0.9.0)
    library_filters: list[LibraryFilter] = field(default_factory=list)      # extra Library filters (0.12.0)
    person_actions: list[_Action] = field(default_factory=list)             # buttons in People (0.11.0)
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
    from . import library, saving
    saving.clear_save_handlers()           # plugins loaded again start from nothing
    library.clear_scan_providers()
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

    def add_tags_panel(self, title: str, widget, collapsed: bool = False):
        """A strip in the Tags tab, under the tag editor, that folds down to its title (0.9.0). Returns the strip
        (strip.set_open(True/False) opens or folds it)."""
        self._host.panes.append((self._plugin.name, title, widget))
        return self._w.add_tags_panel(title, widget, collapsed)

    def add_header_widget(self, widget) -> None:
        """Put a widget (buttons, say) in the header, just before Save (0.9.0). The plugin enables and hides it."""
        self._w.add_header_widget(widget)

    def add_library_column(self, title: str, cell: Callable[[str], str | None],
                           fetch: Callable[[list[str]], Any] | None = None,
                           tooltip: Callable[[str], str | None] | None = None,
                           sort_key: Callable[[str], Any] | None = None) -> None:
        """A column in the Library (0.9.0). cell(path) -> the text to show, or None when it isn't known yet: then
        fetch(paths) is called (on the window's thread, batched) for the rows on show without a value, and the plugin
        calls library_column_changed() once it has them. Keep cell() quick: it's called as rows are drawn."""
        self._host.library_columns.append(LibraryColumn(self._plugin.name, title, cell, fetch, tooltip, sort_key))
        self._w.library_columns_changed()

    def library_column_changed(self) -> None:
        """Values for a plugin column (or filter) have arrived: the Library redraws its plugin columns, and a plugin
        filter that's picked is applied again."""
        self._w.library_columns_changed()

    def add_library_filter(self, label: str, test: Callable[[str], bool | None],
                           prepare: Callable[[list[str]], Any] | None = None) -> None:
        """An entry in the Library's filter list, after Runemark's own (0.12.0): shows the videos test(path) says True
        for. prepare(paths) is called with every video in view when it's picked; call library_column_changed() when
        the answers are in. Keep test() quick: it's called for every video each time the list is filtered."""
        self._host.library_filters.append(LibraryFilter(self._plugin.name, label, test, prepare))
        self._w.library_columns_changed()

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
        """Save the open video (the same as Ctrl+S), then call `then`. From 0.9.0 MP4s are saved in the background:
        `then` runs once the file is written, and the window carries on meanwhile."""
        self._w.save(after=then)

    def saving(self, path: Path | str) -> bool:
        """True while a save of this video is waiting or being written (0.9.0)."""
        return self._w.saving(Path(path))

    def open_file(self, path: Path | str) -> None:
        """Open a video in the editor (added to the open-file list), as if picked in the Library (0.9.0)."""
        self._w.open_paths([Path(path)])

    def open_list(self) -> list[Path]:
        """The open-file list (Ctrl+PgUp / Ctrl+PgDown walk through it), in order (0.9.0)."""
        return list(self._w.queue)

    def open_next(self) -> bool:
        """Open the next video in the open-file list (False when there's no other one) (0.9.0)."""
        return self._w.open_next_in_list()

    def set_chapter_keys(self, keys: dict[str, str] | None, label: str = "") -> None:
        """Number keys 1-9 for the open video only: {"1": "Name", ...}; each adds a chapter with that name, as the
        chapter keys do. Shown under the player (with `label`) and in the Chapters tab. They go when another video
        opens; None or {} gives back the usual chapter keys now (0.9.0)."""
        self._w.set_session_chapter_keys(keys, label)

    def toast(self, text: str, action: str | None = None, callback: Callable[[], Any] | None = None,
              kind: str = "info", seconds: float | None = None) -> None:
        """A message floating over the window, optionally with one button (Undo, Retry…) (0.9.0).
        kind: "info", "warn" or "error" (errors stay until closed)."""
        actions = [(action, lambda: self._w._run_plugin(callback))] if action and callback else None
        self._w.toast(text, kind, actions, seconds)

    def name_from_details(self, details: dict | None = None) -> str:
        """The file name (without extension) Library → Rename from details would give the open video (or these
        details: title, year, people, studio, rating, path), with the pattern last used there. Folders in the
        pattern are left out. "" when the pattern gives nothing (the details it needs are empty) (0.9.0)."""
        return self._w.plugin_name_from_details(details)

    def change_file(self, path: Path | str, changes: dict,
                    then: Callable[[Path | None, str], Any] | None = None) -> None:
        """Change a video that isn't necessarily open, as apply_details would and then saving it: in the background,
        with the same safe write, check and history (Undo last save works on it). changes: what apply_details takes,
        plus cover_if_missing (bytes: a thumbnail only if it has none). then(path, "") when it's written, or
        then(None, reason) (0.9.0)."""
        self._w.plugin_change_file(Path(path), dict(changes), then)

    # ---- saves and scans done elsewhere (3)
    from .library import ScanListing  # noqa: E402  (api.ScanListing)
    from .saving import SaveRequest, SaveResult  # noqa: E402  (api.SaveResult, api.SaveRequest)

    def add_save_handler(self, handler: Callable) -> None:
        """Offer each queued save (the editor's, change_file's, Undo last save's) to handler(request, progress)
        first, in the background, before Runemark writes the file itself (0.10.0). request is a SaveRequest: the
        file, everything the save writes (request.to_dict() as plain data), and the size, modified time and
        fingerprint (0.10.1) it had when it was read (request.expected). progress(percent, message) feeds the status
        bar (-1: busy). Return api.SaveResult.handled(problems, size, mtime, fp) once the file is written (Runemark then reads it back and
        checks it), .not_handled() (or None) to let Runemark save it as usual, or .failed(reason) when it couldn't
        and the file is unchanged: the save then fails with that reason and offers Retry. Raising counts as not
        handled, so only raise before anything was written."""
        from . import saving
        saving.add_save_handler(self._plugin.name, handler)

    def add_scan_provider(self, listing: Callable, thumbnail: Callable[[str], bytes | None] | None = None) -> None:
        """Let Library scans take what's in some folders from the plugin instead of opening the files (0.10.0).
        listing(folders, cancel) runs in the background for each scan: return api.ScanListing(covered, records)
        (or None to cover nothing). covered: the folders (given, or inside them) it lists completely; records: a dict
        per video, {"path", "size", "mtime", + what the scan reads: "container", "tags", "title", "year", "rating",
        "has_cover", "duration", "genre", "data", "studio", "chapters", "chapter_names"}, or {"path", "size",
        "mtime", "unreadable": True} for one Runemark should read itself. The scan still lists the folders itself
        (0.10.1): a new or changed video whose record has the same size and time is imported, any other is read
        here, and only videos that aren't on disk are marked missing. thumbnail(path) gives the
        cover's small JPEG (b"" for none, None: read the file) when the Library shows it."""
        from . import library
        library.add_scan_provider(self._plugin.name, listing, thumbnail)

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

    def add_category(self, path: list[str], genre: bool | None = None) -> list[str]:
        """Make sure a category path exists (["Parent", "Child"]), creating what's missing. genre=True makes the last
        one a genre category (its tags are written to the file's genre field), False makes it an ordinary one, None
        leaves that alone. Returns the path as spelled in the database."""
        db = self._w.db
        parent = None
        out = []
        for name in path:
            found = next((c for c in db.categories() if c.parent_id == parent and c.name.casefold() == name.casefold()),
                         None)
            if found is None:
                found = db.add_category(name, parent)
            parent = found.id
            out.append(found.name)
        if genre is not None and parent is not None:
            db.set_write_genre(parent, bool(genre))
        self._w.tag_editor.refresh_database()
        return out

    def set_tag_category(self, name: str, category: list[str] | None) -> str | None:
        """Move an existing tag into a category path (created if missing), or out of any with None. Returns its
        name, or None when there's no such tag."""
        db = self._w.db
        tag = db.find_tag(name)
        if tag is None:
            return None
        cid = None
        if category:
            path = self.add_category(list(category))
            cid = next(c.id for c in db.categories() if db.category_path(c.id) == path)
        if tag.category_id != cid:
            db.move_tag(tag.id, cid)
            self._w.tag_editor.refresh_database()
        return tag.name

    def tag_rules(self) -> dict[str, list[str]]:
        """{tag name: [the tags it also adds]} for every tag rule."""
        db = self._w.db
        return {db.tag(tid).name: [db.tag(i).name for i in ids] for tid, ids in db._rule_ids()}

    def set_tag_rule(self, name: str, adds: list[str]) -> None:
        """Make a tag also add these tags (its rule is replaced; the tags must exist). An empty list removes it."""
        db = self._w.db
        tag = db.find_tag(name)
        if tag is None:
            raise ValueError(f"There's no tag called “{name}”.")
        ids = []
        for other in adds:
            t = db.find_tag(other)
            if t is None:
                raise ValueError(f"There's no tag called “{other}”.")
            ids.append(t.id)
        db.set_implied(tag.id, ids)
        self._w.tag_editor.refresh_database()

    def clear_tags(self, keep: list[str] = ()) -> dict:
        """Empty the tag database except the tags named in `keep`: every other tag, its rules, and every category
        left empty are removed (a backup is taken first, in the backups folder, restorable from Settings > Backups). Videos aren't changed:
        their tags that are no longer in the database show as unfiled (dashed). Returns
        {"tags_removed", "categories_removed", "backup"} (the backup's path, as text)."""
        import datetime as _dt

        from .paths import default_backup_dir
        db = self._w.db
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = db.backup_to(default_backup_dir() / f"before-clear-{stamp}.db")
        tags, cats = db.clear_tags(list(keep))
        self._w.tag_editor.refresh_database()
        return {"tags_removed": tags, "categories_removed": cats, "backup": str(backup)}

    def rename_tag(self, old: str, new: str) -> str | None:
        """Rename a tag in the database (or merge it into `new` if that exists), and offer to update the videos that
        carry it, exactly like renaming in the Tag database window. Returns the tag's new name, or None."""
        return self._w.plugin_rename_tag(old, new)

    def find_person(self, name: str) -> dict | None:
        """{"name", "aliases", "links", "has_photo", "values"} for the person with this name or alias, or None.
        values (0.11.0): [{"attribute", "value", "tag", "from_year", "to_year"}]."""
        db = self._w.db
        p = db.find_person(name)
        return None if p is None else {"name": p.name, "aliases": list(p.aliases), "links": list(p.links),
                                       "has_photo": db.person_photo(p.id) is not None,
                                       "values": [{"attribute": v.attribute, "value": v.value, "tag": v.tag,
                                                   "from_year": v.from_year, "to_year": v.to_year}
                                                  for v in p.values]}

    def people(self) -> list[str]:
        """Everyone in People, by name (0.11.0)."""
        return [p.name for p in self._w.db.people()]

    def add_person_action(self, text: str, callback: Callable[[str], Any]) -> None:
        """A button in People for the selected person (0.11.0); the callback gets their name."""
        self._host.person_actions.append(_Action(self._plugin.name, text, callback))

    def set_person_values(self, name: str, values: dict, photo: bytes | None = None) -> list[str]:
        """Set a person's values (0.11.0): {"Hair": "Red"} or {"Hair": ("Red", "Red Hair")} replaces what they have
        for each attribute named (None or "" takes it away); attributes are created if missing; an attribute with
        choices takes the allowed value's own tag. photo replaces theirs. Their videos are offered an update (when
        People closes, or right away). Returns the attributes that changed."""
        db = self._w.db
        person = db.find_person(name)
        if person is None:
            raise ValueError(f"{name} isn't in People.")
        changed = []
        for attr_name, value in (values or {}).items():
            value, tag = (value if isinstance(value, (tuple, list)) else (value, None))
            attr = db.find_attribute(attr_name) or db.add_attribute(attr_name)
            had = [v for v in db.person(person.id).values if v.attribute_id == attr.id]
            if value and len(had) == 1 and had[0].value == str(value) and (not tag or had[0].tag == str(tag)):
                continue
            if not value and not had:
                continue
            for v in had:
                db.remove_person_value(v.id)
            if value:
                db.add_person_value(person.id, attr.id, str(value), tag=str(tag) if tag else None)
            changed.append(attr.name)
        if photo:
            jpeg = square_photo(photo)
            if jpeg:
                db.set_person_photo(person.id, jpeg)
                changed.append("photo")
        if changed:
            self._w.people_changed([person.id])
        return changed

    def offer_video_update(self, names: list[str]) -> None:
        """Offer to bring the videos of these people up to date now (0.11.0), as People does when it closes."""
        db = self._w.db
        ids = [p.id for p in (db.find_person(n) for n in names) if p is not None]
        if ids:
            self._w.people_changed(ids, now=True)

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

    def add_attribute(self, name: str, stash_field: str | None = None, as_tag: bool = True,
                      choices: list | None = None) -> str:
        """Add a people attribute field ("Hair"), optionally linked to a Stash performer field ("hair_color") and
        written to videos as tags or not. Does nothing if it exists, except that choices (0.11.0) are set when given:
        the values it allows, each with its tag ([("Red", "Red Hair")...]), shown in People as a drop-down.
        Returns its name."""
        db = self._w.db
        attr = db.find_attribute(name) or db.add_attribute(name, stash_field=stash_field, write_tags=as_tag)
        if choices is not None:
            attr = db.update_attribute(attr.id, choices=list(choices))
        return attr.name

    def set_attribute_choices(self, name: str, choices: list | None) -> None:
        """The values an attribute allows, each with its tag (0.11.0); None or [] lets it take any value."""
        db = self._w.db
        attr = db.find_attribute(name)
        if attr is None:
            raise ValueError(f"There's no attribute called {name}.")
        db.update_attribute(attr.id, choices=list(choices or []))

    def attributes(self) -> list[dict]:
        """People's attribute fields: [{"name", "stash_field", "as_tag", "choices"}]."""
        return [{"name": a.name, "stash_field": a.stash_field, "as_tag": a.write_tags,
                 "choices": [list(c) for c in a.choices]} for a in self._w.db.attributes()]

    # ---- libraries and files
    def libraries(self) -> list[dict]:
        """[{"name", "folders", "skip"}] as set up in the Library (a copy: changing it changes nothing)."""
        return json.loads(json.dumps(self._w.settings.libraries))

    def move_file(self, path: Path | str, folder: Path | str, new_name: str | None = None,
                  then: Callable[[Path | None, str], Any] | None = None, open_after: bool = True,
                  discard_changes: bool = False) -> None:
        """Move a video (and files beside it with the same name: .nfo, subtitles…) into `folder`, optionally
        renamed (new_name without extension). Runemark lets go of it first if it's open (an open video with unsaved
        changes isn't moved, unless discard_changes: 0.12.0, for a reject, where they'd be thrown away anyway), and
        keeps its history and library entry. If it was open it's opened from the new place, or with
        open_after=False the next open video is shown instead. then(new_path, "") when done, or then(None, reason)
        if it couldn't."""
        self._w.plugin_move(Path(path), Path(folder), new_name, then, open_after, discard_changes)

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
