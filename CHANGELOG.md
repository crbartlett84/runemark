# Changelog

All notable changes to Runemark. Versions follow [semantic versioning](https://semver.org).

## [Unreleased]

## [0.11.0] - 2026-11-06

### Changed
- **The first chapter you place is the only chapter.** Adding a chapter no longer adds a "Chapter 1" at 0:00 as
  well (it got in the way of skipping to the action and was deleted by hand every time). MP4 chapter lists must start
  at 0:00, so when the first chapter starts later Runemark writes a lead-in called "Start" before it; Runemark never
  shows it, doesn't count it in the Library, and the Stash plugin (0.3.1) doesn't make a marker of it. A chapter you
  put at 0:00 yourself stays a chapter.
- **Attributes in People are picked from a list.** The Attribute box is a drop-down of your attributes (new ones are
  made in Attributes…). An attribute can have choices, the values it allows, each with its own tag ("Red" gives
  "Red Hair"): its value is then a drop-down too, and the tag follows the value. A value that isn't in the list (from
  an older file) is shown in red in its row with the list to pick a replacement from. Attributes… shows each
  attribute's values.

### Added
- **Plugin API 4:** attribute choices (`add_attribute(..., choices=)`, `set_attribute_choices()`, `attributes()`
  lists them); buttons in People for the selected person (`add_person_action()`); a person's values in
  `find_person()`, `people()`, and `set_person_values()` to set or take away values (and a photo), whose videos are
  offered an update when People closes or on `offer_video_update()`.

## [0.10.2] - 2026-11-06

### Fixed
- **A save written by a save handler says where in the status bar.** A handler's progress line "Saving on X…" (or
  "Saving on X: step") was dropped, so the status bar showed only "Saving a.mp4… 42%". It now reads
  "Saving a.mp4 on X… 42%", and keeps saying so while the handler reports only percentages. Runemark's own saves read
  as before.
- **Details filled in by themselves from a .nfo aren't shown as unsaved changes on their own.** Leaving a video
  already didn't ask about them (they're filled in again when it opens), but the unsaved-changes dot and the • in the
  title said there were changes, so moving on looked like losing them. The dot and the • now show only changes worth
  asking about; the fill-in is still saved with the next save. Plugins get `own_changes` in `current_details()` beside
  `unsaved`.

## [0.10.1] - 2026-11-06

### Fixed
- **Scans through a scan provider no longer mark existing videos missing.** 0.10.0 trusted the provider's listing
  alone, so a video it hadn't caught up with yet (just added, just moved in, just changed) showed as missing. The scan
  now lists the folders itself (names, sizes and times only) and imports a video from the provider's record only when
  the record matches the file on disk (same size, modified time within 2 seconds); any other new or changed video is
  read here. Only videos that aren't there are marked missing.
- **Saves handed to a save handler carry a fingerprint** of the file as it was opened (`request.expected["fp"]`, the
  sha256 of its first and last MiB; `runemark.saving.fingerprint()`), so a handler can tell whether the file changed
  without relying on modified times, which a network share may round and "keep the modified date" leaves alone.
  `SaveResult.handled()` takes the new file's `fp` too.
- `file_saved` is now sent for every video the Library writes itself (bulk changes, undo, **Set details**) and for
  People updates, not only the editor's saves, so a plugin hears about every change.

## [0.10.0] - 2026-11-06

