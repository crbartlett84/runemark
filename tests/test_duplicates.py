"""v0.5.0: finding copies. Fingerprints survive re-encoding, resizing and black bars; matching finds plain copies,
trimmed copies, copies with a scene cut out or scenes swapped, and keeps unrelated videos apart."""
import subprocess
from pathlib import Path

import pytest

from runemark import duplicates as D
from runemark.media import fingerprint as F

FFMPEG = "ffmpeg"
SIZE = "320x180"
SCENE = 8   # seconds per scene

# Six visibly different scenes (moving generators), and three more for an unrelated video. Scene 3 (cellauto) is
# fine noise that averages to plain grey at fingerprint size: it stands in for a dark or faded scene.
SCENES = [
    f"mandelbrot=s={SIZE}:r=25:start_scale=3:end_scale=0.3",
    f"life=s={SIZE}:r=25:mold=10:seed=1:ratio=0.2:death_color=#203040:life_color=#e0c080",
    f"cellauto=s={SIZE}:r=25:rule=110:seed=7",
    f"testsrc2=s={SIZE}:r=25",
    f"mandelbrot=s={SIZE}:r=25:start_x=-0.10:start_y=0.92:start_scale=0.6:end_scale=0.02",
    f"sierpinski=s={SIZE}:r=25:seed=5:jump=50",
]
OTHER = [
    f"mandelbrot=s={SIZE}:r=25:start_x=-0.75:start_y=0.1:start_scale=1.5:end_scale=0.05",
    f"life=s={SIZE}:r=25:mold=4:seed=99:ratio=0.5",
    f"testsrc=s={SIZE}:r=25",
]


def ff(*args):
    subprocess.run([FFMPEG, "-v", "error", "-y", *args], check=True)


def make(sources, out: Path, extra_vf: str = "", size_flags=()):
    """Concatenate generated scenes into one H.264 file."""
    inputs, labels = [], []
    for n, src in enumerate(sources):
        inputs += ["-f", "lavfi", "-t", str(SCENE), "-i", src]
        labels.append(f"[{n}:v]format=yuv420p,setsar=1[v{n}]")
    chain = ";".join(labels) + ";" + "".join(f"[v{n}]" for n in range(len(sources))) + \
        f"concat=n={len(sources)}:v=1:a=0" + (f",{extra_vf}" if extra_vf else "") + "[out]"
    ff(*inputs, "-filter_complex", chain, "-map", "[out]", "-c:v", "libx264", "-preset", "ultrafast",
       "-pix_fmt", "yuv420p", *size_flags, str(out))
    return out


@pytest.fixture(scope="session")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("dupes")
    base = make(SCENES, d / "original.mp4")
    out = {"original": base}
    # the same video re-encoded smaller, at a low bitrate, as MKV
    out["reencoded"] = d / "reencoded.mkv"
    ff("-i", str(base), "-vf", "scale=212:120", "-c:v", "libx264", "-crf", "35", "-preset", "veryfast",
       str(out["reencoded"]))
    # letterboxed into a bigger frame
    out["letterboxed"] = d / "letterboxed.mp4"
    ff("-i", str(base), "-vf", "pad=320:260:0:40:black", "-c:v", "libx264", "-preset", "ultrafast",
       str(out["letterboxed"]))
    # opening (first 5.3 s) and end (last 6.7 s) trimmed off, at times that aren't on the 0.5 s sampling grid
    out["trimmed"] = d / "trimmed.mp4"
    ff("-ss", "5.3", "-i", str(base), "-t", str(SCENE * 6 - 5.3 - 6.7), "-c:v", "libx264", "-preset", "ultrafast",
       str(out["trimmed"]))
    # scene 3 (16-24 s) cut out of the middle
    out["scene_cut"] = make(SCENES[:2] + SCENES[3:], d / "scene_cut.mp4")
    # scenes swapped around
    out["reordered"] = make(SCENES[3:] + SCENES[:3], d / "reordered.mp4")
    # unrelated video, and one that shares only scene 4
    out["unrelated"] = make(OTHER, d / "unrelated.mp4")
    out["shares_one"] = make(OTHER[:2] + [SCENES[4]], d / "shares_one.mp4")
    return out


@pytest.fixture(scope="session")
def prints(clips):
    return {name: F.fingerprint(path) for name, path in clips.items()}


def test_fingerprint_basics(prints, clips):
    fp = prints["original"]
    assert abs(fp.duration - SCENE * 6) <= 1.0 and fp.useful() > len(fp.hashes) * 0.8
    again = F.Fingerprint.from_bytes(fp.to_bytes(), fp.fps, fp.crop)
    assert again.hashes == fp.hashes and again.flat == fp.flat
    assert prints["letterboxed"].crop.startswith("320:180:0:4")          # black bars found and cropped
    assert prints["original"].crop == ""
    assert prints["unrelated"].crop == "" and prints["shares_one"].crop == ""  # dark scenes aren't mistaken for bars


