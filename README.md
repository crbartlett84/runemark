# Runemark

A Windows desktop app for tagging your MP4 and MKV videos.

- **Watch and inspect**: play the video, step frame by frame, and see the useful facts: length, resolution, frame rate (constant or variable), codecs, bit depth, HDR, audio and subtitle tracks, chapters.
- **Pick the thumbnail**: choose any frame, or an image file, and Runemark embeds it as the file's cover image. Windows Explorer shows it as the thumbnail.
- **Tag from your own tag database**: tags are organised in categories and subcategories. The categories only organise the list inside the app; just the tags are written to the file.
- **Keep everything in sync**: every save writes the same tags to Explorer's **Tags** field and to the file's own tags, then reads both back to check them.
- **Chapters**: mark chapters on exact frames. They're numbered automatically, or named after a tag (which also tags the file) or anything you type.
- **Title, year, description and star rating**: Explorer shows them, and Plex and Jellyfin read them.
- **Thumbnail suggestions**: Runemark looks through the video and offers sharp, well-lit, varied frames.
- **Library**: every video in your folders in one list. Search by tags, find what's untagged or has no thumbnail, and tag or rate many files at once.

## Download

Get the latest `Runemark-x.y.z-win64.zip` from the [Releases page](https://github.com/crbartlett84/runemark/releases), unzip it anywhere (for example `C:\Apps\Runemark`) and run **`Runemark.exe`**. It's a portable app: there's no installer, and FFmpeg is included.

Runemark isn't code-signed, so Windows may say it "protected your PC" from an unrecognised app. Choose **More info → Run anyway** if you trust this download. If **Smart App Control** is on, Windows blocks unsigned apps outright and Runemark won't start.

Requires Windows 10 or 11 (64-bit).

## Building it yourself

**Build the app:** double-click **`Build Runemark.cmd`** in this folder (or run `powershell -ExecutionPolicy Bypass -File packaging\build.ps1`).

You don't need to install Python. The build downloads two pinned tools, each checked against a fixed SHA-256: uv, which sets up a private copy of Python 3.12 just for Runemark, and FFmpeg 7.1.1. It then runs the tests and builds `dist\Runemark\Runemark.exe` with a zip beside it. It finishes by self-testing the built app. Everything it prints is also saved to `build-log.txt`. The `dist\Runemark` folder is self-contained: copy it wherever you like and pin `Runemark.exe` to Start.

**Or run straight from the source:** double-click **`Run Runemark.cmd`**.

**Then, once:** open **⋯ → Run self-test**. It checks every piece on your PC using throwaway files: FFmpeg, frame grabs, MP4 tags, Explorer's Tags (both through Windows and through Runemark's own writer), a full save, an MKV conversion, and the tag database. If anything fails, **Copy report** puts the details on the clipboard.

Optional: **⋯ → Settings → Add "Tag with Runemark"** puts the app in Explorer's right-click menu for MP4 and MKV files.

## Where tags go

