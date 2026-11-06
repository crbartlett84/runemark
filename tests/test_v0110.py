"""0.11.0: attributes can have choices (the values they allow, each with its tag), shown in People as drop-downs;
plugins set them, add buttons to People and set a person's values; the first chapter adds no chapter at 0:00."""
from __future__ import annotations

import pytest

from runemark import plugins
from runemark.settings import Settings
from runemark.tagdb import TagDB

from test_v070 import FakeWindow

HAIR = [("Black", "Black Hair"), ("Blonde", "Blonde Hair"), ("Red", "Red Hair")]


class Window(FakeWindow):
    def __init__(self, db, settings):
        super().__init__(db, settings)
        self.changed = []

    def people_changed(self, ids, now=False):
        self.changed.append((sorted(ids), now))


def test_choices_are_stored_cleaned_and_give_their_own_tag(tmp_path):
    db = TagDB(tmp_path / "t.db")
    hair = db.add_attribute("Hair")
    assert hair.choices == []
    hair = db.update_attribute(hair.id, choices=HAIR + [("  red ", "Other")])          # a repeat is dropped
    assert hair.choices == HAIR and hair.choice("RED") == ("Red", "Red Hair") and hair.choice("Teal") is None
    person = db.add_person("Ana Lee")
    v = db.add_person_value(person.id, hair.id, "red", tag="Typed tag")                 # an allowed value: its tag
    assert (v.value, v.tag) == ("Red", "Red Hair")
    v = db.update_person_value(v.id, value="blonde")
    assert (v.value, v.tag) == ("Blonde", "Blonde Hair")
    other = db.add_person_value(person.id, hair.id, "Teal")                             # from an old file: kept
    assert (other.value, other.tag) == ("Teal", "Teal")
    db.close()
    db = TagDB(tmp_path / "t.db")                                                       # survives reopening
    assert db.find_attribute("Hair").choices == HAIR
    db.update_attribute(db.find_attribute("Hair").id, choices=[])
    assert db.find_attribute("Hair").choices == []
    db.close()


def test_an_older_database_gets_the_choices_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    TagDB(path).close()
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE a2 AS SELECT id, uid, name, stash_field, write_tags FROM attributes")
    con.execute("DROP TABLE attributes")
    con.execute("ALTER TABLE a2 RENAME TO attributes")
    con.execute("INSERT INTO attributes(id, uid, name, stash_field, write_tags) VALUES(1, 'u', 'Hair', NULL, 1)")
    con.commit()
    con.close()
    db = TagDB(path)
    assert db.find_attribute("Hair").choices == []
    db.close()


def test_plugin_api_choices_values_and_people_buttons(tmp_path):
    db = TagDB(tmp_path / "t.db")
    win, host = Window(db, Settings()), plugins.PluginHost()
    api = plugins.PluginAPI(win, host, plugins.Plugin(tmp_path, "lore"))
    assert api.api_version >= 4
    assert api.add_attribute("Hair", "hair_color", True, choices=HAIR) == "Hair"
    assert api.add_attribute("Hair", choices=HAIR[:2]) == "Hair"                        # exists: choices updated
    assert next(a for a in api.attributes() if a["name"] == "Hair")["choices"] == [list(c) for c in HAIR[:2]]
    api.set_attribute_choices("Hair", HAIR)
    with pytest.raises(ValueError):
        api.set_attribute_choices("Nope", HAIR)
    api.add_person("Ana Lee", values={"Hair": "Black", "Born": "1990-01-01"})
    assert api.people() == ["Ana Lee"]
    info = api.find_person("Ana Lee")
    assert {(v["attribute"], v["value"], v["tag"]) for v in info["values"]} == {
        ("Hair", "Black", "Black Hair"), ("Born", "1990-01-01", "1990-01-01")}
    assert api.set_person_values("Ana Lee", {"Hair": "Red", "Born": "1990-01-01"}) == ["Hair"]   # same: no change
    assert ("Hair", "Red", "Red Hair") in {(v["attribute"], v["value"], v["tag"]) for v in
                                           api.find_person("Ana Lee")["values"]}
    assert api.set_person_values("Ana Lee", {"Born": None}) == ["Born"]                # taken away
    assert api.set_person_values("Ana Lee", {"Hair": "Red"}) == []
    person_id = db.find_person("Ana Lee").id
    assert win.changed == [([person_id], False), ([person_id], False)]
    api.offer_video_update(["Ana Lee", "Nobody"])
    assert win.changed[-1] == ([person_id], True)
    with pytest.raises(ValueError):
        api.set_person_values("Nobody", {"Hair": "Red"})
    got = []
    api.add_person_action("Look up", got.append)
    assert [a.text for a in host.person_actions] == ["Look up"]
    host.person_actions[0].callback("Ana Lee")
    assert got == ["Ana Lee"]
    db.close()


