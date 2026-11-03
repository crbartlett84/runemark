"""0.6.0: tag rules and keys, bulk details, the palette, people photos, chapter thumbnails, grid covers, the health
check, stats, the activity log and the updater."""
import hashlib
import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from runemark import library as L
from runemark import people as P
from runemark.media.mp4meta import VideoMeta
from runemark.tagdb import TagDB
from runemark.tagsync import read_state, save_mp4



def jpeg_bytes(color=(200, 80, 40), size=(64, 64)) -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG")
    return out.getvalue()


@pytest.fixture
def db(tmp_path):
    d = TagDB(tmp_path / "t.db")
    yield d
    d.close()


# --- tag rules ------------------------------------------------------------------------------------------------

def test_tag_rules_follow_chains_survive_merges_and_travel_in_exports(db, tmp_path):
    cat = db.add_category("Acts").id
    cowgirl, sex, hard, reverse = (db.add_tag(n, cat) for n in ("Cowgirl", "Sex", "Hardcore", "Reverse cowgirl"))
    db.set_implied(cowgirl.id, [sex.id])
    db.set_implied(sex.id, [hard.id, sex.id])               # a rule pointing at itself is ignored
    db.set_implied(hard.id, [cowgirl.id])                   # a loop is harmless
    out = db.with_implied(["Cowgirl"])
    assert out == ["Cowgirl", "Sex", "Hardcore"]
    db.set_implied(reverse.id, [sex.id])
    db.merge_tags(reverse.id, cowgirl.id)                   # merged away: its rules move to the target
    assert [t.name for t in db.implied(cowgirl.id)] == ["Sex"]
    data = db.export_data()
    assert data["rules"]["Cowgirl"] == ["Sex"]
    other = TagDB(tmp_path / "o.db")
    try:
        other.import_data(data)
        assert other.with_implied(["Cowgirl"]) == ["Cowgirl", "Sex", "Hardcore"]
    finally:
        other.close()


def test_bulk_add_applies_tag_rules(db, tmp_path, media):
    cat = db.add_category("Acts").id
    a, b = db.add_tag("Cowgirl", cat), db.add_tag("Sex", cat)
    db.set_implied(a.id, [b.id])
    path = tmp_path / "v.mp4"
    shutil.copy2(media["mp4"], path)
    assert L.apply_bulk(db, [path], L.BulkChange(add=["cowgirl"])) == []
    assert read_state(path).keywords == ["Cowgirl", "Sex"]


def test_tag_editor_adds_implied_tags_and_toggles(db):
    from PySide6.QtWidgets import QApplication
    from runemark.ui.tag_editor import TagEditor
    QApplication.instance() or QApplication([])
    cat = db.add_category("Acts").id
    a, b = db.add_tag("Cowgirl", cat), db.add_tag("Sex", cat)
    db.set_implied(a.id, [b.id])
    editor = TagEditor()
    editor.set_db(db)
    editor.set_enabled(True)
    told = []
    editor.impliedAdded.connect(lambda tag, extra: told.append((tag, extra)))
    editor.add_tag("cowgirl")
    assert editor.tags == ["Cowgirl", "Sex"] and told == [("Cowgirl", ["Sex"])]
    assert editor.toggle_tag("Cowgirl") is False and editor.tags == ["Sex"]
    assert editor.toggle_tag("Cowgirl") is True and editor.tags == ["Sex", "Cowgirl"]
    editor.set_tag_keys({"1": "Cowgirl"})
    assert "Alt+1 Cowgirl" in editor.keys_line.text()


# --- the palette ----------------------------------------------------------------------------------------------

def test_palette_ranks_prefix_matches_first():
    from runemark.ui.palette import Entry, rank
    noop = lambda: None  # noqa: E731
    entries = [Entry("Video", "Beach day", "", noop, "beach.mp4"), Entry("Tag", "Beach", "", noop),
               Entry("Action", "Library", "Ctrl+B", noop), Entry("Person", "Ana Lee", "", noop, "Annie")]
    assert [e.text for e in rank(entries, "")] == ["Library"]                 # nothing typed: the actions
    assert [e.text for e in rank(entries, "bea")] == ["Beach", "Beach day"]
    assert [e.text for e in rank(entries, "annie")] == ["Ana Lee"]           # aliases match


# --- people photos --------------------------------------------------------------------------------------------

