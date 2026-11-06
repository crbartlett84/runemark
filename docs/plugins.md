# Writing a Runemark plugin

A plugin adds features to Runemark without changing Runemark itself. It's a folder with two files, placed in
Runemark's plugins folder (**⋯ → Manage plugins… → Open plugins folder**):

```
plugins/
  my-plugin/
    plugin.json
    plugin.py
```

**plugin.json**

```json
{"name": "My plugin", "version": "1.0", "description": "What it does, in a line.", "api": 1}
```

`api` is the plugin API version it was written for (`3` for the calls marked *3* below, Runemark 0.10.0+; `2` for
those marked *2*, Runemark 0.9.0+; `1` otherwise). Runemark refuses plugins that need a newer one and says so.

**plugin.py** defines `register(api)`, called once when Runemark starts:

```python
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget


def register(api):
    pane = QWidget()
    layout = QVBoxLayout(pane)
    label = QLabel("Open a video")
    layout.addWidget(label)
    button = QPushButton("Add the tag “Reviewed”")
    button.clicked.connect(lambda: api.apply_details(add_tags=["Reviewed"]))
    layout.addWidget(button)
    layout.addStretch(1)
    api.add_pane("Example", pane)
    api.on("file_opened", lambda path: label.setText(path.name))
```

Restart Runemark and the pane appears beside Details. Plugins can use Python's standard library and PySide6 (Qt),
which come with Runemark. They can't install other packages; put any pure-Python helper modules in the plugin's own
folder and import them from `plugin.py` (the folder is on the import path while it loads).

## Rules

- **Edits go through the editor.** `apply_details` changes the open video exactly as typing would: it shows as
  unsaved and nothing reaches the file until the user saves (or the plugin calls `api.save()`).
- **Long work goes in the background.** Network calls and big file reads go through `api.run_in_background`, so the
  window stays responsive; its `done` callback runs back on the window's thread and can update widgets.
- **Errors are contained.** A plugin that fails to load is listed with its error in Manage plugins. An action or
  event handler that raises shows or logs the error; Runemark carries on.
- **Trust.** Plugins run with the same access to your files as Runemark. Only install plugins you trust.

## The API (version 3)

Everything from versions 1 and 2 is still there. Calls new in version 2 (Runemark 0.9.0) are marked *2*, those new in
version 3 (Runemark 0.10.0) *3*.

### Adding things
| Call | What it does |
|---|---|
| `api.add_pane(title, widget)` | A tab beside Tags, Chapters, Thumbnail and Details showing your QWidget. |
| `api.add_action(text, callback, shortcut=None)` | An entry in **⋯ → Plugins**; `shortcut` like `"Ctrl+Alt+L"`. |
| `api.add_library_action(text, callback)` | An entry in the Library's **More → Plugins**; `callback(paths)` gets the selected videos. |
| `api.add_tags_panel(title, widget, collapsed=False)` *2* | A strip in the **Tags** tab, under the tag editor, that folds down to its title. Returns the strip (`strip.set_open(True)` opens it). |
| `api.add_header_widget(widget)` *2* | Your widget (buttons, say) in the header just before **Save**. Enable, disable and hide it yourself. |
| `api.add_library_column(title, cell, fetch=None, tooltip=None, sort_key=None)` *2* | A column in the Library. `cell(path)` returns its text, or `None` when you don't know it yet: then `fetch(paths)` is called (batched, on the window's thread) for the rows on show without a value. Fetch in the background and call `api.library_column_changed()` when the values are in. Keep `cell` quick. |
| `api.on(event, callback)` | `file_opened(path)`, `file_saved(path)` (editor or Library), `file_closed(path)` (before `move_file` moves the open video). |