def test_people_dialog_picks_values_from_the_list(tmp_path):
    from PySide6.QtWidgets import QApplication, QComboBox
    from runemark.ui.people_dialog import PeopleDialog
    QApplication.instance() or QApplication([])
    db = TagDB(tmp_path / "t.db")
    hair = db.update_attribute(db.add_attribute("Hair").id, choices=HAIR)
    db.add_attribute("Born", write_tags=False)
    person = db.add_person("Ana Lee")
    db.add_person_value(person.id, hair.id, "Teal")                                    # not in the list
    clicked = []
    act = plugins._Action("Lore", "Look up (Lore)", clicked.append)
    dialog = PeopleDialog(db, actions=[act])
    dialog.list.setCurrentRow(0)
    assert not dialog.attr_combo.isEditable()
    dialog.attr_combo.setCurrentIndex(dialog.attr_combo.findText("Hair"))
    assert dialog.value_choice.isVisibleTo(dialog) and not dialog.value_edit.isVisibleTo(dialog)
    assert [dialog.value_choice.itemText(i) for i in range(dialog.value_choice.count())] == ["Black", "Blonde", "Red"]
    assert dialog.tag_edit.isReadOnly()
    dialog.value_choice.setCurrentIndex(2)
    assert dialog.tag_edit.text() == "Red Hair"
    dialog._add_value()
    assert ("Red", "Red Hair") in {(v.value, v.tag) for v in db.person(person.id).values}
    # the row with the value that isn't in the list says so, and picking from its list replaces it
    row = next(r for r in range(dialog.values.rowCount()) if dialog.values.item(r, 1).text() == "Teal")
    picker = dialog.values.cellWidget(row, 1)
    assert isinstance(picker, QComboBox) and "not in the list" in picker.itemText(0)
    picker.setCurrentIndex(picker.findText("Blonde"))
    assert sorted((v.value, v.tag) for v in db.person(person.id).values) == [("Blonde", "Blonde Hair"),
                                                                            ("Red", "Red Hair")]
    assert person.id in dialog.touched
    # an attribute without choices is typed as before
    dialog.attr_combo.setCurrentIndex(dialog.attr_combo.findText("Born"))
    assert dialog.value_edit.isVisibleTo(dialog) and not dialog.value_choice.isVisibleTo(dialog)
    dialog.action_buttons[0].click()
    assert clicked == ["Ana Lee"]
    dialog.close()
    db.close()


def test_main_window_people_changed_waits_for_people_or_offers_when_asked(tmp_path):
    from runemark.ui.main_window import MainWindow
    db = TagDB(tmp_path / "t.db")
    p = db.add_person("Ana Lee")
    fid = db.conn.execute("INSERT INTO files(key, path) VALUES('k', ?)", (str(tmp_path / "a.mp4"),)).lastrowid
    db.conn.execute("INSERT INTO file_people(file_id, person_id) VALUES(?, ?)", (fid, p.id))
    db.conn.commit()

    class Dialog:
        touched, refreshed = set(), 0
        def refresh_person(self):
            Dialog.refreshed += 1

    class W:
        _people_dialog = None
        offered = []
        profiles = 0
        def _profiles_changed(self):
            W.profiles += 1
        def _update_people_files(self, paths, label, drop):
            W.offered.append((paths, label, drop))
    w = W()
    w.db = db
    w._people_dialog = Dialog()
    MainWindow.people_changed(w, [p.id])                       # People is open: it offers when it closes
    assert Dialog.touched == {p.id} and Dialog.refreshed == 1 and not W.offered
    w._people_dialog = None
    MainWindow.people_changed(w, [p.id])                       # adds up...
    assert not W.offered
    MainWindow.people_changed(w, [], now=True)                 # ...until asked
    assert W.offered == [([str(tmp_path / "a.mp4")], "People: changed Ana Lee", [])]
    db.close()
