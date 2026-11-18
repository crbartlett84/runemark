"""0.16.0: Start here (S) moves a chapter's start in place; the End column always shows where a chapter stops."""
from runemark.media import chapters as CH


def _marks():
    marks, _ = CH.add_mark([], 5.0)
    marks, b = CH.add_mark(marks, 10.0)
    marks, c = CH.add_mark(marks, 20.0)
    b.name, b.auto, c.name, c.auto = "Blowjob", False, "Missionary", False
    return CH.renumber(marks)


def test_move_start_keeps_name_and_end():
    marks = _marks()
    b = marks[1]
    b.end = 18.0
    assert CH.move_start(marks, b, 11.25) is None
    assert (b.start, b.name, b.auto, b.end) == (11.25, "Blowjob", False, 18.0)
    assert CH.ends_block(CH.renumber(marks)) == {"11.250": 18.0}     # the end follows the new start


def test_move_start_refuses_past_its_end_or_onto_another_chapter():
    marks = _marks()
    b = marks[1]
    b.end = 18.0
    assert CH.move_start(marks, b, 18.0) == "after_end"
    assert CH.move_start(marks, b, 20.0) == "after_end"
    assert CH.move_start(marks, b, 5.0) == "taken"
    assert b.start == 10.0


def test_move_start_past_a_neighbour_resorts_and_renumbers():
    marks = _marks()
    first = marks[0]                                   # "Chapter 1" at 5.0, numbered automatically
    assert CH.move_start(marks, first, 25.0) is None
    marks = CH.renumber(marks)
    assert [m.start for m in marks] == [10.0, 20.0, 25.0]
    assert marks[-1].name == "Chapter 3"


def test_effective_end_is_its_end_then_the_next_chapter_then_the_video():
    marks = _marks()
    a, b, c = marks
    assert CH.effective_end(marks, a, 60.0) == 10.0
    b.end = 14.0
    assert CH.effective_end(marks, b, 60.0) == 14.0
    assert CH.effective_end(marks, c, 60.0) == 60.0
    assert CH.effective_end(marks, c, None) is None


def test_panel_moves_the_selected_chapter_and_seeks_to_ends():
    from PySide6.QtWidgets import QApplication
    from runemark.ui.chapters_panel import COL_END, ChaptersPanel
    QApplication.instance() or QApplication([])
    panel = ChaptersPanel()
    panel.set_enabled(True, "mp4")
    panel.duration = 60.0
    panel.set_marks(_marks())
    seen, changed = [], []
    panel.seekRequested.connect(seen.append)
    panel.changed.connect(lambda: changed.append(1))

    assert panel.move_start_to(12.0) == (None, "none")             # nothing selected
    panel.table.selectRow(1)
    mark, why = panel.move_start_to(12.0)
    assert why is None and mark.name == "Blowjob" and mark.start == 12.0 and changed
    assert panel.table.item(1, 0).text() == "0:00:12.000"

    panel._clicked(1, COL_END)                                     # runs to the next chapter: shown and seekable
    assert seen[-1] == 20.0
    assert panel.table.item(1, COL_END).text() != "—"
    panel._clicked(2, COL_END)                                     # the last one: the end of the video
    assert seen[-1] == 60.0