### Added
- **Plugin API 3: save handlers.** A plugin can offer to write each queued save itself (the editor's saves,
  `change_file` and **Undo last save**): it's given the file, everything the save writes and the size and modified
  time the file had when it was opened, and answers handled, not handled or failed. Not handled (or a plugin that
  isn't reachable) means Runemark saves it as before; failed means the usual error with **Retry**, the edits kept and
  the file unchanged. Handled saves are read back and checked like Runemark's own, go into the same save history
  (chapters included), and keep the background save queue's rules. `runemark.saving.write_copy` writes the saved
  version of a video to another file with Runemark's own writer, so a handler elsewhere produces what a local save
  would. See [docs/plugins.md](docs/plugins.md).
- **Plugin API 3: scan providers.** A plugin can list what's in some library folders for a scan, so the files there
  aren't opened: new and changed videos (by size and modified time) are imported from its records, videos it no
  longer lists are marked missing, files it can't read and folders it doesn't cover are scanned as before. The
  Library's thumbnails for those videos come from the plugin when they're shown.
- Plugins written for API 2 (0.9.x) keep working.

### Changed
- **Undo last save** stops the chapter pictures, suggested cuts and timeline preview reading the video before it
  writes, as saving does, so nothing holds the file while it's replaced.

## [0.9.2] - 2026-11-06

### Added
- **Nudge buttons** under the player: −10 −5 −1 −½ | +½ +1 +5 +10 seconds. They work playing or paused, keep the
  J/K/L speed, never take the keyboard from the window, and their tooltips show the matching key. Jumps are exact
  (no snapping to suggested cuts), stop at the start and the last frame, and the time shows the new spot at once.
- **Nudge keys:** Alt+← → ½ second, Ctrl+Shift+← → 1 second, Shift+← → 5 seconds (as before), Ctrl+← → 10 seconds.
  Like the other player keys, they do nothing while you're typing in a text box. ← → alone and , . still step one
  frame, so the 1-second step is on Ctrl+Shift+← → rather than the plain arrows.
- **Settings → Nudge steps:** the four sizes (½, 1, 5 and 10 seconds to start with); the buttons relabel to match.

### Changed
- Frame stepping (, . and ← →) uses the file's own frame times as before; when they can't be read it now steps by
  the video's frame rate (1/30 s if that isn't known either) instead of not moving.

## [0.9.1] - 2026-11-05

### Fixed
- A video opened again while its save is still being written (shown read-only) showed the length and position of
  the video played before it. It now shows its own length.
- After **K**, the speed button still showed the old speed (say 2×). K now puts the speed back to 1× and the button
  shows 1×: the next J or L starts from 1× anyway, and Space plays on at the speed shown.
- Smart filters: the first click on **Add a rule** (or any other button) right after typing a value did nothing,
  as it only closed the list of suggestions. It now closes the list and does what was clicked.

### Changed
- **Find cuts** is about 10-20% faster (it skips the decoder's deblocking, which makes no difference to the small
  pictures it compares) and finds the same cuts. Decoding the whole video is still most of the time it takes.

## [0.9.0] - 2026-11-05

### Added
- **Saving in the background.** Saving no longer stops everything behind a progress box (chapters on a big file
  took the best part of a minute). Saves wait their turn and are written one at a time, with the same safe write and
  read-back check as before, while you carry on; the status bar shows how far the save has got and how many are
  waiting. **Ctrl+Enter** opens the next video straight away while the previous one saves. A save that fails changes
  nothing in the file: its changes come back as unsaved and a message offers **Retry**. A video opened again while it's
  still saving is shown read-only until it's written, then opens normally. Closing Runemark with saves still running
  says so and closes once they're done. (Converting an MKV still shows its progress box, as before.)
