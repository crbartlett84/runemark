# Changelog

All notable changes to Runemark. Versions follow [semantic versioning](https://semver.org).

## [Unreleased]

### Added
- **People.** Tag the people in a video from a People box beside the tags (any name or alias works). The **People** window (Ctrl+Shift+P) keeps each person's aliases and values for attributes you define, such as *Hair: Red*, written to their videos as tags (*Red hair*). Values can be limited to some years, and a value that doesn't apply to one video can be marked **Not in this video**.
- **Changing a person updates their videos**, as one undoable change. Only tags that came from profiles change; tags you typed yourself stay, and a tag two people share stays while either is in the video.
- **People travel with the files.** Each video carries a small record of its people, so a library scan on a new PC or after losing the tag database brings them back. The format is documented in `docs/data-format.md` for other tools (a Stash import plugin is planned).
- **Library:** a People column; searching finds videos by their people's names and aliases; the `.nfo` export lists people as actors.
- A self-test check for people.

### Fixed
- Release notes on GitHub were empty for 0.3.0 (the notes script couldn't write "→" on Windows).

## [0.3.0] - 2026-11-01

### Added
- **Undo.** Runemark remembers each file's tags, genre, title, year, description and rating from before every save (the last 10 per file). **More → Undo last save…** steps a file back one save at a time; **Library → More → Undo last bulk change…** reverts a whole bulk change, leaving alone any file you've saved again since. Thumbnails and chapters aren't part of undo.
- **Save and next** (Ctrl+Enter) saves if there's anything to save and opens the next file in the list.
- **Copy and paste tags** (Ctrl+Shift+C / Ctrl+Shift+V). Saving a file also makes its tags ready to paste, so giving the next file the same tags is one key. Pasting adds tags; it doesn't remove any.
- **Library:** add several tags at once (separated by commas); **Enter** opens, **0–5** rates and **T** adds tags to the selected files; the Library reopens with your last search and filter; **Stop scan** stops a scan and keeps what it has read.
- **Updating files after renaming or merging a tag can be cancelled**, and the report lists the files it didn't reach.
- **Leftover working files from an interrupted save or conversion** (a crash, power cut or forced close) are found when the video opens, with an offer to delete them.
- **Unexpected errors are written to the log and shown**, instead of disappearing in the packaged app.

### Changed
- **The Library is much faster with big collections.** Typing a search no longer rebuilds the whole list on every key (with 5,000 videos, from about 180 ms per key to under 3 ms), and scans read each MP4 twice instead of four times and save to the database in batches (about 5× faster here; more on network shares).
- Sorting the Library puts blanks (no year, no rating, …) last in both directions, so "highest rated first" starts with five stars.
- The Library keeps your selection after a bulk change or a rescan, so you can rate a set of files and then tag the same files.

### Fixed
- **Converting an MKV on a network share or a USB drive no longer deletes the original.** Those drives have no Recycle Bin, and Windows deletes instead of recycling there. Runemark now keeps the MKV and tells you.
- **Saving no longer cuts a full date down to the year.** A date such as `2019-07-14T10:00:00Z` is kept unless you change the year. This also applied to bulk rating changes in the Library.
- **Saving no longer overwrites a separate comment.** Explorer's Comments is only updated when it was empty or matched the description.
- **Read-only videos fail straight away with a clear message**, instead of retrying and leaving a full-size working copy behind. Working copies no longer inherit the Read-only flag.
- **Backups in folders whose names contain `#`, `?` or `%`** can be checked and restored.
- Closing the Library during a scan stops the scan, instead of leaving it running against the closed window.
- The Library's "Change files" prompt shows tag names as you typed them ("Add Lake, Summer"), not lower-cased.

## [0.2.0] - 2026-10-22

### Added
- **Chapters.** Mark chapters on exact frames (button or **M**). They're numbered automatically and renumber when one is added or removed. Name a chapter after a tag (which also tags the file) or anything you type. Existing chapters are shown so they can be corrected. MKV conversions keep edited chapters.
- **Title, year, description and star rating** in the Details tab. Explorer shows them as Title, Year, Comments and Rating; Plex and Jellyfin read them too.
- **Thumbnail suggestions.** The Thumbnail tab offers sharp, well-lit, varied frames; double-click one to use it.
- **Library** (Ctrl+B). Lists every video in your folders and every file you've saved. Search by name, title and tags; filter for untagged, no thumbnail, unrated, unconverted MKV or missing files; save searches; add or remove tags and set ratings on many files at once; export `.nfo` files for Jellyfin and Kodi.
- **Backups.** Back up now, choose where the daily backups go (for example OneDrive), restore from a backup (the current database is backed up first), and an optional portable mode that keeps the data beside the app.
- Self-test checks for details and rating, chapters and suggestions.

### Fixed
- The number boxes in Settings showed no value.
- Chapters running past the end of the video no longer make Windows report the wrong length after an MKV conversion.
- The build uses a signed Python from python.org when Windows Smart App Control blocks the downloaded one.

## [0.1.1] - 2026-10-20

### Fixed
- The "Open a video" message was hidden behind the video area on Windows.
- The "Saved" message in the status bar was replaced straight away by "Opened".
- The tag list no longer jumps down when the first tag is added.
- Shift+arrow jumps land on the exact frame 5 seconds away, and filmstrip clicks show the frame pictured.
- Garbled "…" in `build-log.txt`.

## [0.1.0] - 2026-10-19

First release: open MP4 and MKV files, step frame by frame, choose the thumbnail, see file details, tag from a categorised tag database, keep Explorer's Tags, the file's own tags and Genre in sync, and convert MKV to MP4 without re-encoding the video.

[0.3.0]: https://github.com/crbartlett84/runemark/releases/tag/v0.3.0
[0.2.0]: https://github.com/crbartlett84/runemark/releases/tag/v0.2.0
[0.1.1]: https://github.com/crbartlett84/runemark/releases/tag/v0.1.1
[0.1.0]: https://github.com/crbartlett84/runemark/releases/tag/v0.1.0