@pytest.mark.parametrize("copy", ["reencoded", "letterboxed"])
def test_reencoded_and_letterboxed_copies_are_duplicates(prints, copy):
    m = D.compare(prints["original"], prints[copy])
    assert m is not None and m.kind == D.DUPLICATE, m
    assert m.coverage_a > 0.9 and m.coverage_b > 0.9 and not m.a_gaps and not m.reordered


def test_trimmed_copy_is_a_cut_with_gaps_at_the_ends(prints):
    m = D.compare(prints["original"], prints["trimmed"])
    assert m.kind == D.CUT and m.coverage_b > 0.95
    assert len(m.a_gaps) == 2
    (s0, e0), (s1, e1) = m.a_gaps
    assert s0 == 0 and abs(e0 - 5.3) <= 1.0                            # the opening that was trimmed
    assert abs(s1 - (SCENE * 6 - 6.7)) <= 1.0 and abs(e1 - SCENE * 6) <= 1.0


def test_a_scene_cut_from_the_middle_is_found_and_located(prints):
    m = D.compare(prints["original"], prints["scene_cut"])
    assert m.kind == D.CUT and len(m.runs) >= 2 and m.coverage_b > 0.9
    (s, e), = m.a_gaps
    assert abs(s - 2 * SCENE) <= 1.0 and abs(e - 3 * SCENE) <= 1.0        # scene 3 is the missing stretch
    assert not m.b_gaps and not m.reordered


def test_reordered_scenes(prints):
    m = D.compare(prints["original"], prints["reordered"])
    assert m.kind == D.DUPLICATE and m.reordered


def test_unrelated_videos_dont_match_and_shared_sections_are_labelled(prints):
    assert D.compare(prints["original"], prints["unrelated"]) is None
    m = D.compare(prints["shares_one"], prints["original"])
    assert m is not None and m.kind == D.SHARED
    assert 0.25 < m.coverage_a < 0.45 and 0.1 < m.coverage_b < 0.25        # one scene of 3, one of 6


def test_finding_copies_across_a_library(prints):
    found = D.find_copies(prints)
    kinds = {frozenset((f.a, f.b)): f.match.kind for f in found}
    assert kinds[frozenset(("original", "reencoded"))] == D.DUPLICATE
    assert kinds[frozenset(("original", "trimmed"))] == D.CUT
    group = {"original", "reencoded", "letterboxed", "trimmed", "scene_cut", "reordered"}
    assert all(not ("unrelated" in pair and pair & group) for pair in kinds)
    assert kinds[frozenset(("unrelated", "shares_one"))] == D.SHARED        # they share two scenes
    assert kinds[frozenset(("original", "shares_one"))] == D.SHARED
    (found_group,) = D.groups(found)      # shares_one and unrelated aren't copies, so they're not in it
    assert set(found_group) == group
    assert all(f.match.kind != D.SHARED for f in D.find_copies(prints, include_shared=False))


def test_cancel(clips):
    import threading
    stop = threading.Event()
    stop.set()
    with pytest.raises(F.Cancelled):
        F.fingerprint(clips["original"], cancel=stop)


def test_library_scan_stores_fingerprints_and_reads_each_video_once(clips, tmp_path, monkeypatch):
    import shutil
    from runemark import library as L
    from runemark.tagdb import TagDB
    folder = tmp_path / "lib"
    folder.mkdir()
    for name in ("original", "trimmed", "unrelated"):
        shutil.copy2(clips[name], folder / clips[name].name)
    db = TagDB(tmp_path / "t.db")
    try:
        L.scan(db, [str(folder)])
        first = D.scan_library(db)
        assert first.read == 3 and not first.problems
        (f,) = first.found
        assert {Path(f.a).name, Path(f.b).name} == {"original.mp4", "trimmed.mp4"} and f.match.kind == D.CUT
        calls = []
        real = F.fingerprint
        monkeypatch.setattr(F, "fingerprint", lambda *a, **k: calls.append(a) or real(*a, **k))
        again = D.scan_library(db)
        assert again.read == 0 and calls == [] and len(again.found) == 1      # all from the database
        (folder / "unrelated.mp4").unlink()
        L.scan(db, [str(folder)])
        D.scan_library(db)
        assert db.conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0] == 2   # the gone file's dropped
    finally:
        db.close()


