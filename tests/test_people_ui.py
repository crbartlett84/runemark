"""v0.4.0 round 2: the People window, the People box beside a video, the editor's save, the Library."""
import shutil

import pytest
from PySide6.QtWidgets import QInputDialog, QMessageBox

from runemark import library as L
from runemark import people as P
from runemark.media.mp4meta import VideoMeta
from runemark.media.probe import probe
from runemark.settings import Settings
from runemark.tagdb import TagDB
from runemark.tagsync import read_state, save_mp4


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def db(tmp_path):
    d = TagDB(tmp_path / "tags.db")
    yield d
    d.close()


@pytest.fixture
def yes(monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"unexpected warning: {a[2]}"))


def video(tmp_path, media, name="a.mp4", year=2018, tags=()):
    folder = tmp_path / "v"
    folder.mkdir(exist_ok=True)
    shutil.copy2(media["mp4"], folder / name)
    save_mp4(folder / name, keywords=list(tags), genre=[], meta=VideoMeta("", year, "", 0))
    return folder / name


# --- the People window ---------------------------------------------------------------------------------------

def test_people_window_edits_and_offers_the_update(tmp_path, media, db, qapp, yes, monkeypatch):
    from runemark.ui.people_dialog import PeopleDialog
    f = video(tmp_path, media)
    ana = db.add_person("Ana Lee")
    P.save_people(db, f, [(ana, [])], label="test")
    other = db.add_person("Nobody Here")                       # not in any video: nothing to update

    dialog = PeopleDialog(db, select_uid=ana.uid)
    assert dialog.name.text() == "Ana Lee" and "1 video" in dialog.usage.text()
    dialog.aliases.setText("Annie, Ana L.")
    dialog._save_names()
    dialog.attr_combo.setCurrentText("Hair")                   # a new attribute, created on the spot
    dialog.value_edit.setText("Red")
    dialog.tag_edit.setText("Red hair")
    dialog.from_edit.setText("2010")
    dialog._add_value()
    assert db.person(ana.id).aliases == ["Ana L.", "Annie"]
    (value,) = db.person(ana.id).values
    assert (value.attribute, value.value, value.tag, value.from_year) == ("Hair", "Red", "Red hair", 2010)
    assert dialog.values.rowCount() == 1

    emitted = []
    dialog.filesNeedUpdate.connect(lambda *args: emitted.append(args))
    dialog.done(0)
    (paths, label, drop) = emitted[0]
    assert paths == [str(f)] and label == "People: changed Ana Lee" and drop == []
    assert P.refresh_files(db, paths, label=label, drop=set(drop)) == []
    assert read_state(f).keywords == ["Ana Lee", "Red hair"]
    assert other.id not in {p.id for p in db.people() if db.files_with_person(p.id)}


def test_people_window_rejects_a_name_someone_else_has(db, qapp, monkeypatch):
    from runemark.ui.people_dialog import PeopleDialog
    db.add_person("Ana Lee", ["Annie"])
    bea = db.add_person("Bea Cole")
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    dialog = PeopleDialog(db, select_uid=bea.uid)
    dialog.aliases.setText("annie")
    dialog._save_names()
    assert warnings and "Ana Lee" in warnings[0]
    assert db.person(bea.id).aliases == [] and dialog.aliases.text() == ""     # the form shows what's saved
    dialog.done(0)


def test_deleting_a_person_takes_them_out_of_their_videos(tmp_path, media, db, qapp, yes):
    from runemark.ui.people_dialog import PeopleDialog
    f = video(tmp_path, media, tags=["Lake"])
    ana = db.add_person("Ana Lee")
    P.save_people(db, f, [(ana, [])], label="test")
    dialog = PeopleDialog(db, select_uid=ana.uid)
    emitted = []
    dialog.filesNeedUpdate.connect(lambda *args: emitted.append(args))
    dialog._delete_person()
    dialog.done(0)
    assert db.people() == []
    (paths, label, drop) = emitted[0]
    assert drop == [ana.uid] and "removed Ana Lee" in label
    P.refresh_files(db, paths, label=label, drop=set(drop))
    state = read_state(f)
    assert state.keywords == ["Lake"] and state.data is None


def test_attributes_window(tmp_path, media, db, qapp, yes, monkeypatch):
    from runemark.ui.people_dialog import AttributesDialog
    hair = db.add_attribute("Hair")
    ana = db.add_person("Ana Lee")
    db.add_person_value(ana.id, hair.id, "Red", tag="Red hair")
    dialog = AttributesDialog(db)
    dialog.table.selectRow(0)
    dialog.table.cellWidget(0, 1).setCurrentText("hair_color")
    assert db.find_attribute("Hair").stash_field == "hair_color" and dialog.affected_people == {ana.id}
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Hair colour", True))
    dialog._rename()
    assert db.find_attribute("Hair colour") is not None
    dialog.table.selectRow(0)
    dialog._delete()
    assert db.attributes() == [] and db.person(ana.id).values == []