### The open video
| Call | What it does |
|---|---|
| `api.current_file()` | Its path, or `None`. |
| `api.current_details()` | `{"path", "title", "year", "date", "studio", "description", "rating", "links", "tags", "people", "has_cover", "container", "unsaved"}` as shown now (unsaved edits included), or `None`. `tags` are the hand-typed ones; tags from people's profiles are added when saving. |
| `api.apply_details(**changes)` | Any of `title`, `year`, `date` (`"YYYY-MM-DD"`, sets the year too), `studio`, `description`, `rating` (0–5), `links` (replaces), `add_links`, `add_tags` (names; spelled as in the database when they exist there), `people` (names or aliases; new names are added to People), `cover` (JPEG or PNG bytes). Returns what changed. |
| `api.save(then=None)` | Save, like Ctrl+S, then call `then()`. From 0.9.0 MP4s are saved in the background: `then` runs once the file is written, while the window carries on. |
| `api.saving(path)` *2* | True while a save of that video is waiting or being written. (`move_file` and `change_file` wait for it by themselves.) |
| `api.set_chapter_keys(keys, label="")` *2* | Number keys 1–9 for the open video only, `{"1": "Name", …}`: each adds a chapter with that name. Shown under the player (with `label`) and in the Chapters tab; they go when another video opens, or with `None`. |
| `api.name_from_details(details=None)` *2* | The file name (no extension) **Rename from details** would give the open video (or these details), using the pattern last used there. Folders in the pattern are left out; `""` when it gives nothing. |
| `api.open_file(path)` / `api.open_next()` / `api.open_list()` *2* | Open a video (added to the open-file list); open the next one in the list (`False` if there isn't one); the list itself. |

### Your database
| Call | What it does |
|---|---|
| `api.tags()` | `[{"name", "category": ["Parent", "Child"]}]`. |
| `api.find_tag(name)` | The tag as spelled in the database (case doesn't matter), or `None`. |
| `api.categories()` | Every category as a path, `["Parent", "Child"]`. |
| `api.add_category(path, genre=None)` | Make sure a category path exists, creating what's missing; `genre=True` makes the last one a genre category (its tags go to the file's genre field). Returns the path as spelled. |
| `api.set_tag_category(name, category)` | Move an existing tag into a category path (created if missing), or out of any with `None`. |
| `api.tag_rules()` / `api.set_tag_rule(name, adds)` | Read tag rules (`{tag: [tags it also adds]}`), or set one tag's rule (an empty list removes it). |
| `api.add_tag(name, category=None)` | Add a tag (in an existing category path); returns its name. Does nothing if it exists. |
| `api.clear_tags(keep)` | Remove every tag except those named in `keep`, their rules, and every category left empty, after a backup (`before-clear-….db`, restorable from Settings → Backups). Videos aren't changed. Returns `{"tags_removed", "categories_removed", "backup"}`. |
| `api.rename_tag(old, new)` | Rename a tag (or merge it into an existing `new`), then offer to update the videos that carry it, like renaming in the Tag database. Returns the new name. |
| `api.find_person(name)` | `{"name", "aliases", "links", "has_photo"}` by name or alias, or `None`. |
| `api.add_person(name, aliases=(), links=(), photo=None, values=None)` | Add someone, or fill in what their profile lacks: new aliases, links, a photo if they have none (any picture; the top-centre square is used), attribute values they don't have yet (`{"Hair": "Red"}`, or `{"Hair": ("Red", "Red hair")}` to choose the tag it becomes; attributes are created if missing). Returns their main name. |
| `api.add_attribute(name, stash_field=None, as_tag=True)` | Add a people attribute field, optionally linked to a Stash performer field and written as tags or not. Returns its name. |
| `api.attributes()` | People's attribute fields: `[{"name", "stash_field", "as_tag"}]`. |

### Libraries and files
| Call | What it does |
|---|---|
| `api.libraries()` | `[{"name", "folders", "skip"}]` as set up in the Library (a copy). |
| `api.upgrade_copy(curated, better, old_to=None, then=None)` | Put `better` (a sharper or more complete copy of the same video) in `curated`'s place, same folder and name, with all of curated's tags, people, details, links, thumbnail and chapters (moved to the matching moments). Runemark compares the two, shows the user what will happen and asks first; it refuses two different videos. The curated copy goes to the `old_to` folder, or the Recycle Bin. `then(final_path, "")`, or `then(None, reason)`. |
| `api.video_info(path)` | `{"width", "height", "duration", "bit_rate", "size", "container"}` of any video. |
| `api.change_file(path, changes, then=None)` *2* | Change a video that needn't be open, then save it, in the background with the same safe write, check and history as the editor (Undo last save works). `changes` are what `apply_details` takes, plus `cover_if_missing` (a thumbnail only if it has none). `then(path, "")` once written, or `then(None, reason)`. |
| `api.move_file(path, folder, new_name=None, then=None, open_after=True)` | Move a video, and the files beside it with the same name (`.nfo`, subtitles), into `folder`, optionally renamed. Runemark lets go of it first if it's open (an open video with unsaved changes isn't moved) and keeps its history and library entry. If it was open, it's reopened from the new place, or with `open_after=False` the next open video is shown. Never overwrites. While the video is still being saved, the move waits for the save (and doesn't happen if the save fails). `then(new_path, "")`, or `then(None, reason)`. |

### Saves and scans done elsewhere *3*
For a library on a network share, a plugin can hand the heavy work to a program on the computer that holds the files,
so only the changes and the results cross the network.