- **Chapter ticks on the timeline**, where each chapter starts (chapter ends never show there; they stay in the
  Chapters tab's *Ends at* column). **Hover over the timeline** for a small picture of that moment.
- **Suggested cut points.** *Find cuts* in the Chapters tab looks through the video in the background for places
  where the picture changes completely, and remembers them for that video (saving its tags or chapters keeps them).
  **[** and **]** jump to the previous and next cut, and a chapter added (M or a number key) within a second of a
  cut starts exactly on it. The cuts show as faint dots under the timeline (Settings can hide them, or make Runemark
  look as each video opens). Opening a video never waits for this.
- **Playback speed and J/K/L.** A speed button beside the timeline (1×, 1.5×, 2×, 4×). **L** plays, and each L again
  goes faster; **K** pauses; **J** goes backward (by stepping back through the video, as the player can't play in
  reverse), and each J again goes back faster. The speed button shows the current speed.
- **Undo and redo for chapters**: **Ctrl+Z** and **Ctrl+Y** (outside text boxes) step through every change to the
  chapters since the video was opened in this session, saves included. **Undo last save** now puts a save's chapters
  back too, for saves made from 0.9.0 on.
- **Smart filters in the Library**: saved filters with rules (tag, person, studio, name or title, rating, year,
  genre, thumbnail, chapters, people, saved, inbox, MKV), matched all together or any of them. Pinned ones sit above
  the list with a live count of the videos each matches; click one to show just those.
- **Plugins (API 2)**: strips in the Tags tab, buttons beside Save, Library columns, number keys for one video,
  floating messages with a button, opening videos, the *Rename from details* name, and changing a video that isn't
  open (saved in the background, undoable). See docs/plugins.md.

### Changed
- **The Library opens maximised** (make it smaller and it remembers), and lists each video by its title when it has
  one; the file name has its own column.
- Short notices float over the bottom of the window for a few seconds instead of only changing the status bar.

### Fixed
- **The Library's filter list** (Inbox, Still to do…) was cut off; it now opens wide enough for every entry.

## [0.8.3] - 2026-11-04

### Fixed
- **Player keys after clicking a list.** Space, the arrow keys, Home, M, E and 1-9 stopped working once you clicked a chapter (the list kept the keyboard). Lists now hand them to the player; text boxes, spin boxes and a chapter name being edited still keep them.
- **One length everywhere.** The Library showed the audio track's length (sometimes a few seconds off); it now reads the video's own length, as the player and Details do, and the player's total time uses the same value.
- **Explorer Tags note after a save** says when Windows couldn't write the Tags and Runemark's own writer did instead (the reason is in the log), rather than "written by Runemark" with no explanation.
- **Edits made while a save runs are kept.** A chapter renamed (or a tag, detail or person changed) while the file was saving or opening again was dropped when the saved file reopened, so the next save wrote the old name back ("Chapter 2"). Those edits now carry over to the reopened file, unsaved, and the status bar says to save again.
- **End here (E) ends the chapter you're in.** Straight after adding a chapter and moving on (Shift+→, a frame step), E could end the *previous* chapter at the new one's start. It now always ends the chapter under the playhead, and the playhead's time is the one you moved to even before the new frame has shown. On a chapter's very first frame, E says to move to where it should end.
- **Details from .nfo files** no longer make switching videos or closing Runemark ask "Save changes?" when the only changes are the ones filled in from the `.nfo`. The rule: details filled in from a `.nfo` stay pending, never lost. They aren't written until you save, and they're filled in again every time the video is opened, until Runemark has saved it. Anything you change yourself on top of them is an unsaved change as before, and closing or switching asks about it.
- **.nfo ids of 0** (or empty) are skipped: no more links to an id-0 page.
- **Health check summary** counts the videos that have something to fix, out of the videos checked.
- **Chapter buttons** wrap onto a second line in a narrow window instead of being cut off.

## [0.8.2] - 2026-11-03

### Added
- **Chapter ends.** *End here* in the Chapters tab (or **E**) marks where the chapter you're in stops, for when filler follows it before the next chapter. The end isn't a chapter: it's kept with the video's details (Runemark's data block), so players' timelines and chapter lists stay exactly as they were. The chapter list shows it under *Ends at* (click it to jump there); *Clear end* removes it. Ends move with the chapters on *Upgrade to a better copy* and *Save space*, and survive changes to people, file-name details and duplicates merges.
- **Stash plugin 0.3.0** sends each chapter's end as its marker's end (otherwise the next chapter's start, as before), and brings markers it made earlier up to date when an end is added. RuneStash's moments and chapter playlists stop at the end.

## [0.8.1] - 2026-11-03

### Changed
- **Libraries are kept apart.** The library picker in the Library window is always there: each library by name, *All libraries* (only files inside one of them), and *Everything else*, for videos you saved from other folders (Downloads, a USB stick…), which no longer show up in your libraries. *New library…* is at the bottom of the picker, and a new library is shown as soon as it has its folder.
- **Opening a video from the Library** puts the Library aside and brings the main window forward with the video loaded. The Library comes back as you left it (same search and selection; a scan keeps running).

### Fixed
- **Timeline:** a click anywhere on the line jumps straight there (it used to move only a step towards the click), and you can keep holding to scrub from that spot.

## [0.8.0] - 2026-11-03

### Added
- **Save space** (Library → More → *Save space (re-encode to HEVC)…*): lists the selected videos (or all shown) that would get much smaller as HEVC, with the size now, about after and the saving, ticked and totalled. *Re-encode* rewrites them one at a time with the fastest encoder that works on the PC (Intel Quick Sync, NVIDIA, AMD, else x265), keeps every tag, person, detail, thumbnail and chapter and the same file name, and sends each original to the Recycle Bin only after its new copy checks out (HEVC, same length, smaller). Stop any time between videos; the history says "Re-encoded to save space".

## [0.7.5] - 2026-11-03

### Added
- **Still to do** in the Library's filter: videos you've already saved (or marked done) that are still missing something — tags, a genre, people, a thumbnail, chapters or a studio. The *Still to do* column says what, and sorting by it puts the most gaps first. Genre now counts in that column too.

## [0.7.4] - 2026-11-03

### Fixed
- **Updates failed to install** ("it is in use"). The script that swaps in the new version was started with Runemark's own folder as its working folder (where shortcuts start Runemark), which locks that folder against being renamed. It now starts from the temp folder, waits for anything still running from the app folder, and retries for a few seconds if Windows is still holding the folder. Updating *to* 0.7.4 still uses your current version's script: start Runemark from another folder for that one update (or install 0.7.4 by hand); updates after that work normally.

### Changed
- **Backups go to Documents\Backups\Runemark** unless you've chosen a folder (Settings → Backups). When OneDrive backs up your Documents, that puts them in OneDrive. Portable mode still keeps them beside the app. Older backups stay in the data folder's `backups`, and Restore can open them from there.

## [0.7.3] - 2026-11-03

### Added
- Plugins can empty the tag database except the tags they name (`api.clear_tags(keep)`), for starting over from a tag plan. A backup is taken first (restore it from Settings → Backups); videos aren't changed, and their tags that are no longer in the database show as unfiled.

## [0.7.2] - 2026-11-03

### Added
- Plugins can create category paths (and make one a genre category), move tags between categories, and read and set tag rules (`api.add_category`, `api.set_tag_category`, `api.tag_rules`, `api.set_tag_rule`).

## [0.7.1] - 2026-11-03

### Added
- **Upgrade to a better copy** (Duplicates → *Upgrade this one to the one to keep…*). The better copy takes the curated one's place, same folder and name, with all its tags, people, details, links, thumbnail and chapters. Chapters move to the same moments in the new copy (found by lining the two videos up), so a longer intro or a trimmed ending doesn't put them in the wrong place; chapters whose moment isn't in the new copy are named before anything changes. An MKV becomes an MP4. The old copy goes to the Recycle Bin (where there is none, it's kept, renamed "(replaced)"). Plugins can do the same (`api.upgrade_copy`) and read a video's resolution and bitrate (`api.video_info`).

## [0.7.0] - 2026-11-02

### Added
- **Libraries.** The Library can hold several named libraries, each with its own folders and subfolders it leaves out (**Folders → New library…**, **Leave out a subfolder…**). Pick which one to show at the top of the Library; one video can be in more than one. Your existing folders become a library called "Library".
- **Links.** A **Links** box in Details for web addresses about a video (where it came from, its page in an online database), written into the file (`URL`, one value each) and filled in from a `.nfo`'s `<uniqueid>` entries. People get **Links** too, carried in their videos' data block.
- **Full release dates.** The Year box also takes a date (2019-07-14), written to `©day`; a `.nfo`'s premiered date is used too.
- **Plugins** (⋯ → Manage plugins…). Add features without changing Runemark: a pane beside Details, actions in the ⋯ menu and the Library, things that happen when a video opens or is saved. See [docs/plugins.md](docs/plugins.md).
- **Stash plugin 0.2.0:** links become the scene's URLs, and StashDB pages link the scene and performers to those entries; performers are found by those links first; full dates are imported; with *Add performers* off, people Stash doesn't have stay as tags so they can still be found.

## [0.6.0] - 2026-11-02

### Added
- **Tag keys:** Alt+1–9 add a chosen tag to the open video, or remove it. Right-click a tag → Put on a key, or ⋯ → Tag keys….
- **Tag rules** (Tag database → Also adds…): a tag brings others with it (*Cowgirl* also adds *Sex*), in the editor, tag keys, chapter names and Library bulk changes. Included in exports.
- **Command palette** (Ctrl+K): type to find any action, tag, person or video.
- **People photos:** crop one from the frame on screen (right-click a person beside the video) or from a picture (People window). Shown beside their name, carried in their videos' data block, and used by the Stash plugin for performers without a picture.
- **Chapter thumbnails:** each chapter shows its first frame.
- **Library:** **Details…** sets studio and year and adds people on many videos at once (undoable); a **Grid** of thumbnails beside the list; **Health check** (tags out of sync, people's tags out of date, unused `.nfo` details, no thumbnail, missing files) with fixes; **Stats**.
- **Scan on start:** new videos reach the inbox without a manual scan.
- **Updates:** a daily check for a new version, and **Update now**: download, check against GitHub's checksum, swap in on close, keep the previous version, roll back if the swap fails.
- **Activity** (⋯ → Activity…): what was saved, changed in bulk, undone, renamed or recycled, and when.

## [0.5.0] - 2026-11-02

### Added
- **Inbox** (Library filter). Videos the Library has found that you haven't saved in Runemark yet, so new arrivals are easy to work through: open one and the rest of the inbox follows, so **Ctrl+Enter** (Save and next) goes straight through it. **D** or **More → Mark as done** takes a video out without saving. A **Still to do** column shows what each video is missing (tags, people, thumbnail, chapters, studio, converting).
- **Studio.** A Studio field in the Details tab, with suggestions from your library. Written to the file (`STUDIO`), shown and searchable in the Library, in `.nfo` exports, as `{Studio}` in file-name patterns, and filled in from duplicates when merging.
- **Details from .nfo files.** Opening a video Runemark hasn't saved yet fills its empty title, year, studio, description and people from the `.nfo` beside it (the Kodi format Radarr, Sonarr and Jellyfin write); nothing is saved until you save. **Library → More → Read details from .nfo files…** does many at once, undoably. Can be turned off in Settings.
- **Chapter keys.** **1**–**9** add a chapter at the frame on screen with a name you've set for that key (**Chapters → Keys…**, or right-click a chapter → **Put on a key**).
- **Chapter name suggestions.** Naming a chapter offers the names you've used before first, starting with those used in videos with the same people or studio.
- **Stash plugin** (`integrations/stash`): imports the tags, details, studio, people (as performers, with their attributes) and chapters (as markers named by tag) that Runemark wrote into your videos. It fills blanks and never removes anything, and runs by itself when a Stash scan adds a video.
- **Find duplicates** (Library → More). Finds copies of a video even when re-encoded, resized, converted, letterboxed, trimmed, with scenes cut out or reordered, and says how each copy differs ("Missing 2:40 at 12:30"). Suggests the best-quality copy to keep, can copy the others' tags and people into it, and sends the rest to the Recycle Bin when you say. Videos are read once and remembered. Videos that only share a section are listed separately when asked for.
- **Read details from file names** (Library → More): fill in title, year, rating and people from names like `Ana Lee - Beach Day (2019)`.
- **Rename from details** (Library → More): rename and sort into folders from a pattern like `{Year}\{Title}`, with a preview, clash checks, subtitles and `.nfo` files moving along, and **Undo last rename**.
- Self-test checks for finding duplicates, and for the studio.

### Changed
- The first Library scan after updating reads every file again, to pick up studios and chapters.

## [0.4.0] - 2026-11-01

### Added
- **People.** Tag the people in a video from a People box beside the tags (any name or alias works). The **People** window (Ctrl+Shift+P) keeps each person's aliases and values for attributes you define, such as *Hair: Red*, written to their videos as tags (*Red hair*). Values can be limited to some years, and a value that doesn't apply to one video can be marked **Not in this video**.
- **Changing a person updates their videos**, as one undoable change. Only tags that came from profiles change; tags you typed yourself stay, and a tag two people share stays while either is in the video.
- **People travel with the files.** Each video carries a small record of its people, so a library scan on a new PC or after losing the tag database brings them back. The format is documented in `docs/data-format.md` for other tools (a Stash import plugin is planned).
- **Library:** a People column; searching finds videos by their people's names and aliases; the `.nfo` export lists people as actors.
- A self-test check for people.

### Fixed
- Closing a window while it was still loading the frames across the video could report an error from a background job. The job now just stops.
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

[0.4.0]: https://github.com/crbartlett84/runemark/releases/tag/v0.4.0
[0.3.0]: https://github.com/crbartlett84/runemark/releases/tag/v0.3.0
[0.2.0]: https://github.com/crbartlett84/runemark/releases/tag/v0.2.0
[0.1.1]: https://github.com/crbartlett84/runemark/releases/tag/v0.1.1
[0.1.0]: https://github.com/crbartlett84/runemark/releases/tag/v0.1.0