def test_photos_travel_in_files_and_come_back_to_a_new_library(tmp_path, media, db):
    ana = db.add_person("Ana Lee")
    photo = jpeg_bytes()
    db.set_person_photo(ana.id, photo)
    path = tmp_path / "v.mp4"
    shutil.copy2(media["mp4"], path)
    save_mp4(path, keywords=[], genre=[], meta=VideoMeta())
    assert P.save_people(db, path, [(db.person(ana.id), [])], label="t") == []
    (entry,) = read_state(path).data["people"]
    assert P.photo_of(entry) == photo
    fresh = TagDB(tmp_path / "fresh.db")
    try:
        (pid,) = P.resolve_people(fresh, read_state(path).data)
        assert fresh.person_photo(pid) == photo
    finally:
        fresh.close()
    assert P.photo_of({"photo": "bm90IGEganBlZw=="}) is None            # "not a jpeg"
    db.set_person_photo(ana.id, None)
    assert db.person_photo(ana.id) is None and "photo" not in P.person_entry(db, db.person(ana.id))


def test_crop_view_keeps_a_square_inside_the_picture():
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication
    from runemark.ui.photo_crop import CropView, to_jpeg
    QApplication.instance() or QApplication([])
    image = QImage(400, 200, QImage.Format.Format_RGB888)
    image.fill(0x336699)
    view = CropView(image)
    view.box = view._clamp(QRectF(350, 150, 500, 90))
    assert view.box.width() == view.box.height() == 200 and view.box.right() <= 400 and view.box.x() >= 0
    data = to_jpeg(view.cropped())
    out = QImage()
    out.loadFromData(data)
    assert data[:2] == b"\xff\xd8" and out.width() == out.height() == 240


