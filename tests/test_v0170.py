"""0.17.0: chapter ends on the timeline: a yellow tick at each end and a faint yellow band from start to end."""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from runemark.media.chapters import ChapterMark as M
from runemark.ui import theme
from runemark.ui.player import JumpSlider, chapter_spans


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _slider(marks, value=0, width=800, duration_ms=100_000):
    slider = JumpSlider(Qt.Orientation.Horizontal)
    slider.resize(width, 30)
    slider.setRange(0, duration_ms)
    slider.setValue(value)
    slider.set_chapters(marks)
    return slider


def _is_yellow(rgb: int) -> bool:
    c = QColor(rgb)
    warning = QColor(theme.current().warning)
    return abs(c.red() - warning.red()) < 40 and abs(c.green() - warning.green()) < 40 and \
        abs(c.blue() - warning.blue()) < 40


def _column_has(image, x, test, slider):
    mid = slider.height() // 2
    return any(test(image.pixel(x + dx, y)) for dx in (-1, 0, 1) for y in range(mid - 6, mid + 7))


def test_spans_only_for_chapters_with_an_end():
    spans = chapter_spans([M(0.0, "A"), M(10.0, "B", end=14.0), M(20.0, "C", end=20.0), M(30.0, "D", end=25.0)])
    assert [(s.start, s.end, s.name) for s in spans] == [(10.0, 14.0, "B")]


def test_an_end_past_the_video_end_is_drawn_at_the_end():
    spans = chapter_spans([M(90.0, "tail", end=130.0)], duration=100.0)
    assert spans[0].end == 100.0
    assert chapter_spans([M(100.0, "after", end=130.0)], duration=100.0) == []


def test_overlapping_spans_get_lanes_and_touching_ones_share_a_lane():
    spans = chapter_spans([M(0.0, "A", end=10.0), M(5.0, "B", end=20.0), M(10.0, "C", end=15.0),
                           M(8.0, "D", end=9.0), M(20.0, "E", end=30.0)])
    lanes = {s.name: s.lane for s in spans}
    assert lanes == {"A": 0, "B": 1, "D": 2, "C": 0, "E": 0}


def test_end_tick_is_yellow_and_start_tick_stays(qapp):
    slider = _slider([M(20.0, "A", end=50.0)], value=100_000)      # handle at the far end
    image = slider.grab().toImage()
    end_x = slider.x_for_ms(50_000)
    assert _column_has(image, end_x, _is_yellow, slider)
    start_x = slider.x_for_ms(20_000)
    accent = QColor(theme.current().accent).rgb() & 0xFFFFFF
    assert _column_has(image, start_x, lambda p: (p & 0xFFFFFF) == accent, slider)


def test_band_fills_start_to_end_but_not_outside(qapp):
    plain = _slider([M(20.0, "A")]).grab().toImage()
    banded = _slider([M(20.0, "A", end=50.0)]).grab().toImage()
    slider = _slider([M(20.0, "A", end=50.0)])
    y = slider.height() // 2 - 4                                  # just above the line, inside the band
    inside = slider.x_for_ms(35_000)
    outside = slider.x_for_ms(70_000)
    assert banded.pixel(inside, y) != plain.pixel(inside, y)
    assert banded.pixel(outside, y) == plain.pixel(outside, y)


def test_playing_chapter_band_is_stronger(qapp):
    y_of = lambda s: s.height() // 2 - 4                         # noqa: E731
    idle = _slider([M(20.0, "A", end=50.0)], value=80_000)
    playing = _slider([M(20.0, "A", end=50.0)], value=30_000)
    x = idle.x_for_ms(40_000)
    a = QColor(idle.grab().toImage().pixel(x, y_of(idle)))
    b = QColor(playing.grab().toImage().pixel(x, y_of(playing)))
    assert a != b


def test_played_part_and_playhead_show_over_the_band(qapp):
    plain = _slider([], value=60_000).grab().toImage()
    slider = _slider([M(10.0, "A", end=90.0)], value=60_000)
    banded = slider.grab().toImage()
    y = slider.height() // 2
    played = slider.x_for_ms(40_000)
    assert banded.pixel(played, y) == plain.pixel(played, y)    # the blue played line is untouched
    _opt, _groove, handle = slider._geometry()
    c = handle.center()
    assert banded.pixel(c.x(), c.y()) == plain.pixel(c.x(), c.y())


def test_end_on_the_next_start_shows_both_ticks(qapp):
    slider = _slider([M(10.0, "A", end=30.0), M(30.0, "B", end=60.0)], value=100_000)
    ticks = {span.name: tick for span, _xe, tick in slider.end_tick_xs()}
    start_b = slider.x_for_ms(30_000)
    assert ticks["A"] < start_b and start_b - ticks["A"] <= 3       # yellow just left of the blue tick
    image = slider.grab().toImage()
    assert _column_has(image, ticks["A"], _is_yellow, slider)


def test_very_short_chapter_still_shows(qapp):
    slider = _slider([M(40.0, "A", end=40.05)], value=100_000)
    (_span, xe, tick), = slider.end_tick_xs()
    xs = slider.x_for_ms(40_000)
    assert xe - xs >= JumpSlider.MIN_SPAN_PX and tick > xs
    assert _column_has(slider.grab().toImage(), tick, _is_yellow, slider)


def test_ends_follow_a_new_video_length(qapp):
    slider = JumpSlider(Qt.Orientation.Horizontal)
    slider.set_chapters([M(10.0, "A", end=500.0)])               # chapters can arrive before the duration
    slider.setRange(0, 60_000)
    assert slider.chapter_spans[0].end == 60.0


def test_hover_caption_names_the_chapter_and_its_times(qapp, monkeypatch):
    monkeypatch.setenv("RUNEMARK_NO_AUDIO", "1")
    from runemark.ui.player import PlayerPanel
    panel = PlayerPanel()
    panel.slider.setRange(0, 100_000)
    panel.set_chapters([M(10.0, "Opening", end=20.0), M(15.0, "Inside", end=30.0), M(40.0, "No end")])
    assert panel.chapter_caption(12_000) == "Opening  0:00:10–0:00:20"
    assert panel.chapter_caption(16_000).splitlines() == ["Opening  0:00:10–0:00:20", "Inside  0:00:15–0:00:30"]
    assert panel.chapter_caption(45_000) == ""
    panel.preview.want(None, None, 12.0, panel.slider.mapToGlobal(panel.slider.rect().center()), panel.slider,
                       caption=panel.chapter_caption(12_000))
    assert not panel.preview.chapter.isHidden() and "Opening" in panel.preview.chapter.text()
    panel.preview.hide_now()
