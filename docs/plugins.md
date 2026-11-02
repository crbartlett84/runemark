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

`api` is the plugin API version it was written for. Runemark refuses plugins that need a newer one and says so.

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

## The API (version 1)

### Adding things
| Call | What it does |
|---|---|
| `api.add_pane(title, widget)` | A tab beside Tags, Chapters, Thumbnail and Details showing your QWidget. |
| `api.add_action(text, callback, shortcut=None)` | An entry in **⋯ → Plugins**; `shortcut` like `"Ctrl+Alt+L"`. |
| `api.add_library_action(text, callback)` | An entry in the Library's **More → Plugins**; `callback(paths)` gets the selected videos. |
| `api.on(event, callback)` | `file_opened(path)`, `file_saved(path)` (editor or Library), `file_closed(path)` (before `move_file` moves the open video). |

### The open video
| Call | What it does |
|---|---|
| `api.current_file()` | Its path, or `None`. |
| `api.current_details()` | `{"path", "title", "year", "date", "studio", "description", "rating", "links", "tags", "people", "has_cover", "container", "unsaved"}` as shown now (unsaved edits included), or `None`. `tags` are the hand-typed ones; tags from people's profiles are added when saving. |
| `api.apply_details(**changes)` | Any of `title`, `year`, `date` (`"YYYY-MM-DD"`, sets the year too), `studio`, `description`, `rating` (0–5), `links` (replaces), `add_links`, `add_tags` (names; spelled as in the database when they exist there), `people` (names or aliases; new names are added to People), `cover` (JPEG or PNG bytes). Returns what changed. |
| `api.save(then=None)` | Save, like Ctrl+S, then call `then()`. |

### Your database
| Call | What it does |
|---|---|
| `api.tags()` | `[{"name", "category": ["Parent", "Child"]}]`. |
| `api.find_tag(name)` | The tag as spelled in the database (case doesn't matter), or `None`. |
| `api.categories()` | Every category as a path, `["Parent", "Child"]`. |
| `api.add_tag(name, category=None)` | Add a tag (in an existing category path); returns its name. Does nothing if it exists. |
| `api.find_person(name)` | `{"name", "aliases", "links", "has_photo"}` by name or alias, or `None`. |
| `api.add_person(name, aliases=(), links=(), photo=None, values=None)` | Add someone, or fill in what their profile lacks: new aliases, links, a photo if they have none (any picture; the top-centre square is used), attribute values they don't have yet (`{"Hair": "Red"}`; attributes are created if missing). Returns their main name. |
| `api.attributes()` | People's attribute fields: `[{"name", "stash_field", "as_tag"}]`. |

### Libraries and files
| Call | What it does |
|---|---|
| `api.libraries()` | `[{"name", "folders", "skip"}]` as set up in the Library (a copy). |
| `api.move_file(path, folder, new_name=None, then=None)` | Move a video, and the files beside it with the same name (`.nfo`, subtitles), into `folder`, optionally renamed. Runemark lets go of it first if it's open (an open video with unsaved changes isn't moved), keeps its history and library entry, and reopens it from the new place. Never overwrites. `then(new_path, "")`, or `then(None, reason)`. |

### Settings and messages
| Call | What it does |
|---|---|
| `api.settings` | A dict for the plugin's own settings, kept with Runemark's; call `api.save_settings()` after changing it. |
| `api.status(text)` | A message in the status bar. |
| `api.run_in_background(work, done=None, failed=None)` | `work()` off the window's thread; then `done(result)` or `failed(exc)` on it. |
| `api.folder`, `api.runemark_version`, `api.api_version` | The plugin's folder; Runemark's version; the API version. |