| Call | What it does |
|---|---|
| `api.add_save_handler(handler)` *3* | Each queued save (the editor's, `change_file`'s and **Undo last save**'s) is offered to `handler(request, progress)` first, in the background, before Runemark writes the file itself. Several handlers are asked in the order they were added, until one answers handled or failed. |
| `api.add_scan_provider(listing, thumbnail=None)` *3* | Library scans ask `listing(folders, cancel)` (in the background) for what's in the library folders, instead of opening the files there. `thumbnail(path)` gives a covered video's small cover picture when the Library shows it. |
| `api.SaveResult`, `api.SaveRequest`, `api.ScanListing` *3* | The classes below (also `runemark.saving.SaveResult`, `runemark.saving.SaveRequest`, `runemark.library.ScanListing`). |

**The save request.** `request.path` (the video), `request.keywords`, `request.genre`, `request.cover` (`UNCHANGED`,
image bytes, or `None` to remove it), `request.meta` (`UNCHANGED` or the details: title, year, description, rating,
studio, links, date), `request.data` (Runemark's data block, see [data-format.md](data-format.md); `UNCHANGED`, or
`None` to remove it), `request.chapters` (`None`: the chapter track stays; a list: the file is written again with these
chapters, ends included, and cover, details and data are then the complete values), `request.preserve_mtime`,
`request.kind` (`"save"` or `"undo"`) and `request.expected` (`{"size", "mtime"}`: the file as it was when it was
read; if it isn't like that any more, someone else changed it). `request.to_dict()` is all of it except the path and
`expected` as plain JSON-safe data (cover as base64; a missing key means unchanged), and
`SaveRequest.from_dict(path, changes, expected)` turns it back. `request.changed_fields()` names what it writes,
without values, for a log.

**What the handler returns.**
- `api.SaveResult.handled(problems=[], size=None, mtime=None)`: the file is written. Runemark reads it back and checks
  it exactly as after its own saves (problems found either way come up as the usual "saved, but…" warning), records
  the save history (chapters included, so **Undo last save** works), and opens the video again. Give the file's new
  `size` and `mtime`: a network share can report the old ones for a few seconds, and Runemark uses yours meanwhile.
- `api.SaveResult.not_handled(reason="")`, `None`, or raising: Runemark saves the file itself, exactly as without the
  plugin. Raise only before anything was written.
- `api.SaveResult.failed(reason)`: the file wasn't changed and shouldn't be saved here either (it changed since it was
  opened, say). The save fails with the usual error and **Retry**; the edits stay unsaved.

`progress(percent, message)` shows in the status bar (`percent` -1 for "busy"). The rest stays as it is for every
save: one at a time from the queue, a video being saved is read-only and blocks Library and plugin changes to it, and
closing waits for it.

**Writing a save somewhere else.** Wherever Runemark's package can be imported (it needs mutagen, Pillow and FFmpeg;
not Qt), `runemark.saving.write_copy(request, source, dest)` writes the saved version of `source` to `dest` (an
`.mp4` in the same folder is best) and leaves `source` alone: the same code as a local save, so the result is what
Runemark would have written (Explorer's Tags by Runemark's own writer, as on any system without Windows'). It returns
the problems its read-back found; `runemark.saving.verify(request, path)` checks a file against a request. Swapping
the copy in, keeping the modified date when `preserve_mtime` is set, and checking `expected` first are up to the
handler.

**The scan listing.** `listing(folders, cancel)` returns `api.ScanListing(covered, records)`, or `None` to cover
nothing (a failure counts as nothing too: the scan goes on as usual). `covered`: the folders, among the ones asked for
or inside them, the listing describes completely; those aren't walked. `records`: one dict per video under them,
`{"path", "size", "mtime"}` plus what a scan reads from a file (`runemark.library.read_for_index` gives exactly these):
`"container", "tags", "title", "year", "rating", "has_cover", "duration", "genre", "data", "studio", "chapters"` (the
count, or a list of `{"start", "end", "title"}`) and `"chapter_names"`. A record with `"unreadable": True` (and only
path, size and mtime) is read by Runemark itself. `api.ScanListing(covered, records, not_covered)`: folders inside
the covered ones that the listing leaves out (a folder that isn't yours to list, say) are scanned by Runemark as
usual. The scan imports new and changed videos (by size and modified time)
without opening them, marks videos it had under a covered folder that the listing no longer has as missing, leaves out
folders the library skips, and scans every folder that isn't covered as before. Paths are as Runemark sees them (the
library's own folders), not as the other computer does.

**Thumbnails.** In the Library's grid, a covered video's cover comes from `thumbnail(path)` when it's shown: a small
JPEG (`runemark.covers.small_jpeg(cover)` makes the same one Runemark would), `b""` when it has none, or `None` to let
Runemark read the file. It's kept in Runemark's cover cache like the others.

### Settings and messages
| Call | What it does |
|---|---|
| `api.settings` | A dict for the plugin's own settings, kept with Runemark's; call `api.save_settings()` after changing it. |
| `api.status(text)` | A message in the status bar. |
| `api.toast(text, action=None, callback=None, kind="info", seconds=None)` *2* | A message floating over the window, with one optional button (`"Undo"`, `"Retry"`…) that calls `callback`. `kind` `"warn"` or `"error"` (errors stay until closed). |
| `api.run_in_background(work, done=None, failed=None)` | `work()` off the window's thread; then `done(result)` or `failed(exc)` on it. |
| `api.folder`, `api.runemark_version`, `api.api_version` | The plugin's folder; Runemark's version; the API version. |
