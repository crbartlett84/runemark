# Contributing to Runemark

Runemark is a personal project, shared in case it's useful. Bug reports, ideas and pull requests are welcome; replies may take a while.

## Reporting a problem

Open an issue and include:

- what you did, what you expected, and what happened,
- the self-test report (**⋯ → Run self-test → Copy report**),
- the log, if there was an error (**⋯ → Open log folder**, newest file).

Please don't attach personal videos. If a particular file causes the problem, the output of `ffprobe -show_streams -show_format yourfile` usually says enough.

## Building and testing

Requirements: Windows 10 or 11. Nothing else needs installing: double-click **`Build Runemark.cmd`**. It downloads a private Python 3.12 (via [uv](https://github.com/astral-sh/uv)) and FFmpeg, runs the tests, builds `dist\Runemark\Runemark.exe` and self-tests it. If Windows Smart App Control blocks the downloaded Python, install Python 3.12 from [python.org](https://www.python.org/downloads/) and run the build again; it switches to that Python automatically.

While working on the code:

```
Run Runemark.cmd                          # run from source
.venv\Scripts\python -m pytest            # tests (FFmpeg from vendor\ffmpeg must be on PATH)
.venv\Scripts\python -m runemark --self-test report.txt
```

The tests make their own small videos with FFmpeg, so no media files are needed. They also run on Linux (without the Windows property-system checks).

## Pull requests

- Keep changes focused, and add or update tests for behaviour changes.
- Never re-encode video, and never change a user's file without reading it back to check it. Saves go through `tagsync.save_mp4`, which verifies what it wrote.
- User-facing text is plain language: say what happens to the user's files, not how the code works.
- Add a line to `CHANGELOG.md` under an "Unreleased" heading.

## Releasing (maintainer)

1. Update `__version__` in `runemark/__init__.py` and `version` in `pyproject.toml`.
2. Move the "Unreleased" notes in `CHANGELOG.md` under the new version.
3. Commit, then tag and push: `git tag v0.3.0` and `git push origin main v0.3.0`.

GitHub Actions builds and self-tests the app on Windows, then publishes a release with the zip and the changelog notes.

By contributing, you agree that your contributions are licensed under the GPL v3, like the rest of Runemark.
