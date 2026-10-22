import pytest

from runemark.tagdb import DuplicateName, InvalidName, TagDB, TagDBError


@pytest.fixture
def db(tmp_path):
    database = TagDB(tmp_path / "tags.db")
    yield database
    database.close()


def test_nested_categories_and_genre_inheritance(db):
    genre = db.add_category("Genre", write_genre=True)
    action = db.add_category("Action", genre.id)
    people = db.add_category("People")
    db.add_tag("Heist", action.id)
    db.add_tag("Aubree", people.id)
    assert db.category_path(action.id) == ["Genre", "Action"]
    assert db.is_genre_tag("heist")          # inherited from the parent, case-insensitive
    assert not db.is_genre_tag("Aubree")
    db.set_write_genre(genre.id, False)
    assert not db.is_genre_tag("Heist")
    db.set_write_genre(action.id, True)
    assert db.is_genre_tag("Heist")


def test_tag_names_unique_across_categories(db):
    a = db.add_category("A")
    b = db.add_category("B")
    db.add_tag("Lake", a.id)
    with pytest.raises(DuplicateName):
        db.add_tag("lake", b.id)


@pytest.mark.parametrize("bad", ["", "   ", "a;b", "x" * 101, "bell\x07name"])
def test_invalid_names(db, bad):
    with pytest.raises(InvalidName):
        db.add_tag(bad)


def test_whitespace_is_normalised(db):
    assert db.add_tag("  Summer   2026 ").name == "Summer 2026"


def test_sibling_categories_unique_but_cousins_fine(db):
    a = db.add_category("A")
    b = db.add_category("B")
    db.add_category("Misc", a.id)
    db.add_category("Misc", b.id)
    with pytest.raises(DuplicateName):
        db.add_category("misc", a.id)


def test_move_category_rejects_cycles(db):
    a = db.add_category("A")
    b = db.add_category("B", a.id)
    c = db.add_category("C", b.id)
    with pytest.raises(TagDBError):
        db.move_category(a.id, c.id)
    db.move_category(c.id, None)
    assert db.category(c.id).parent_id is None


def test_delete_category_moves_contents_up(db):
    top = db.add_category("Top")
    mid = db.add_category("Mid", top.id)
    db.add_category("Leaf", mid.id)
    tag = db.add_tag("Kept", mid.id)
    db.delete_category(mid.id)
    assert db.tag(tag.id).category_id == top.id
    assert [c.name for c in db.children(top.id)] == ["Leaf"]


def test_rename_updates_remembered_files(db, tmp_path):
    tag = db.add_tag("Old")
    db.remember_file(tmp_path / "a.mp4", ["Old", "Other"])
    assert db.files_with_tags(["old"]) == [str(tmp_path / "a.mp4")]
    db.rename_tag(tag.id, "New")
    assert sorted(db.file_tags(tmp_path / "a.mp4")) == ["New", "Other"]


def test_rename_onto_existing_asks_for_merge(db):
    a = db.add_tag("One")
    db.add_tag("Two")
    with pytest.raises(DuplicateName) as info:
        db.rename_tag(a.id, "two")
    assert info.value.existing_id is not None


def test_merge_tags(db, tmp_path):
    src = db.add_tag("Src")
    dst = db.add_tag("Dst")
    db.remember_file(tmp_path / "a.mp4", ["Src"])
    db.remember_file(tmp_path / "b.mp4", ["Src", "Dst"])
    db.merge_tags(src.id, dst.id)
    assert db.find_tag("Src") is None
    assert db.file_tags(tmp_path / "a.mp4") == ["Dst"]
    assert db.file_tags(tmp_path / "b.mp4") == ["Dst"]


def test_move_file_and_usage(db, tmp_path):
    db.remember_file(tmp_path / "a.mkv", ["X", "Y"])
    db.move_file(tmp_path / "a.mkv", tmp_path / "a.mp4")
    assert db.remembered_files() == [str(tmp_path / "a.mp4")]
    assert db.tag_usage() == {"x": 1, "y": 1}


def test_export_import_roundtrip(db, tmp_path):
    genre = db.add_category("Genre", write_genre=True)
    sub = db.add_category("Drama", genre.id)
    db.add_tag("Slow Burn", sub.id)
    db.add_tag("Loose")
    db.export_json(tmp_path / "out.json")
    other = TagDB(tmp_path / "other.db")
    try:
        other.add_tag("Loose")  # already there: kept, not duplicated
        cats, tags = other.import_json(tmp_path / "out.json")
        assert (cats, tags) == (2, 1)
        assert other.is_genre_tag("Slow Burn")
        assert other.category_path(other.find_tag("Slow Burn").category_id) == ["Genre", "Drama"]
        again = other.import_json(tmp_path / "out.json")
        assert again == (0, 0)
    finally:
        other.close()


def test_backup_keeps_recent(db, tmp_path):
    folder = tmp_path / "bk"
    for _ in range(3):
        db.backup(folder, keep=2)
    assert len(list(folder.glob("tags-*.db"))) <= 2


def test_tabs_and_newlines_become_spaces(db):
    assert db.add_tag("tab\tname").name == "tab name"
