"""Checks that every part Runemark relies on works on this PC, using throwaway files.

Run from the app (… menu → Run self-test) or headless:  Runemark.exe --self-test report.txt
"""
from __future__ import annotations

import hashlib
import platform
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__, explorer, proc
from .media import convert, frames, mp4meta, xtra
from .media.probe import probe
from .paths import IS_WINDOWS
from .tagdb import TagDB
from .tagsync import read_state, save_mp4


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def run(progress: Callable[[int, str], None] | None = None) -> list[Check]:
    results: list[Check] = []
    steps = [
        ("ffmpeg and ffprobe", _tools),
        ("Read video details", _probe),
        ("Grab an exact frame", _frame),
        ("MP4 tags, genre and thumbnail", _mp4_tags),
        ("Explorer Tags (Windows property system)", _explorer_windows),
        ("Explorer Tags (Runemark's own writer, read by Windows)", _explorer_builtin),
        ("Full save with verification", _full_save),
        ("Title, year, description and star rating", _details),
        ("Chapters (written into an MP4)", _chapters),
        ("Thumbnail suggestions", _suggestions),
        ("MKV to MP4 conversion", _convert),
        ("Tag database", _database),
        ("People (data block, profile tags, rebuild from the file)", _people),
        ("Duplicates (fingerprint a video and recognise a re-encoded copy)", _duplicates),
        ("Recycle Bin support", _recycle),
    ]
    with tempfile.TemporaryDirectory(prefix="runemark-selftest-") as tmp:
        ctx = {"dir": Path(tmp)}
        for number, (name, fn) in enumerate(steps):
            if progress:
                progress(int(number * 100 / len(steps)), name + "…")
            try:
                ok, detail = fn(ctx)
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"
            results.append(Check(name, ok, detail))
    if progress:
        progress(100, "Done")
    return results


def report(results: list[Check]) -> str:
    lines = [f"Runemark {__version__} self-test", f"{platform.platform()} · Python {platform.python_version()}", ""]
    for check in results:
        mark = "SKIP" if check.detail.startswith("Skipped") else ("PASS" if check.ok else "FAIL")
        lines.append(f"[{mark}] {check.name}")
        for line in check.detail.strip().splitlines():
            lines.append(f"       {line}")
    passed = sum(c.ok for c in results)
    lines += ["", f"{passed} of {len(results)} checks passed."]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------

