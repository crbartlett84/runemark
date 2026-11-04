"""0.8.2: chapter ends ("End here") live in the data block, not the chapter track; the Stash plugin sends them."""
import importlib.util
import shutil
import sys
from pathlib import Path

from runemark import people as P
from runemark.media import chapters as CH
from runemark.media.probe import probe
from runemark.tagdb import TagDB
from runemark.tagsync import read_state, save_mp4, save_mp4_with_chapters

PLUGIN = Path(__file__).resolve().parents[1] / "integrations" / "stash" / "runemark"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, PLUGIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _marks():
    marks, _ = CH.add_mark([], 0.0)
    marks, b = CH.add_mark(marks, 10.0)
    marks, c = CH.add_mark(marks, 20.0)
    b.name, b.auto, c.name, c.auto = "Blowjob", False, "Missionary", False
    return CH.renumber(marks)


def test_end_at_ends_the_chapter_playing_and_stays_valid():
    marks = _marks()
    mark = CH.end_at(marks, 14.5)
    assert mark.name == "Blowjob" and mark.end == 14.5
    assert CH.ends_block(marks) == {"10.000": 14.5}
    assert CH.end_at(marks, 0.0) is None                       # nothing started yet
    marks[1].end = 25.0                                        # past the next chapter: not recorded
    assert CH.ends_block(marks) == {}
    copied = CH.copy_marks(_marks())
    copied[1].end = 12.0
    assert CH.copy_marks(copied)[1].end == 12.0                # copies keep the end
    assert CH.same_marks(_marks(), copied)                     # an end doesn't rewrite the chapter track


def test_apply_ends_reads_them_back():
    marks = CH.apply_ends(_marks(), {"format": P.FORMAT, "chapter_ends": {"10.000": 14.5}})
    assert [m.end for m in marks] == [None, 14.5, None]


def test_carry_keeps_ends_when_people_change():
    old = {"format": P.FORMAT, "version": P.VERSION, "people": [], "profile_tags": [], "chapter_ends": {"1.000": 2}}
    assert P.carry(None, old) == {"format": P.FORMAT, "version": P.VERSION, "chapter_ends": {"1.000": 2}}
    assert "chapter_ends" not in (P.carry(None, old, drop=("chapter_ends",)) or {})
    assert P.carry({"format": P.FORMAT, "people": [1]}, None) == {"format": P.FORMAT, "people": [1]}


def test_ends_survive_a_people_save_and_reach_stash(tmp_path, media):
    path = tmp_path / "v.mp4"
    shutil.copy2(media["mp4_cover"], path)
    marks = _marks()
    CH.end_at(marks, 14.5)
    state = read_state(path)
    block = {"format": P.FORMAT, "version": P.VERSION, "chapter_ends": CH.ends_block(marks)}
    save_mp4_with_chapters(path, probe(path), marks, keywords=["x"], genre=[], cover=state.cover, meta=state.meta,
                           data=block)
    assert read_state(path).data["chapter_ends"] == {"10.000": 14.5}
    # someone is added to the video: the end stays
    db = TagDB(tmp_path / "t.db")
    ana = db.add_person("Ana Lee")
    P.save_people(db, path, [(db.person(ana.id), [])], label="t")
    data = read_state(path).data
    assert data["chapter_ends"] == {"10.000": 14.5} and data["people"]
    db.close()
    # the chapter track itself is unchanged: still three chapters, no extra "end" one
    assert len(probe(path).chapters) == 3

    R = _load("runemark_read")
    I = _load("runemark_import")
    assert R.chapter_ends(R.read(str(path))) == {"10.000": 14.5}

    class S:
        def __init__(self):
            self.created, self.updated, self.tags = [], [], {}

        def tag_id(self, name):
            return self.tags.setdefault(name, str(len(self.tags) + 1))

        def q(self, query, variables=None):
            if "sceneMarkerCreate" in query:
                self.created.append(variables["i"])
            elif "sceneMarkerUpdate" in query:
                self.updated.append(variables["i"])
            return {}

    s = S()
    scene = {"id": "1", "scene_markers": []}
    I.import_chapters(s, scene, str(path), {}, R.chapter_ends(R.read(str(path))))
    by_start = {round(m["seconds"]): m["end_seconds"] for m in s.created}
    assert by_start[10] == 14.5                 # ended early
    assert abs(by_start[0] - 10.0) < 0.1        # no end: runs to the next chapter
    # markers made before the end was set get it brought up to date
    s2 = S()
    scene2 = {"id": "1", "scene_markers": [
        {"id": "m2", "seconds": 10.0, "end_seconds": 20.0, "title": "", "primary_tag": {"id": "7", "name": "Blowjob"}}]}
    I.import_chapters(s2, scene2, str(path), {}, R.chapter_ends(R.read(str(path))))
    assert [u["end_seconds"] for u in s2.updated] == [14.5]
    assert not any(round(c["seconds"]) == 10 for c in s2.created)


def test_editor_document_loads_compares_and_saves_ends(tmp_path, media):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from runemark.ui.main_window import Document
    path = tmp_path / "d.mp4"
    shutil.copy2(media["mp4_cover"], path)
    state = read_state(path)
    save_mp4_with_chapters(path, probe(path), _marks(), keywords=["x"], genre=[], cover=state.cover,
                           meta=state.meta, data={"format": P.FORMAT, "version": P.VERSION,
                                                  "chapter_ends": {"10.000": 14.5}})
    db = TagDB(tmp_path / "t.db")
    doc = Document(path, probe(path), read_state(path), db)
    assert [m.end for m in doc.chapters] == [None, 14.5, None] and not doc.dirty()
    CH.end_at(doc.chapters, 25.0)                         # end Missionary early
    assert doc.dirty() and doc.ends_changed() and not doc.chapters_changed()   # no chapter-track rewrite
    _tags, block = doc.compose(db)
    assert block["chapter_ends"] == {"10.000": 14.5, "20.000": 25.0}
    doc.chapters[1].end = None
    doc.chapters[2].end = None
    _tags, block = doc.compose(db)
    assert block is None or "chapter_ends" not in block   # all cleared: nothing left behind
    db.close()
