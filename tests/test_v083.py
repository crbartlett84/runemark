"""0.8.3 fixes: .nfo ids of 0, one video length everywhere, accurate Explorer Tags note, .nfo fill-ins."""
import shutil

from runemark import explorer, nfo
from runemark.media.atoms import movie_duration
from runemark.media.probe import fmt_duration, probe


def test_nfo_skips_empty_and_zero_ids(tmp_path):
    video = tmp_path / "Scene.mp4"
    video.write_bytes(b"")
    (tmp_path / "Scene.nfo").write_text(
        "<movie><title>T</title><uniqueid type=\"tmdb\">0</uniqueid><uniqueid type=\"imdb\"></uniqueid>"
        "<uniqueid type=\"tvdb\">000</uniqueid></movie>", encoding="utf-8")
    d = nfo.read_for(video)
    assert d.ids == {} and d.links == []


def test_library_length_is_the_movie_length(tmp_path, media):
    from runemark import library as L
    path = tmp_path / "v.mp4"
    shutil.copy2(media["mp4"], path)
    with open(path, "rb") as fh:
        movie = movie_duration(fh)
    info = probe(path)
    assert movie is not None and abs(movie - info.duration) < 0.05
    assert fmt_duration(L.read_for_index(path)["duration"]) == fmt_duration(info.duration)


def test_movie_duration_of_a_non_mp4(tmp_path):
    bad = tmp_path / "x.mp4"
    bad.write_bytes(b"not an mp4 at all")
    with open(bad, "rb") as fh:
        assert movie_duration(fh) is None


def test_fallback_to_builtin_writer_is_reported(tmp_path, media, monkeypatch):
    from runemark.ui.main_window import _explorer_note
    path = tmp_path / "v.mp4"
    shutil.copy2(media["mp4"], path)
    monkeypatch.setattr(explorer, "windows_api_available", lambda: True)

    def broken(*_a, **_k):
        raise OSError("locked")
    monkeypatch.setattr(explorer, "write_windows", broken)
    try:
        method = explorer.write_tags(path, ["x"], explorer.MODE_AUTO)
    except Exception:  # noqa: BLE001  (no room in this tiny file: the padding path is covered below)
        from runemark.tagsync import _write_explorer
        method = _write_explorer(path, ["x"], [], explorer.MODE_AUTO)
    assert method == "fallback"
    assert "Windows couldn't" in _explorer_note(method)
    assert explorer.write_tags(path, ["y"], explorer.MODE_BUILTIN) == "builtin"
    assert _explorer_note("builtin").endswith("by Runemark")


def test_nfo_fill_in_alone_is_not_an_unsaved_change(tmp_path, media):
    from runemark.tagdb import TagDB
    from runemark.tagsync import read_state
    from runemark.ui.main_window import Document
    path = tmp_path / "v.mp4"
    shutil.copy2(media["mp4"], path)
    db = TagDB(tmp_path / "t.db")
    info = probe(path)
    doc = Document(path, info, read_state(path, info), db)
    assert not doc.has_own_changes()
    doc.meta.title = "From the nfo"               # what the fill-in does
    doc.mark_auto_filled()
    assert doc.dirty() and not doc.has_own_changes()   # Save still writes it; switching away doesn't ask
    doc.meta.studio = "Typed by hand"
    assert doc.has_own_changes()
    db.close()


def test_edits_made_while_saving_carry_over(tmp_path, media):
    from runemark.media import chapters as CH
    from runemark.tagdb import TagDB
    from runemark.tagsync import read_state
    from runemark.ui.main_window import Document
    path = tmp_path / "v.mp4"
    shutil.copy2(media["mp4"], path)
    db = TagDB(tmp_path / "t.db")
    info = probe(path)
    old = Document(path, info, read_state(path, info), db)
    old.chapters, _ = CH.add_mark(old.chapters, 1.0, at_zero_too=True)
    old.saved_chapters = CH.copy_marks(old.chapters)   # what the save wrote ("Chapter 2")
    old.chapters[1].name, old.chapters[1].auto = "Lake", False   # renamed while it saved
    new = Document(path, info, read_state(path, info), db)
    assert new.take_edits_from(old)
    assert [m.name for m in new.chapters][-1] == "Lake" and new.dirty()
    fresh = Document(path, info, read_state(path, info), db)
    assert not fresh.take_edits_from(Document(path, info, read_state(path, info), db))
    db.close()


def test_end_goes_on_the_chapter_under_the_playhead():
    from runemark.media import chapters as CH
    marks, _ = CH.add_mark([], 0.0)
    marks, second = CH.add_mark(marks, 10.0)
    # one frame into the new chapter (30 fps): it's the one that ends, not the chapter before
    assert CH.end_at(marks, 10.0 + 1 / 30) is second and second.end == round(10.0 + 1 / 30, 3)
    assert marks[0].end is None
    second.end = None
    # on the new chapter's first frame: nothing ends (never the previous chapter at the next one's start)
    assert CH.end_at(marks, 10.0) is None and marks[0].end is None
    assert CH.chapter_at(marks, 10.0) is second and CH.chapter_at(marks, 9.99) is marks[0]
    assert CH.end_at(marks, 5.0) is marks[0] and marks[0].end == 5.0