def test_stash_gets_the_photo_only_when_the_performer_has_none():
    import importlib.util
    import sys
    plugin = Path(__file__).resolve().parents[1] / "integrations" / "stash" / "runemark"
    for name in ("runemark_read", "runemark_import"):
        spec = importlib.util.spec_from_file_location(name, plugin / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    imp = sys.modules["runemark_import"]
    person = {"name": "Ana", "aliases": [], "values": [], "photo": "QUJD"}
    assert imp.performer_fields(person, None, False)["image"] == "data:image/jpeg;base64,QUJD"
    placeholder = {"name": "Ana", "image_path": "http://x/performer/1/image?default=true"}
    assert "image" in imp.performer_fields(person, placeholder, False)
    own = {"name": "Ana", "image_path": "http://x/performer/1/image?t=123"}
    assert "image" not in imp.performer_fields(person, own, True)          # never replaces a real picture


# --- bulk details, activity -----------------------------------------------------------------------------------

def test_bulk_details_set_studio_year_and_add_people_in_one_undoable_change(tmp_path, media, db):
    from runemark import names as N
    paths = []
    for n in ("a.mp4", "b.mp4"):
        shutil.copy2(media["mp4"], tmp_path / n)
        save_mp4(tmp_path / n, keywords=["Lake"], genre=[], meta=VideoMeta("T", 2001, "", 0, "Old"))
        paths.append(tmp_path / n)
    plans = [N.ReadPlan(str(p), year=2019, studio="New", people=["Ana Lee"], matched=True) for p in paths]
    assert N.apply_read(db, plans, overwrite=True, label="Library: set details") == []
    for p in paths:
        st = read_state(p)
        assert (st.meta.title, st.meta.year, st.meta.studio) == ("T", 2019, "New")
        assert [x.name for x, _ in P.current_people(db, st.data)] == ["Ana Lee"]
    acts = db.activity()
    assert [a["label"] for a in acts[:2]] == ["Library: set details"] * 2 and acts[0]["batch"] == acts[1]["batch"]
    entries = db.last_batch()
    assert len(entries) == 2 and L.undo_batch(db, entries) == []
    assert read_state(paths[0]).meta.studio == "Old"
    assert db.activity()[0]["label"].startswith("Undone: Library: set details")


def test_activity_groups_batches_and_same_minute_saves():
    from runemark.ui.activity_dialog import group
    rows = [{"at": "2026-11-02T10:00:05", "label": "Saved in the editor", "path": "c", "batch": None},
            {"at": "2026-11-02T09:59:00", "label": "Library: add Lake", "path": "b", "batch": "x"},
            {"at": "2026-11-02T09:59:00", "label": "Library: add Lake", "path": "a", "batch": "x"},
            {"at": "2026-11-02T09:00:00", "label": "Saved in the editor", "path": "c", "batch": None}]
    out = group(rows)
    assert [(g["label"], len(g["paths"])) for g in out] == [("Saved in the editor", 1), ("Library: add Lake", 2),
                                                            ("Saved in the editor", 1)]


# --- grid covers, stats, health -------------------------------------------------------------------------------

def test_cover_thumbnails_are_cached(tmp_path, media):
    from runemark import covers
    path = tmp_path / "v.mp4"
    shutil.copy2(media["mp4"], path)
    folder = tmp_path / "cache"
    folder.mkdir()
    assert covers.thumbnail(path, folder) is None and len(list(folder.iterdir())) == 1     # "no cover", cached
    save_mp4(path, keywords=[], genre=[], cover=jpeg_bytes(size=(1280, 720)))
    data = covers.thumbnail(path, folder)
    from PIL import Image
    assert Image.open(io.BytesIO(data)).size == (320, 180)
    assert covers.thumbnail(path, folder) == data


def test_stats(tmp_path, media, db):
    from runemark import stats as S
    folder = tmp_path / "v"
    folder.mkdir()
    for n, tags in (("a.mp4", ["Lake"]), ("b.mp4", ["Lake", "Sun"]), ("c.mp4", [])):
        shutil.copy2(media["mp4"], folder / n)
        if tags:
            save_mp4(folder / n, keywords=tags, genre=[], meta=VideoMeta("", None, "", 3, "Sunny"))
    L.scan(db, [str(folder)])
    s = S.compute(db.library())
    assert (s.total, s.tagged, s.rated, s.with_studio, s.inbox) == (3, 2, 2, 2, 3)
    assert s.top_tags[0] == ("Lake", 2) and s.top_studios == [("Sunny", 2)] and len(s.added_by_month) == 1
    from PySide6.QtWidgets import QApplication
    from runemark.ui.stats_dialog import html
    QApplication.instance() or QApplication([])
    assert "3 videos" in html(s)


def test_health_check_finds_and_fixes(tmp_path, media, db):
    from runemark import health as H
    folder = tmp_path / "v"
    folder.mkdir()
    a, b, c = folder / "a.mp4", folder / "b.mp4", folder / "c.mp4"
    for p in (a, b, c):
        shutil.copy2(media["mp4"], p)
    hair = db.add_attribute("Hair")
    ana = db.add_person("Ana Lee")
    db.add_person_value(ana.id, hair.id, "Red", tag="Red hair")
    save_mp4(a, keywords=[], genre=[], meta=VideoMeta())
    P.save_people(db, a, [(db.person(ana.id), [])], label="t")
    db.add_person_value(ana.id, hair.id, "Long", tag="Long hair")          # profile changed since a was saved
    b.with_suffix(".nfo").write_text("<movie><title>Beach</title><studio>Sunny</studio></movie>", encoding="utf-8")
    L.scan(db, [str(folder)])
    c.unlink()
    L.scan(db, [str(folder)])
    issues = H.check(db, db.library())
    kinds = {(Path(i.path).name, i.kind) for i in issues}
    assert ("a.mp4", H.PEOPLE_STALE) in kinds and ("b.mp4", H.NFO_UNUSED) in kinds and ("c.mp4", H.MISSING) in kinds
    stale = next(i for i in issues if i.kind == H.PEOPLE_STALE)
    assert "Long hair" in stale.detail
    assert H.fix(db, issues) == []
    assert "Long hair" in read_state(a).keywords
    assert (read_state(b).meta.title, read_state(b).meta.studio) == ("Beach", "Sunny")
    assert not any(Path(r.path).name == "c.mp4" for r in db.library())
    left = {i.kind for i in H.check(db, db.library())}
    assert left <= {H.NO_COVER}


# --- updater --------------------------------------------------------------------------------------------------

def _release_json(sha="ab" * 32, version="9.9.9"):
    return {"tag_name": f"v{version}", "body": "Notes", "html_url": "https://github.com/crbartlett84/runemark/x",
            "assets": [{"name": "other.txt"},
                       {"name": f"Runemark-{version}-win64.zip", "size": 10, "digest": f"sha256:{sha}",
                        "browser_download_url": f"https://github.com/crbartlett84/runemark/releases/download/"
                                                f"v{version}/Runemark-{version}-win64.zip"}]}


def test_check_parses_the_latest_release():
    from runemark import updater as U
    rel = U.check(lambda url: json.dumps(_release_json()).encode())
    assert rel.version == "9.9.9" and rel.newer and rel.sha256 == "ab" * 32 and rel.notes == "Notes"
    assert U.parse_version("v0.10.2") > U.parse_version("0.9.9")
    old = U.parse_release(_release_json(version="0.0.1"))
    assert not old.newer
    assert U.parse_release({"assets": []}) is None
    ok, why = U.can_install()
    assert not ok and "source" in why                      # tests run from source, never the packaged app


class _Resp(io.BytesIO):
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _zip(files: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return out.getvalue()


def test_download_is_checked_against_the_published_checksum(tmp_path, monkeypatch):
    from runemark import updater as U
    payload = _zip({"Runemark.exe": b"exe", "_internal/x.dll": b"dll"})
    monkeypatch.setattr(U.urllib.request, "urlopen", lambda req, timeout=0: _Resp(payload))
    good = U.parse_release(_release_json(hashlib.sha256(payload).hexdigest()))
    zip_path = U.download(good, tmp_path / "dl" / "r.zip")
    assert zip_path.read_bytes() == payload
    bad = U.parse_release(_release_json("00" * 32))
    with pytest.raises(U.UpdateError, match="doesn't match"):
        U.download(bad, tmp_path / "dl" / "bad.zip")
    assert not (tmp_path / "dl" / "bad.zip").exists()
    none = U.parse_release(_release_json())
    none.sha256 = None
    with pytest.raises(U.UpdateError, match="checksum"):
        U.download(none, tmp_path / "dl" / "n.zip")
    elsewhere = U.parse_release(_release_json(hashlib.sha256(payload).hexdigest()))
    elsewhere.url = "https://example.com/Runemark-9.9.9-win64.zip"
    with pytest.raises(U.UpdateError, match="releases page"):
        U.download(elsewhere, tmp_path / "dl" / "e.zip")


def test_stage_unpacks_beside_the_app_and_refuses_bad_zips(tmp_path):
    from runemark import updater as U
    app = tmp_path / "Apps" / "Runemark"
    app.mkdir(parents=True)
    z = tmp_path / "r.zip"
    z.write_bytes(_zip({"Runemark.exe": b"exe", "_internal/x.dll": b"dll"}))
    staged = U.stage(z, app)
    assert staged == app.parent / U.STAGE_NAME and (staged / "Runemark.exe").is_file()
    z.write_bytes(_zip({"Runemark/Runemark.exe": b"exe"}))             # a top folder is fine too
    assert (U.stage(z, app) / "Runemark.exe").is_file()
    z.write_bytes(_zip({"readme.txt": b"no app"}))
    with pytest.raises(U.UpdateError, match="Runemark.exe"):
        U.stage(z, app)
    z.write_bytes(_zip({"../evil.txt": b"x", "Runemark.exe": b"exe"}))
    with pytest.raises(U.UpdateError, match="unsafe"):
        U.stage(z, app)
    assert not (tmp_path / "Apps" / "evil.txt").exists()


def test_swap_script_keeps_portable_data_and_rolls_back():
    from runemark import updater as U
    from pathlib import PureWindowsPath as W
    text = U.swap_script(W(r"C:\Apps\Runemark"), W(r"C:\Apps\Runemark-update"), 1234, W(r"C:\T\log.txt"))
    assert "Wait-Process -Id 1234" in text and "'C:\\Apps\\Runemark-previous'" in text
    assert "'Runemark-portable.txt', 'Runemark data'" in text
    assert "--update-failed" in text and "--updated" in text
    quoted = U.swap_script(W(r"C:\O'Brien\Runemark"), W(r"C:\O'Brien\Runemark-update"), 1, W("l"))
    assert "'C:\\O''Brien\\Runemark'" in quoted


def test_the_swap_runs_outside_the_app_folder_and_waits_out_locks(monkeypatch, tmp_path):
    """0.7.3: Runemark's working folder is usually its own folder (shortcuts start it there); a swap script started
    with that working folder locks the folder it has to rename ("in use"), so every update failed."""
    from pathlib import PureWindowsPath as W

    from runemark import updater as U
    text = U.swap_script(W(r"C:\Apps\Runemark"), W(r"C:\Apps\Runemark-update"), 1234, W(r"C:\T\log.txt"))
    assert "Set-Location" in text and text.count("Retry \"") >= 3
    assert "StartsWith($app + '\\'" in text                      # waits for tools still running from the folder
    started = {}
    monkeypatch.setattr("subprocess.Popen", lambda args, **kw: started.update(kw, args=args))
    monkeypatch.setattr(U.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(U, "app_folder", lambda: tmp_path / "Runemark")
    U.launch_swap(tmp_path / "Runemark-update")
    assert started["cwd"] == str(tmp_path) and started["args"][-1] == str(tmp_path / "runemark-update.ps1")
