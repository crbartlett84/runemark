# Third-party components

Runemark is built on these open-source projects:

| Component | Used for | Licence |
|---|---|---|
| [FFmpeg](https://ffmpeg.org) 7.1.1, "essentials" build by [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) | Reading video details, frame grabs, MKV → MP4 | GPL v3 (this build); its licence text is copied to `ffmpeg\LICENSE.txt` |
| [Qt for Python (PySide6)](https://www.qt.io/qt-for-python) | The interface and the video player | LGPL v3 |
| [mutagen](https://github.com/quodlibet/mutagen) | MP4 tags and cover art | GPL v2 or later |
| [Pillow](https://python-pillow.org) | Thumbnail images | MIT-CMU (HPND) |
| [Send2Trash](https://github.com/arsenetar/send2trash) | Recycle Bin | BSD |
| [pywin32](https://github.com/mhammond/pywin32) | Windows property system and COM | PSF |
| [uv](https://github.com/astral-sh/uv) (build time only) | Setting up Python for the build | MIT / Apache 2.0 |

The layout of Microsoft's `Xtra` box follows the documentation in [ExifTool](https://exiftool.org)'s `Microsoft.pm`.