| What you see | MP4 | MKV |
|---|---|---|
| Explorer's **Tags** column | `WM/Category` in Microsoft's `Xtra` box (written through Windows itself) | Not possible; see below |
| The file's own tags (Mp3tag, ExifTool, ffprobe…) | `KEYWORDS` (iTunes-style text, separated by `; `) | Converted to MP4 on save |
| **Genre** (Plex, Jellyfin, Explorer's Genre column) | `©gen`: only tags from categories marked "write to Genre" | Converted to MP4 on save |
| Thumbnail | `covr` cover art (Explorer shows it) | Converted to MP4 on save |
| Title / Year | `©nam` / `©day` | Converted to MP4 on save |
| Description | `desc` and `©cmt` (Explorer shows it as Comments) | Converted to MP4 on save |
| Star rating | `WM/SharedUserRating` in the `Xtra` box (Explorer's Rating) | Converted to MP4 on save |
| Chapters | A chapter track (VLC, Plex, Jellyfin) | Converted to MP4 on save |

ID3 is the tag format for MP3 audio and doesn't exist in video files. The fields above are what video files use instead.

**Keeping things in sync.** When a file opens, Runemark reads all three places. If Explorer's Tags and the file's own tags differ (for example after editing Tags in Explorer), it shows what is only on each side. It combines them, and saving puts both sides back in sync. **Use one side…** keeps only Explorer's tags or only the file's tags instead.

**Genre.** Genre only gets tags from categories you flag ("Also write tags in this category to Genre"). Subcategories inherit the flag. If a file already had a genre that isn't in your database, Runemark keeps it in Genre until you file that tag in a category, so nothing is quietly dropped.

## MKV files

Windows can't edit tags in MKV files, and Windows 11 doesn't use an MKV's embedded cover as its thumbnail. So **saving an MKV converts it to MP4**:

1. A plan shows every track and what happens to it. The **video is copied as is**, so the picture is identical and nothing is re-encoded. You can change the choices before converting.
   - Audio that MP4 can't hold is converted. Lossless formats (TrueHD, PCM) become FLAC, which is still lossless. Others (such as Vorbis) become AAC.
   - Text subtitles (SRT, ASS) become MP4 text subtitles. ASS styling is lost unless you choose "Save beside the video".
   - Picture-based subtitles (PGS, VobSub) are saved beside the video, e.g. `Movie.eng.sup`.
   - Fonts are left out. An MKV cover image becomes the MP4 thumbnail unless you pick a frame.
   - Chapters are kept. Any that run past the end of the video are trimmed or left out, because in an MP4 they'd make Windows and media servers show the wrong length.
2. Runemark converts to a temporary file, checks that the length and tracks match, writes your tags and thumbnail, then renames it to `.mp4`.
3. **Only then does the original MKV go to the Recycle Bin**, so you can restore it. On a drive with no Recycle Bin (a network share, a USB stick or a card reader) Runemark keeps the MKV and tells you, rather than deleting it.

MKVs with VP8, Theora or VC-1 video can't go into MP4 without re-encoding the picture, so Runemark says so and leaves them alone.

## Choosing a thumbnail

Pause on a frame, using **←/→** or **, .** to step exactly one frame, and click **Use this frame** (Ctrl+T). The **Thumbnail** tab shows the current cover beside the new one. Runemark decodes that exact frame with FFmpeg. It corrects non-square pixels and rotated phone videos, and tone-maps HDR so it isn't washed out. It saves a JPEG, 1280 px on the long side by default (see Settings).

If Explorer keeps showing an old thumbnail, that's its cache. Leave "Keep each file's Date modified" off (the default) so Explorer notices the change.

## Chapters

Open the **Chapters** tab, pause on the frame where a chapter starts (step with ← →) and click **Add chapter** or press **M**. The first chapter you add also gets a chapter at 0:00, because players expect the first chapter to start at the beginning. New chapters are called "Chapter 1, 2, 3…" and renumber themselves when you add or delete one. Double-click a name to change it: pick a tag from your database, and the file gets that tag too, or type any name, which is also added to the file's tags (shown dashed until you file it). Removing or renaming a chapter leaves the file's tags alone. **Number** turns a named chapter back into a numbered one. Click a chapter to see its first frame.

Existing chapters in a file show up in the list, so you can correct them without losing them. Saving changed chapters writes the whole file again (nothing is re-encoded, and data tracks such as camera GPS aren't kept; Runemark asks first if there are any). It checks the new file before replacing the original. Explorer doesn't show chapters; VLC, Plex and Jellyfin do.

## Title, year, description and rating

They're at the top of the **Details** tab. Click a star to rate, and click the same star again to clear the rating. The library can also rate many files at once.

## Library

**Library** (Ctrl+B) lists every video in the folders you add (**Folders → Add a folder…**), plus every file you've saved in Runemark. Scans only read files that are new or changed.

- **Search** by file name or title, and add tags under **Must have tag…**. Every tag you add must match.
- **Filters** show videos with no tags, no thumbnail, no rating, MKVs that aren't converted yet, or files that have moved or been deleted.
- **Saved searches** keep a search under a name.
- Select several videos and use **Add tag…** (type several, separated by commas), **Remove tag…** or **Rate…**. Each file is saved and checked like a normal save. MKVs are skipped, since they need converting first.
- The Library reopens with the search and filter you last used. A scan can be stopped with **Stop scan**; what it has read so far is kept.
- **More → Export for Jellyfin / Kodi (.nfo)** writes a `.nfo` file beside each video with the title, year, description, rating, genres and tags, and optionally a collection name ("set"). Kodi turns sets into movie collections. Jellyfin and Emby read `.nfo` files when NFO metadata is turned on for the library.
- **Remove from the library** only changes Runemark's list; the files aren't touched.

## People

Tag the people in a video once, and everything you've recorded about them follows.

- **People box** (beside the tags): type any name or alias. A new name can be added on the spot.
- **People window** (**⋯ → People…**, Ctrl+Shift+P, or click a person): each person has a name and any number of aliases, and values for **attributes** you define (for example *Hair: Red*, written to files as the tag *Red hair*). A value can be limited to some years; it then applies only to videos whose year falls inside them. **Attributes…** renames or deletes fields, sets whether a field is written as a tag, and links it to a Stash performer field.
- A person's name, their attribute values (and aliases, if you tick that) are **written to their videos as ordinary tags**, so Explorer (`tags:` in its search box), Plex, Jellyfin and other programs find them. In the editor they're shown in italics. Clicking one offers **Not in this video**, for a value that doesn't apply to one video; that's stored in the file, so it sticks.
- **Change a profile and the videos follow.** When you close People after changing someone, Runemark offers to update the videos they're in (one undoable change, like a bulk change). Only tags that came from profiles change: tags you typed yourself stay, and a tag two people share stays while either of them is in the video. Deleting a person takes them out of their videos.
- **Library:** a People column, and searching finds a video by any of its people's names or aliases. The `.nfo` export lists them as actors.
- **Portable:** each video also carries a small record of its people (aliases, values, which tags came from a profile). A library scan on a new PC, or after losing the tag database, rebuilds the people list from the videos. The format is described in [docs/data-format.md](docs/data-format.md), so other tools can read it too.

## Duplicates

**Library → More → Find duplicates…** finds copies of the same video, even when they were re-encoded, resized, converted to another format, given black bars, trimmed (opening or credits cut off), had scenes cut out or put in a different order.

- Each video is read once (about 3 minutes per hour of 1080p video on a modest PC; faster with a graphics card) and remembered. After that only new or changed videos are read, and a search can be stopped and picks up where it left off.
- Copies are shown in groups, with the **best-quality** one suggested to keep (highest resolution, then bitrate; bitrates within 20% count as equal and go to the most complete copy). Each other copy says how it differs: *Same video*, *Missing 2:40 at 12:30*, *Has 0:45 more at 0:00*.
- **Copy tags into the one to keep** adds the other copies' tags and people and fills in its title, year, description and rating where it has none. Nothing is removed, and it can be undone.
- **Send ticked to the Recycle Bin** (tick them, or use **Tick all but the one to keep**). Nothing is deleted automatically, and on drives without a Recycle Bin the file is kept.
- **Shares a section** (tick it before searching) lists different videos that share part of their content, such as the same intro.
- It can't recognise a copy that was mirrored, heavily cropped, covered by a big watermark, sped up, or filmed off a screen.

## File names

Both work on the selected videos in the Library (or all shown, if none are selected) and show a preview first.

- **More → Read details from file names…** fills in title, year, rating and people from names that follow a pattern, such as `{People} - {Title} ({Year})`. People can be separated by commas, "&" or "and", and are matched to your People list by name or alias (new ones can be added). Details already filled in are kept unless you choose to replace them. Undo with **Undo last bulk change**.
- **More → Rename from details…** renames from a pattern such as `{Title} ({Year})`, and `\` sorts into folders (`{Year}\{Title}`). Characters Windows doesn't allow are replaced, empty fields leave no stray brackets or dashes, videos without a title are skipped, and clashes are shown and skipped. Subtitles and `.nfo` files with the same name move along, and Runemark's library, undo history and people follow. **More → Undo last rename…** puts them back.
- If Sonarr, Radarr or Plex manage a folder, let them rename, or rescan them afterwards.

## Undo

Runemark remembers each file's tags, genre, title, year, description and rating from just before every save (the last 10 saves per file, in the tag database).

- **More → Undo last save…** puts the open file back one save. Use it again to go further back.
- **Library → More → Undo last bulk change…** reverts every file of the last bulk change (add or remove tags, set a rating, or updating files after renaming or merging a tag). Files you've saved again since are left alone, so a later edit is never lost.
- Thumbnails and chapters aren't part of undo: they live only in the video file.

## The tag database

**Manage…** (Ctrl+D) opens the database:

- **Categories:** create, nest, rename or delete them, and drag one into another. Deleting a category keeps its tags; they move up a level.
- **Tags:** create, rename, move (or drag onto a category), merge or delete them. Tag names are unique across all categories, because only the name is written to files.
- **Updating files:** Runemark remembers which files it tagged. Renaming, merging or deleting a tag, or changing which categories feed Genre, offers to update those files for you.
- **Import… / Export…** use a JSON file for moving tags to another PC.

**Backups.** The database (`%APPDATA%\Runemark\tags.db`) is backed up automatically once a day, keeping the last 10. In **⋯ → Settings** you can:
- back up now to any folder,
- send the daily backups somewhere else, such as a OneDrive folder (backups are complete, closed copies, so syncing them is safe),
- restore from a backup (your current database is backed up first, so a restore can be undone),
- switch to **portable** mode, which keeps the database and settings in a "Runemark data" folder beside Runemark.exe. Rebuilding with `Build Runemark.cmd` keeps that folder, and it's never put in the zip.

Tags that a file already has but your database doesn't are shown dashed. Click one to file it in a category.

## Keyboard

| Key | Action |
|---|---|
| Space | Play / pause |
| ← → or , . | Previous / next frame (exact) |
| Shift+← → | Back / forward 5 seconds |
| Ctrl+T | Use this frame as the thumbnail |
| M | Add a chapter at this frame |
| Ctrl+B | Library |
| Ctrl+S | Save |
| Ctrl+Enter | Save and go to the next file |
| Ctrl+Shift+C | Copy this file's tags |
| Ctrl+Shift+V | Add the copied tags (or the last saved file's tags) to this file |
| Ctrl+O | Open |
| Ctrl+L | Jump to the tag box |
| Ctrl+D | Tag database |
| Ctrl+Shift+P | People |
| Ctrl+PgUp / PgDn | Previous / next open file |

You can open several files at once (or drop them on the window). Move between them with the arrows next to the file name.

In the Library list: **Enter** opens the selected files, **0–5** sets their rating and **T** adds tags.

## Safety

- The video and audio data are never re-encoded. MP4 tag edits change only the tag area: when the file's index sits at the end there is room to grow in place, and otherwise Runemark works on a copy and swaps it in when done. Every save reads the tags back and reports any mismatch.
- Conversions never touch the MKV until the new MP4 has been checked, and the MKV goes to the Recycle Bin, not straight to deletion. Where there is no Recycle Bin (network and removable drives), the MKV is kept.
- Logs: `%APPDATA%\Runemark\logs`. Tag database and settings: `%APPDATA%\Runemark` (or "Runemark data" beside the app in portable mode).

## Limitations

- MKV → MP4 needs video MP4 can hold (H.264, HEVC, AV1, VP9, MPEG-2/4). Runemark doesn't re-encode video.
- Explorer's Tags field only exists for MP4/M4V on Windows. The tags are still in the file's own fields for other programs.
- Plex and Jellyfin read Genre from embedded tags only if they're set to use local or embedded metadata.

## For tinkering

```
runemark/
  app.py            start-up and --self-test
  tagdb.py          the tag database (SQLite) and the library index
  tagsync.py        reading tags from every place, saving and verifying
  library.py        library scans, search, bulk changes, .nfo export
  people.py         people in videos: the data block, profile tags, updating videos (docs/data-format.md)
  duplicates.py     finding copies: comparing fingerprints, groups, the keep rule, merging tags
  names.py          file names: reading details from names, renaming from details, undo
  media/fingerprint.py  video fingerprints (a picture hash every half-second)
  datastore.py      backups, restore, portable mode
  explorer.py       Windows property system, Recycle Bin, right-click menu
  media/probe.py    ffprobe parsing and the Details tab text
  media/frames.py   exact frame grabs, thumbnails
  media/mp4meta.py  MP4 keywords, genre, cover, title, year, description (mutagen)
  media/xtra.py     Explorer's Tags and rating inside MP4, written directly
  media/chapters.py chapter marks, numbering, writing chapters into an MP4
  media/suggest.py  thumbnail suggestions
  media/convert.py  MKV → MP4 plan, conversion and checks
  ui/               the Qt interface
tests/              pytest suite (generates its own small test videos with FFmpeg)
packaging/          build script, uv and FFmpeg downloads, PyInstaller spec, icon
```

Run the tests with `.venv\Scripts\python -m pytest`. `.github/workflows/build-windows.yml` builds and self-tests on GitHub's Windows machines if you push this folder to a repository.

See [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) for the open-source components, [CONTRIBUTING.md](CONTRIBUTING.md) for building, testing and reporting problems, and [CHANGELOG.md](CHANGELOG.md) for what changed in each version.

## License

Copyright © 2026 Chris Bartlett.

Runemark is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. It is distributed in the hope that it will be useful, but **without any warranty**; see [LICENSE](LICENSE) for details.

The GPL fits the parts Runemark is built on: the bundled FFmpeg build is GPL v3 and mutagen is GPL v2 or later.