def test_keep_rule_and_relations(prints, clips):
    copies = [D.Copy("a", 1920, 1080, 4_000_000, 60), D.Copy("b", 1920, 1080, 8_000_000, 58),
              D.Copy("c", 1280, 720, 20_000_000, 61)]
    assert D.best_quality(copies).path == "b"                                   # resolution, then bitrate
    assert D.best_quality([D.Copy("x", 1280, 720, 5, 40), D.Copy("y", 1280, 720, 5, 60)]).path == "y"
    # a trimmed copy with a slightly higher bitrate doesn't beat the complete one
    assert D.best_quality([D.Copy("full", 1280, 720, 1_000_000, 60), D.Copy("cut", 1280, 720, 1_100_000, 48)]).path \
        == "full"
    real = D.describe_copy(str(clips["reencoded"]))
    assert (real.width, real.height) == (212, 120) and real.duration > 40 and real.size > 0
    found = D.find_copies(prints)
    assert D.relation("original", "reencoded", found) == "Same video"
    text = D.relation("original", "scene_cut", found)
    assert text.startswith("Missing 0:08 at 0:16"), text
    assert D.relation("trimmed", "original", found).startswith("Has ")


def test_merge_into_the_kept_copy(clips, tmp_path):
    import shutil
    from runemark import people as P
    from runemark.media.mp4meta import VideoMeta
    from runemark.tagdb import TagDB
    from runemark.tagsync import read_state, save_mp4
    keep, other = tmp_path / "keep.mp4", tmp_path / "other.mp4"
    shutil.copy2(clips["original"], keep)
    shutil.copy2(clips["trimmed"], other)
    db = TagDB(tmp_path / "t.db")
    try:
        save_mp4(keep, keywords=["Lake"], genre=[], meta=VideoMeta("", None, "", 0))
        save_mp4(other, keywords=["Summer", "lake"], genre=[], meta=VideoMeta("Cottage", 2019, "", 4))
        ana = db.add_person("Ana Lee")
        P.save_people(db, other, [(ana, [])], label="t")
        assert D.merge_into(db, str(keep), [str(other)]) == []
        st = read_state(keep)
        assert st.keywords == ["Lake", "Summer", "Ana Lee"]
        assert (st.meta.title, st.meta.year, st.meta.rating) == ("Cottage", 2019, 4)
        assert st.data["people"][0]["id"] == ana.uid and db.files_with_person(ana.id) == sorted(
            [str(keep), str(other)])
        assert db.history(keep)[0].label.startswith("Duplicates")
    finally:
        db.close()


# --- the Duplicates window ------------------------------------------------------------------------------------

def test_duplicates_window(clips, tmp_path, monkeypatch):
    import shutil
    import time
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox
    from runemark import explorer
    from runemark import library as L
    from runemark.settings import Settings
    from runemark.tagdb import TagDB
    from runemark.tagsync import read_state, save_mp4
    from runemark.ui.duplicates_window import DuplicatesWindow
    app = QApplication.instance() or QApplication([])
    folder = tmp_path / "lib"
    folder.mkdir()
    for name in ("original", "reencoded", "trimmed", "unrelated", "shares_one"):
        shutil.copy2(clips[name], folder / clips[name].name)
    save_mp4(folder / "trimmed.mp4", keywords=["Lake"], genre=[])
    db = TagDB(tmp_path / "t.db")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    recycled = []
    monkeypatch.setattr(explorer, "recycle", lambda p: (recycled.append(Path(p).name), Path(p).unlink()))
    try:
        L.scan(db, [str(folder)])
        w = DuplicatesWindow(db, Settings.load())
        w.shared_box.setChecked(True)
        w.find()
        end = time.time() + 120
        while w._job is not None and time.time() < end:
            app.processEvents()
            time.sleep(0.02)
        (group,) = w.groups
        assert {Path(c.path).name for c in group["copies"]} == {"original.mp4", "reencoded.mkv", "trimmed.mp4"}
        assert Path(group["keep"]).name == "original.mp4"                  # 320x180 beats the 212x120 re-encode
        rows = {r.text(0): r.text(1) for r in w._rows()}
        assert rows["original.mp4"].startswith("Keep") and rows["reencoded.mkv"] == "Same video"
        assert rows["trimmed.mp4"].startswith("Missing ")
        assert w.tabs.isTabEnabled(1) and w.shared.topLevelItemCount() >= 1

        # copy tags into the kept copy (runs in the background)
        w.copies.setCurrentItem(next(r for r in w._rows() if r.text(0) == "original.mp4"))
        w._merge()
        end = time.time() + 30
        while "Lake" not in read_state(folder / "original.mp4").keywords and time.time() < end:
            app.processEvents()
            time.sleep(0.02)
        assert read_state(folder / "original.mp4").keywords == ["Lake"]

        # tick the others and recycle them
        w._tick_others()
        assert sorted(w._ticked()) == sorted([str(folder / "reencoded.mkv"), str(folder / "trimmed.mp4")])
        w._recycle()
        assert sorted(recycled) == ["reencoded.mkv", "trimmed.mp4"] and w.groups == []
        assert {Path(r.path).name for r in db.library()} == {"original.mp4", "unrelated.mp4", "shares_one.mp4"}
        w.close()
    finally:
        db.close()