def _make_mp4(ctx, name="test.mp4") -> Path:
    path = ctx["dir"] / name
    if not path.exists():
        proc.run([proc.ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=2",
                  "-f", "lavfi", "-i", "sine=f=440:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                  "-c:a", "aac", "-shortest", str(path)], timeout=120)
    return path


def _tools(ctx):
    lines = []
    for name in ("ffmpeg", "ffprobe"):
        path = proc.tool(name)
        out = proc.run([path, "-hide_banner", "-version"], timeout=30).stdout.decode("utf-8", "replace")
        lines.append(f"{name}: {path} ({out.splitlines()[0] if out else 'unknown version'})")
    return True, "\n".join(lines)


def _probe(ctx):
    info = probe(_make_mp4(ctx))
    video = info.main_video
    ok = video is not None and video.width == 320 and abs((info.duration or 0) - 2) < 0.2
    return ok, f"{video.width}x{video.height}, {info.duration:.2f}s" if video else "no video found"


def _frame(ctx):
    path = _make_mp4(ctx)
    info = probe(path)
    image, pts = frames.grab_shown_frame(path, info, 1.03)
    jpeg = frames.to_jpeg(image)
    ctx["jpeg"] = jpeg
    return image.size == (320, 180) and jpeg[:2] == b"\xff\xd8", f"frame at {pts:.3f}s, JPEG {len(jpeg)} bytes"


def _mp4_tags(ctx):
    path = ctx["dir"] / "tags.mp4"
    path.write_bytes(_make_mp4(ctx).read_bytes())
    jpeg = ctx.get("jpeg") or frames.to_jpeg(frames.grab_frame(path, 0.5))
    mp4meta.write_mp4_tags(path, keywords=["Alpha", "Beta"], genre=["Alpha"], cover=jpeg)
    tags = mp4meta.read_mp4_tags(path)
    ok = tags.keywords == ["Alpha", "Beta"] and tags.genre == ["Alpha"] and tags.cover == jpeg
    return ok, f"keywords={tags.keywords} genre={tags.genre} cover={'yes' if tags.cover else 'no'}"


def _explorer_windows(ctx):
    if not explorer.windows_api_available():
        return True, "Skipped: not Windows, or pywin32 isn't available (the built-in writer is used instead)."
    path = ctx["dir"] / "explorer-win.mp4"
    path.write_bytes(_make_mp4(ctx).read_bytes())
    mp4meta.write_mp4_tags(path, keywords=["One", "Two"], genre=[])
    explorer.write_windows(path, ["One", "Two"])
    via_windows = explorer.read_windows(path)
    via_file = xtra.read_category(path)
    keywords = mp4meta.read_mp4_tags(path).keywords
    ok = sorted(via_windows) == ["One", "Two"] and keywords == ["One", "Two"]
    return ok, (f"Windows reads: {via_windows}\nIn the file (Xtra box): {via_file}\n"
                f"File's Keywords still: {keywords}")


def _explorer_builtin(ctx):
    path = ctx["dir"] / "explorer-builtin.mp4"
    path.write_bytes(_make_mp4(ctx).read_bytes())
    mp4meta.write_mp4_tags(path, keywords=["Red", "Green Blue"], genre=[])
    xtra.write_category(path, ["Red", "Green Blue"])
    via_file = xtra.read_category(path)
    if explorer.windows_api_available():
        via_windows = explorer.read_windows(path)
        ok = sorted(via_windows) == ["Green Blue", "Red"]
        return ok, f"Written by Runemark, Windows reads: {via_windows}"
    return via_file == ["Red", "Green Blue"], f"Read back from the file: {via_file} (Windows not available to confirm)"


def _full_save(ctx):
    path = ctx["dir"] / "save.mp4"
    path.write_bytes(_make_mp4(ctx).read_bytes())
    jpeg = ctx.get("jpeg")
    outcome = save_mp4(path, keywords=["Tag One", "Tag Two"], genre=["Tag One"], cover=jpeg or mp4meta.UNCHANGED)
    state = read_state(path)
    detail = (f"Explorer method: {outcome.explorer_method}, copy used: {outcome.used_copy}\n"
              f"keywords={state.keywords} explorer={state.explorer} genre={state.genre}")
    if outcome.problems:
        detail += "\nProblems: " + "; ".join(outcome.problems)
    return outcome.ok and state.sync().in_sync, detail


def _details(ctx):
    path = ctx["dir"] / "details.mp4"
    path.write_bytes(_make_mp4(ctx).read_bytes())
    meta = mp4meta.VideoMeta("Self-test Title", 2024, "A description.", 4)
    outcome = save_mp4(path, keywords=["Tag"], genre=[], meta=meta)
    state = read_state(path)
    detail = f"Read back: {state.meta}"
    ok = outcome.ok and state.meta.key() == meta.key()
    if explorer.windows_api_available():
        shown = {name: explorer.read_windows_value(path, name)
                 for name in ("System.Title", "System.Media.Year", "System.Rating", "System.Comment")}
        detail += "\nWindows shows: " + ", ".join(f"{k.split('.')[-1]}={v!r}" for k, v in shown.items())
        ok = ok and shown["System.Title"] == meta.title and int(shown["System.Media.Year"] or 0) == meta.year \
            and mp4meta.value_to_stars(int(shown["System.Rating"] or 0)) == meta.rating
    if outcome.problems:
        detail += "\nProblems: " + "; ".join(outcome.problems)
    return ok, detail


def _chapters(ctx):
    from .media import chapters
    from .tagsync import save_mp4_with_chapters
    path = ctx["dir"] / "chapters.mp4"
    path.write_bytes(_make_mp4(ctx).read_bytes())
    info = probe(path)
    marks, _ = chapters.add_mark([], 0.0)
    marks, second = chapters.add_mark(marks, 1.0)
    second.name, second.auto = "Second Part", False
    jpeg = ctx.get("jpeg")
    outcome = save_mp4_with_chapters(path, info, marks, keywords=["Second Part"], genre=[], cover=jpeg,
                                     meta=mp4meta.VideoMeta("Chaptered", None, "", 3))
    after = probe(path)
    state = read_state(path)
    titles = [c.title for c in after.chapters]
    ok = outcome.ok and titles == ["Chapter 1", "Second Part"] and state.keywords == ["Second Part"] \
        and state.meta.rating == 3 and (state.cover == jpeg if jpeg else True) and state.sync().in_sync
    return ok, f"chapters={titles} keywords={state.keywords} explorer={state.explorer} rating={state.meta.rating}"


def _suggestions(ctx):
    from .media import suggest
    found = suggest.suggest(probe(_make_mp4(ctx)), count=4, samples=12)
    return len(found) >= 2, "frames at " + ", ".join(f"{s.pts:.2f}s" for s in found)


def _convert(ctx):
    mkv = ctx["dir"] / "test.mkv"
    proc.run([proc.ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=2",
              "-f", "lavfi", "-i", "sine=f=440:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
              "-c:a", "libopus" if _has_encoder("libopus") else "ac3", "-shortest", str(mkv)], timeout=120)
    info = probe(mkv)
    plan = convert.plan_conversion(info)
    tmp, sidecars = convert.run_conversion(plan)
    problems = convert.verify_conversion(plan, tmp)
    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()[:12]
    convert.cleanup([tmp, *sidecars])
    return not problems, ("; ".join(problems) or f"converted and verified (sha256 {digest}…)")


def _database(ctx):
    db = TagDB(ctx["dir"] / "tags.db")
    try:
        genre = db.add_category("Genre", write_genre=True)
        sub = db.add_category("Action", genre.id)
        db.add_tag("Heist", sub.id)
        db.export_json(ctx["dir"] / "export.json")
        other = TagDB(ctx["dir"] / "tags2.db")
        try:
            cats, tags = other.import_json(ctx["dir"] / "export.json")
            ok = (cats, tags) == (2, 1) and other.is_genre_tag("Heist")
        finally:
            other.close()
    finally:
        db.close()
    return ok, f"import added {cats} categories and {tags} tag(s)"


def _people(ctx):
    from . import library, people
    path = ctx["dir"] / "people.mp4"
    path.write_bytes(_make_mp4(ctx).read_bytes())
    save_mp4(path, keywords=["Hand Tag"], genre=[], meta=mp4meta.VideoMeta("", 2020, "", 0))
    db = TagDB(ctx["dir"] / "people.db")
    fresh = TagDB(ctx["dir"] / "people-fresh.db")
    try:
        attr = db.add_attribute("Hair")
        person = db.add_person("Test Person", ["Tester"])
        db.add_person_value(person.id, attr.id, "Red", tag="Red hair")
        problems = people.save_people(db, path, [(db.person(person.id), [])], label="self-test")
        state = read_state(path)
        library.scan(fresh, [str(ctx["dir"])])
        back = fresh.find_person("Tester")
        ok = (not problems and state.keywords == ["Hand Tag", "Test Person", "Red hair"] and state.sync().in_sync
              and state.data["profile_tags"] == ["Test Person", "Red hair"]
              and back is not None and back.uid == person.uid and [v.tag for v in back.values] == ["Red hair"])
        detail = (f"keywords={state.keywords} explorer={state.explorer}\n"
                  f"rebuilt from the file: {back.name if back else None} {[v.tag for v in back.values] if back else ''}")
        if problems:
            detail += "\nProblems: " + "; ".join(problems)
    finally:
        db.close()
        fresh.close()
    return ok, detail


def _duplicates(ctx):
    from . import duplicates
    from .media import fingerprint
    original = ctx["dir"] / "dupe-a.mp4"
    copy = ctx["dir"] / "dupe-b.mp4"
    proc.run([proc.ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
              "mandelbrot=s=320x180:r=25:start_scale=3:end_scale=0.3", "-t", "12", "-c:v", "libx264",
              "-pix_fmt", "yuv420p", str(original)], timeout=180)
    proc.run([proc.ffmpeg(), "-v", "error", "-y", "-i", str(original), "-vf", "scale=212:120", "-c:v", "libx264",
              "-crf", "33", str(copy)], timeout=180)
    a, b = fingerprint.fingerprint(original), fingerprint.fingerprint(copy)
    match = duplicates.compare(a, b)
    ok = match is not None and match.kind == duplicates.DUPLICATE
    return ok, (f"{len(a.hashes)} samples; " + (f"{match.kind}, {match.coverage_a:.0%} / {match.coverage_b:.0%} matched"
                                                if match else "no match"))


def _recycle(ctx):
    try:
        import send2trash  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"send2trash isn't available: {exc}"
    return True, "Available (not exercised here, to keep junk out of your Recycle Bin)."


def _has_encoder(name: str) -> bool:
    try:
        out = proc.run([proc.ffmpeg(), "-hide_banner", "-encoders"], timeout=30).stdout.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return False
    return f" {name} " in out


__all__ = ["run", "report", "Check", "IS_WINDOWS"]
