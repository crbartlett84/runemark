"""0.15.0: a video changed with no window (runemark.headless), as a plugin's change_file changes it."""
import io
import shutil

import pytest
from PIL import Image

from runemark import headless, people as people_io, saving
from runemark.tagdb import TagDB
from runemark.tagsync import read_state


def _jpeg(size=(320, 200), colour=(200, 30, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "JPEG")
    return out.getvalue()


@pytest.fixture
def db(tmp_path):
    d = TagDB(tmp_path / "tags.db")
    yield d
    d.close()


def test_change_and_write(tmp_path, media, db):
    """Details, tags, a genre, a person with a profile tag and a thumbnail, written by write_copy and read back."""
    path = tmp_path / "a.mp4"
    shutil.copy2(media["mp4"], path)
    assert headless.ensure_tag(db, "Lake", ["Places"]) == "Lake"
    assert headless.ensure_tag(db, "Sex Scene", ["Genre"], genre=True) == "Sex Scene"
    assert headless.ensure_tag(db, "lake", ["Elsewhere"]) == "Lake"            # the database's spelling, its category
    name = headless.add_person(db, "Ann Example", aliases=["Annie"], links=["https://example.org/ann"],
                               photo=_jpeg((300, 500)), values={"Hair": ("Red", "Red Hair"), "Born": "1990-01-01"},
                               uid="11111111-2222-3333-4444-555555555555")
    assert name == "Ann Example"
    person = db.find_person("annie")
    assert person.uid == "11111111-2222-3333-4444-555555555555"
    photo = Image.open(io.BytesIO(db.person_photo(person.id)))
    assert photo.size == (headless.PHOTO_SIZE, headless.PHOTO_SIZE)

    request, done, edit = headless.change(path, db, {
        "title": "Studio - A  Title", "date": "2024-05-06", "studio": "Studio", "description": " About it. ",
        "add_links": ["https://stashdb.org/scenes/00000000-0000-0000-0000-000000000001"],
        "add_tags": ["lake", "Sex Scene"], "people": ["Ann Example", "Mister No"],
        "cover_if_missing": _jpeg()}, person_ok=lambda n: False if n == "Mister No" else None)
    assert request is not None and {"title", "date", "studio", "description", "links", "tags", "people"} <= set(done)
    assert db.find_person("Mister No") is None                                 # ruled out, never created
    assert request.expected_size == path.stat().st_size and request.expected_fp
    assert "Lake" in request.keywords and "Ann Example" in request.keywords
    assert request.genre == ["Sex Scene"]
    out = tmp_path / "out.mp4"
    problems = saving.write_copy(request, path, out)
    assert not problems
    state = read_state(out)
    assert state.meta.title == "Studio - A Title" and state.meta.date == "2024-05-06" and state.meta.year == 2024
    assert state.meta.studio == "Studio" and state.meta.description == "About it."
    assert {t.casefold() for t in state.keywords} >= {"lake", "sex scene", "ann example"}
    assert any(e["id"] == person.uid for e in people_io.entries_of(state.data))
    assert state.genre == ["Sex Scene"]

    # the same change again on the result: nothing left to save
    again, _done, _edit = headless.change(out, db, {"title": "Studio - A Title", "add_tags": ["Lake"],
                                                    "people": ["Ann Example"]})
    assert again is None


def test_same_as_the_editor(tmp_path, media, db):
    """The request is what the editor's own Document composes for the same changes."""
    pytest.importorskip("PySide6")
    from runemark.media.probe import probe
    from runemark.tagsync import route_fields
    from runemark.ui.main_window import Document
    path = tmp_path / "a.mp4"
    shutil.copy2(media["mp4"], path)
    headless.ensure_tag(db, "Lake", ["Places"])
    headless.add_person(db, "Ann Example", values={"Hair": ("Red", "Red Hair")})
    changes = {"title": "T", "year": 2020, "add_tags": ["Lake", "Unfiled"], "people": ["Ann Example"]}
    request, _done, _edit = headless.change(path, db, changes)
    info = probe(path)
    doc = Document(path, info, read_state(path, info), db)
    headless.apply_changes(doc, db, changes)
    tags, block = doc.compose(db)
    keywords, genre = route_fields(tags, is_genre=db.is_genre_tag, is_known=lambda n: db.find_tag(n) is not None,
                                   original_genre=doc.state.genre)
    assert (request.keywords, request.genre, request.data) == (keywords, genre, block)
    assert request.meta.key() == doc.meta.key()


def test_mkv_has_no_request(tmp_path, media, db):
    if "mkv" not in media:
        pytest.skip("no MKV fixture")
    path = tmp_path / "a.mkv"
    shutil.copy2(media["mkv"], path)
    request, done, _edit = headless.change(path, db, {"title": "T"})
    assert request is None and done == ["title"]


def test_backup_database_api(tmp_path, db):
    """api.backup_database writes a copy another TagDB opens."""
    from types import SimpleNamespace
    from runemark.plugins import API_VERSION, PluginAPI
    assert API_VERSION >= 8
    headless.add_person(db, "Ann Example")
    api = PluginAPI.__new__(PluginAPI)
    api._w = SimpleNamespace(db=db)
    out = api.backup_database(tmp_path / "copy" / "tags.db")
    other = TagDB(out)
    try:
        assert other.find_person("Ann Example") is not None
    finally:
        other.close()
