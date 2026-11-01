# Runemark's data in video files

Runemark writes everything it knows about a video into the file itself, so the file can move to another PC, another
library or another program without losing anything. This page describes exactly what is written where, so other
tools (a Stash plugin, a script, a media server) can read it.

Runemark never needs its database to understand a file: a library scan on a fresh install rebuilds the people list
from the files alone.

## Where things go (MP4 / M4V)

| What | Where in the file | Read by |
|---|---|---|
| Tags | `----:com.apple.iTunes:KEYWORDS` (UTF-8, `; ` between tags) | Mp3tag, ExifTool, ffprobe, Runemark |
| Tags | Explorer's Tags (`System.Keywords`, Microsoft's `Xtra` box) | Windows Explorer and Windows Search (`tags:` in Explorer's search box) |
| Genre | `©gen` (only tags from categories set to write Genre) | Plex, Jellyfin, Explorer's Genre column |
| Title | `©nam` | most players and media servers |
| Year | `©day` (a full date is kept if only the year is unchanged) | most players and media servers |
| Description | `desc`, and `©cmt` (Explorer's Comments) when that was empty or matched | Plex, Jellyfin, Explorer |
| Rating | Explorer's rating (`System.Rating`, 1–99; 1, 25, 50, 75, 99 = 1–5 stars) in the `Xtra` box | Explorer, Windows |
| Thumbnail | `covr` (JPEG or PNG) | Explorer, players, media servers |
| Chapters | the MP4's chapters, as FFmpeg writes them | players, media servers |
| People and where tags came from | `----:com.runemark:data` (UTF-8 JSON, below) | Runemark, and tools written to this page |

People's names and attribute values are **also written as ordinary tags** (Keywords and Explorer's Tags), so
programs that know nothing about Runemark still find them. The data block adds what plain tags can't say: which
tags belong to which person, their aliases, and which tags came from a profile rather than being typed by hand.

MKV files carry tags only in Matroska tags; Runemark converts them to MP4 when saved, and the data block is written then.

## The data block

A freeform MP4 item with mean `com.runemark` and name `data`, holding one UTF-8 JSON object. Keys are sorted and
there is no extra whitespace, but readers must not rely on either. A file without people has no data block.

```json
{
  "format": "runemark-data",
  "version": 1,
  "people": [
    {
      "id": "5262154904194d16b8d0b2442d52a5cb",
      "name": "Ana Lee",
      "aliases": ["Ana L.", "Annie"],
      "aliases_as_tags": false,
      "values": [
        {
          "id": "0b7c4f6a2e8d4a9b9d6f3e1a5c2b7d90",
          "attribute": {"id": "9f1e2d3c4b5a69788796a5b4c3d2e1f0", "name": "Hair", "stash_field": "hair_color"},
          "value": "Red",
          "tag": "Red hair",
          "as_tag": true,
          "from_year": null,
          "to_year": 2019
        }
      ],
      "skip": []
    }
  ],
  "profile_tags": ["Ana Lee", "Red hair"],
  "also_hand": []
}
```

### Fields

**Top level**

| Key | Type | Meaning |
|---|---|---|
| `format` | string | Always `"runemark-data"`. Ignore the block if it's anything else. |
| `version` | integer | Format version, currently `1`. A reader should accept versions it knows and treat a higher version as read-only. |
| `people` | array | The people in the video, as their profiles were when the file was saved. |
| `profile_tags` | array of strings | The tags in the file that came from these people's profiles. |
| `also_hand` | array of strings | Tags in `profile_tags` that were *also* typed by hand, so they stay when the person or value is removed. Optional; missing means empty. |

**Person**

| Key | Type | Meaning |
|---|---|---|
| `id` | string | Permanent id (32 hex characters). The same person has the same id in every file and survives renames. Match on this first. |
| `name` | string | Main name, as written to the file's tags. |
| `aliases` | array of strings | Other names. Names and aliases are unique across people in a library. |
| `aliases_as_tags` | boolean | Whether the aliases were also written as tags. |
| `values` | array | The person's attribute values (all of them, including ones skipped for this video or outside its years). |
| `skip` | array of strings | Ids of values that don't apply to this video ("not in this video"). |

**Value**

| Key | Type | Meaning |
|---|---|---|
| `id` | string | Permanent id of this value. |
| `attribute.id` | string | Permanent id of the attribute field. |
| `attribute.name` | string | The field's name, as the user defined it ("Hair"). Runemark ships no preset fields. |
| `attribute.stash_field` | string or null | The Stash performer field it maps to (`hair_color`, `ethnicity`, `tattoos`, …), if the user set one. |
| `value` | string | The value ("Red"). |
| `tag` | string | The tag it becomes in files ("Red hair"); often the same as `value`. |
| `as_tag` | boolean | Whether this field is written as a tag at all. |
| `from_year`, `to_year` | integer or null | The years it applies to, matched against the video's year (`©day`). Either can be open. A value with a range doesn't apply to a video without a year. |

### Working out a video's tags

A person contributes: their `name`; their `aliases` if `aliases_as_tags`; and the `tag` of every value where `as_tag`
is true, the id is not in `skip`, and the video's year is inside `from_year`–`to_year`. `profile_tags` is the union
over all people (case-insensitive, first spelling kept). The file's full tag list is the hand-typed tags followed by
`profile_tags`.

The **hand-typed tags** are the file's tags minus `profile_tags`, plus anything in `also_hand`. When people change,
Runemark keeps the hand-typed tags and recomputes `profile_tags` from everyone still in the video, so a tag two people
share stays while either of them is there.

### Rules for other tools

- Read the block; don't depend on Runemark's database.
- Match people by `id`, then by `name` or any alias. Don't create a duplicate when a name or alias already exists.
- If you change a file's tags, either update the block to match or leave it alone; Runemark treats tags missing from
  `profile_tags` as hand-typed.
- Unknown keys may appear in later versions: keep them when rewriting a block.

## Changes

- Version 1 (Runemark 0.4.0): first version.