# --- the People box beside a video ---------------------------------------------------------------------------

def test_people_box_shows_profile_tags_and_not_in_this_video(qapp):
    from runemark.ui.tag_editor import TagEditor
    editor = TagEditor()
    editor.set_enabled(True)
    editor.set_tags(["Lake"])
    events = []
    editor.personRemoved.connect(lambda uid: events.append(("removed", uid)))
    editor.skipToggled.connect(lambda p, v: events.append(("skip", p, v)))
    editor.personEntered.connect(lambda text: events.append(("entered", text)))
    editor.set_people([("u1", "Ana Lee", "")], [
        {"tag": "Ana Lee", "skipped": False, "givers": [("u1", "Ana Lee", None, "their name")]},
        {"tag": "Red hair", "skipped": False, "givers": [("u1", "Ana Lee", "v1", "Hair: Red")]},
        {"tag": "Freckles", "skipped": True, "givers": [("u1", "Ana Lee", "v2", "Marks: Freckles")]},
        {"tag": "Lake", "skipped": False, "givers": [("u1", "Ana Lee", "v3", "Place: Lake")]},  # typed too
    ])
    chips = [editor.chips.itemAt(i).widget() for i in range(editor.chips.count())]
    assert [(c.text, c.property("profile"), c.property("skipped")) for c in chips] == [
        ("Lake", None, None), ("Ana Lee", "true", "false"), ("Red hair", "true", "false"),
        ("Freckles", "true", "true")]
    assert editor.count_label.text() == "3 tags"               # Lake, Ana Lee, Red hair (Freckles is off)
    (person_chip,) = [editor.people_chips.itemAt(i).widget() for i in range(editor.people_chips.count())]
    person_chip.removeClicked.emit("Ana Lee")
    editor.person_input.setText("  Annie ")
    editor._commit_person()
    assert events == [("removed", "u1"), ("entered", "Annie")]


def test_editor_document_combines_hand_tags_and_people(tmp_path, media, db, qapp):
    from runemark.ui.main_window import Document
    f = video(tmp_path, media, tags=["Lake", "Freckles"])
    hair, marks = db.add_attribute("Hair"), db.add_attribute("Marks")
    ana = db.add_person("Ana Lee")
    red = db.add_person_value(ana.id, hair.id, "Red", tag="Red hair")
    db.add_person_value(ana.id, marks.id, "Freckles")
    ana = db.person(ana.id)
    P.save_people(db, f, [(ana, [])], label="test")

    doc = Document(f, probe(f), read_state(f), db)
    assert doc.tags == ["Lake", "Freckles"]                    # hand tags only (Freckles was typed by hand too)
    assert not doc.dirty()
    doc.people = [(ana, [red.uid])]                            # "not in this video"
    assert doc.dirty()
    tags, block = doc.compose(db)
    assert tags == ["Lake", "Freckles", "Ana Lee"] and block["people"][0]["skip"] == [red.uid]
    doc.people = []
    tags, block = doc.compose(db)
    assert tags == ["Lake", "Freckles"] and block is None
    doc.discard()
    assert doc.people == doc.saved_people and not doc.dirty()


# --- the Library ---------------------------------------------------------------------------------------------

def test_library_people_column_and_search_by_alias(tmp_path, media, db, qapp):
    from runemark.ui.library_model import COLUMNS
    from runemark.ui.library_window import LibraryWindow
    a = video(tmp_path, media, "a.mp4")
    video(tmp_path, media, "b.mp4")
    ana = db.add_person("Ana Lee", ["Annie"])
    P.save_people(db, a, [(ana, [])], label="test")
    L.scan(db, [str(tmp_path / "v")])
    w = LibraryWindow(db, Settings.load(), host=None)
    try:
        col = COLUMNS.index("People")
        from pathlib import Path
        rows = {Path(w.model.record(r).path).name: w.model.data(w.model.index(r, col))
                for r in range(w.model.rowCount())}
        assert rows == {"a.mp4": "Ana Lee", "b.mp4": ""}
        w._set_search(L.Search([], "annie", L.FILTER_ALL))
        w._apply()
        assert w.model.rowCount() == 1 and w.model.record(0).people == ["Ana Lee"]
    finally:
        w.close()


def test_no_button_in_the_people_window_takes_enter(db, qapp):
    """Enter in the value box adds the value; it mustn't also press New person… (found in the click-through)."""
    from PySide6.QtWidgets import QPushButton
    from runemark.ui.people_dialog import PeopleDialog
    dialog = PeopleDialog(db)
    assert all(not b.autoDefault() and not b.isDefault() for b in dialog.findChildren(QPushButton))
    dialog.done(0)
