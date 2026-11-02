# Runemark → Stash

A [Stash](https://stashapp.cc) plugin that imports what [Runemark](../../README.md) wrote into your videos. Stash
doesn't read tags embedded in video files on its own, so without it, files you tagged in Runemark arrive in Stash bare.

| Runemark | Stash |
|---|---|
| Title, description, rating | Scene title, details, rating |
| Date (`©day`) | Scene date (full dates; year-only dates only if you turn that on) |
| Tags you typed, and attribute tags such as *Red hair* | Scene tags (created if missing) |
| People | Performers, matched by name or alias (created if missing), with their aliases |
| A person's attributes | The performer field you linked them to in Runemark (*Hair* → hair colour), otherwise a performer custom field (*Marks*: *Freckles*) |
| Chapters | Scene markers with start and end, tagged with the chapter's name ("Missionary"), so they group by name |
| Thumbnail | Scene cover |

It **fills in blanks and never removes anything**: tags, performers and markers are only added, and details Stash
already has are kept unless you turn on *Replace details Stash already has*. Running it again changes nothing new.
It only reads your video files; it never writes to them.

## Install

1. Copy the `runemark` folder (with `runemark.yml`, `runemark_import.py` and `runemark_read.py`) into Stash's
   `plugins` folder (in Docker usually `/root/.stash/plugins`).
2. In Stash: **Settings → Plugins → Reload plugins**.
3. Run **Settings → Tasks → Plugin Tasks → Import from Runemark** once for your existing library. After that, new
   scenes are imported automatically when a scan adds them.

Needs Stash 0.25 or later (tested with 0.31.1) and Python 3.9+; Stash's Docker image has both. No extra Python packages.

## Settings

- **Replace details Stash already has**: off by default.
- **Year-only dates become 1 January**: off by default, so a video Runemark only knows the year of keeps a blank date.
- **Import Runemark's thumbnail as the cover**: on by default, for scenes nobody has edited yet.
- **Add performers Stash doesn't have yet**: on by default.

The data it reads is described in [docs/data-format.md](../../docs/data-format.md).
