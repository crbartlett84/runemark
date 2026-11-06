"""0.9.2: nudge buttons and keys for precise seeking (−10 −5 −1 −½ | +½ +1 +5 +10), their sizes in Settings,
and frame stepping by the frame rate when the frame times can't be read."""
from __future__ import annotations

from fractions import Fraction
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent

from test_v090 import _close, _window, needs_window, qapp, wait_until  # noqa: F401


class _Engine:
    """Stands in for QMediaPlayer: a position, a length and a play state."""

    def __init__(self, position_ms=3000, duration_ms=6000, playing=False):
        from PySide6.QtMultimedia import QMediaPlayer
        self.S = QMediaPlayer.PlaybackState
        self.pos, self.dur = position_ms, duration_ms
        self.state = self.S.PlayingState if playing else self.S.PausedState
        self.rate = 1.0
        self.sets = []

    def position(self):
        return self.pos

    def duration(self):
        return self.dur

    def playbackState(self):  # noqa: N802
        return self.state

    def pause(self):
        self.state = self.S.PausedState

    def setPosition(self, ms):  # noqa: N802
        self.sets.append(ms)
        self.pos = ms

    def setPlaybackRate(self, rate):  # noqa: N802
        self.rate = rate


def _panel(at=3.0, duration=6.0, fps=Fraction(25), playing=False):
    from runemark.ui.player import PlayerPanel
    panel = PlayerPanel()
    engine = _Engine(int(at * 1000), int(duration * 1000), playing)
    panel._real_player, panel.player = panel.player, engine
    panel.path = "C:/v/a.mp4"
    panel.info = SimpleNamespace(main_video=SimpleNamespace(fps=fps, index=0), duration=duration)
    panel.loaded = True
    panel._frame_us = int(at * 1_000_000)
    panel.slider.setRange(0, int(duration * 1000))
    panel._set_enabled(True)
    return panel, engine


def _button(panel, text):
    return next(b for b in panel.nudge_buttons if b.text() == text)


def test_nudge_helpers():
    from runemark.ui.player import DEFAULT_NUDGE_STEPS, clean_nudge_steps, nudge_level, step_label
    assert DEFAULT_NUDGE_STEPS == (0.5, 1.0, 5.0, 10.0)
    assert clean_nudge_steps(None) == DEFAULT_NUDGE_STEPS
    assert clean_nudge_steps([0.25, "2", -1, float("nan")]) == (0.25, 2.0, 5.0, 10.0)
    assert clean_nudge_steps([1, 2]) == (1.0, 2.0, 5.0, 10.0)
    assert [step_label(s) for s in (0.5, 0.25, 1.0, 2.5, 10.0)] == ["½", "¼", "1", "2.5", "10"]
    M = Qt.KeyboardModifier
    assert nudge_level(M.NoModifier) is None                      # plain arrows step a frame
    assert nudge_level(M.AltModifier) == 0
    assert nudge_level(M.ControlModifier | M.ShiftModifier) == 1
    assert nudge_level(M.ShiftModifier) == 2
    assert nudge_level(M.ControlModifier) == 3
    assert nudge_level(M.ControlModifier | M.AltModifier) is None


def test_buttons_row_labels_tooltips_and_focus(qapp):
    panel, _engine = _panel()
    assert [b.text() for b in panel.nudge_buttons] == ["−10", "−5", "−1", "−½", "+½", "+1", "+5", "+10"]
    assert all(b.focusPolicy() == Qt.FocusPolicy.NoFocus for b in panel.nudge_buttons)
    tips = {b.text(): b.toolTip() for b in panel.nudge_buttons}
    assert "(Alt+←)" in tips["−½"] and "(Ctrl+Shift+→)" in tips["+1"]
    assert "(Shift+←)" in tips["−5"] and "(Ctrl+→)" in tips["+10"]
    panel._set_enabled(False)                                     # no video: the row is off too
    assert not any(b.isEnabled() for b in panel.nudge_buttons)


def test_button_steps_are_exact_and_update_the_time_label(qapp):
    from runemark.media.probe import fmt_duration
    panel, engine = _panel(at=3.0)
    panel._clock.add(0.0, 6.0, [i * 0.4 for i in range(16)], 6.0)   # cuts / frame times nearby don't pull it
    for text, expected in (("+½", 3.5), ("+1", 4.5), ("−5", 0.0), ("+5", 5.0), ("−1", 4.0), ("−½", 3.5)):
        _button(panel, text).click()
        assert engine.sets[-1] == int(expected * 1000) + 1         # aimed just past the exact time
        assert panel.time_label.text() == fmt_duration(expected, millis=True)   # straight away
        assert panel.slider.value() == int(expected * 1000)
        panel._frame_us = int(expected * 1_000_000)                 # the new frame arrives
    panel.close()


def test_nudges_clamp_to_start_and_end(qapp):
    panel, engine = _panel(at=2.0, duration=6.0)
    _button(panel, "−10").click()
    assert panel.current_pts() == 0.0 and engine.sets[-1] == 1
    panel._frame_us = int(5.5 * 1_000_000)
    _button(panel, "+10").click()
    assert engine.sets[-1] <= 6000 and abs(panel.nudge_target(0) - 5.998) < 1e-6
    assert panel.nudge_target(100) == pytest.approx(5.998)
    engine.dur = 0                                              # engine doesn't know yet: the file's length
    assert panel.nudge_target(100) == pytest.approx(5.998)
    panel.close()


def test_nudges_keep_the_speed_and_work_while_playing(qapp):
    panel, engine = _panel(at=3.0, playing=True)
    panel.shuttle.direction, panel.shuttle.index = 1, 2          # L L L: 2×
    engine.pos = 3500                                           # playing: from the engine's position
    _button(panel, "+1").click()
    assert engine.sets[-1] == 4501
    assert panel.shuttle.rate == 2.0 and panel.shuttle.direction == 1
    assert engine.state == engine.S.PlayingState                # it keeps playing
    panel.close()


def test_no_nudging_while_unloaded_or_saving(qapp):
    panel, engine = _panel()
    panel.loaded = False                                        # a video still saving is shown unloaded
    panel.nudge(5)
    panel.nudge_step(1, 3)
    assert engine.sets == []
    panel.close()


def test_frame_step_uses_the_frame_rate_when_frame_times_are_missing(qapp):
    panel, engine = _panel(at=2.0, fps=Fraction(25))
    assert panel.frame_seconds() == pytest.approx(0.04)
    panel._step_by_rate(1)
    assert panel.current_pts() == pytest.approx(2.04)
    panel._step_by_rate(-2)
    assert panel.current_pts() == pytest.approx(1.96)
    panel.info = SimpleNamespace(main_video=SimpleNamespace(fps=None, index=0), duration=6.0)
    assert panel.frame_seconds() == pytest.approx(1 / 30)           # no frame rate: 1/30 s
    panel.info = None
    assert panel.frame_seconds() == pytest.approx(1 / 30)
    panel.close()


def test_frame_step_falls_back_when_the_lookup_finds_nothing(qapp, monkeypatch):
    from runemark.ui import player as P
    panel, engine = _panel(at=2.0, fps=Fraction(25))
    monkeypatch.setattr(P, "packet_times", lambda *a, **k: [])
    panel.step(1)                                               # pauses, then looks the frame times up
    assert wait_until(qapp, lambda: panel._step_job is None and engine.sets, 10)
    assert panel.current_pts() == pytest.approx(2.04)
    assert engine.state == engine.S.PausedState
    panel.close()


def test_settings_change_the_steps_and_the_buttons_relabel(qapp):
    from runemark.settings import Settings
    from runemark.ui.dialogs import SettingsDialog
    settings = Settings()
    assert settings.nudge_steps == [0.5, 1.0, 5.0, 10.0]
    dialog = SettingsDialog(settings, None, db=None)
    assert [s.value() for s in dialog.nudge_spins] == [0.5, 1.0, 5.0, 10.0]
    for spin, value in zip(dialog.nudge_spins, (0.25, 2, 15, 30)):
        spin.setValue(value)
    dialog._save()
    assert settings.nudge_steps == [0.25, 2.0, 15.0, 30.0]
    assert Settings.load().nudge_steps == [0.25, 2.0, 15.0, 30.0]  # kept
    panel, engine = _panel(at=20.0, duration=60.0)
    panel.set_nudge_steps(settings.nudge_steps)
    assert [b.text() for b in panel.nudge_buttons] == ["−30", "−15", "−2", "−¼", "+¼", "+2", "+15", "+30"]
    assert "Forward 15 seconds (Shift+→)" == _button(panel, "+15").toolTip()
    _button(panel, "+15").click()
    assert engine.sets[-1] == 35001
    panel.close()


class _Player:
    def __init__(self, steps=(0.5, 1.0, 5.0, 10.0)):
        self.calls = []
        self.nudge_steps = steps

    def nudge(self, n):
        self.calls.append(("nudge", n))

    def step(self, n):
        self.calls.append(("step", n))

    def toggle_play(self):
        self.calls.append("play")

    def seek_seconds(self, s):
        self.calls.append(("seek", s))


def _win(steps=(0.5, 1.0, 5.0, 10.0)):
    from runemark.ui.main_window import MainWindow

    class Win:                                         # just enough of MainWindow for its key routing
        _player_key = MainWindow._player_key

        def __init__(self):
            self.doc = SimpleNamespace(read_only=False)
            self.player = _Player(steps)

        def undo_chapters(self):
            self.player.calls.append("undo")

        def redo_chapters(self):
            self.player.calls.append("redo")
    return Win()


def _press(win, key, mods=Qt.KeyboardModifier.NoModifier):
    return win._player_key(QKeyEvent(QEvent.Type.KeyPress, key, mods))


def _focus(monkeypatch, widget):
    from runemark.ui import main_window as mw

    class App:
        @staticmethod
        def focusWidget():  # noqa: N802
            return widget
    monkeypatch.setattr(mw, "QApplication", App)


def test_keys_nudge_by_the_steps_and_arrows_still_step_frames(qapp, monkeypatch):
    _focus(monkeypatch, None)
    M, K = Qt.KeyboardModifier, Qt.Key
    win = _win()
    assert _press(win, K.Key_Left, M.AltModifier)
    assert _press(win, K.Key_Right, M.ControlModifier | M.ShiftModifier)
    assert _press(win, K.Key_Left, M.ShiftModifier)
    assert _press(win, K.Key_Right, M.ControlModifier)
    assert _press(win, K.Key_Right, M.KeypadModifier | M.ControlModifier)       # the keypad's arrows too
    assert _press(win, K.Key_Right) and _press(win, K.Key_Left)
    assert _press(win, K.Key_Comma) and _press(win, K.Key_Period)
    assert _press(win, K.Key_Z, M.ControlModifier)                                # chapter undo stays
    assert win.player.calls == [("nudge", -0.5), ("nudge", 1.0), ("nudge", -5.0), ("nudge", 10.0),
                                ("nudge", 10.0), ("step", 1), ("step", -1), ("step", -1), ("step", 1), "undo"]
    custom = _win(steps=(0.25, 2.0, 15.0, 30.0))
    assert _press(custom, K.Key_Left, M.ControlModifier) and _press(custom, K.Key_Right, M.AltModifier)
    assert custom.player.calls == [("nudge", -30.0), ("nudge", 0.25)]


def test_keys_dont_fire_while_typing(qapp, monkeypatch):
    from PySide6.QtWidgets import QLineEdit, QPlainTextEdit, QDoubleSpinBox
    M, K = Qt.KeyboardModifier, Qt.Key
    for box in (QLineEdit(), QPlainTextEdit(), QDoubleSpinBox()):
        _focus(monkeypatch, box)
        win = _win()
        for key, mods in ((K.Key_Left, M.AltModifier), (K.Key_Right, M.ShiftModifier),
                          (K.Key_Left, M.ControlModifier), (K.Key_Right, M.ControlModifier | M.ShiftModifier),
                          (K.Key_Comma, M.NoModifier), (K.Key_Period, M.NoModifier)):
            assert not _press(win, key, mods)
        assert win.player.calls == []


@needs_window
def test_nudge_buttons_on_a_real_video(tmp_path, media, qapp):
    win, db, (a,) = _window(tmp_path, media, ("a.mp4",))
    try:
        win.open_paths([a])
        assert wait_until(qapp, lambda: win.doc is not None and win.player.loaded and win.player.duration_seconds() > 0)
        wait_until(qapp, lambda: False, 0.5)
        win.player.stop_moving()
        win.player.seek_seconds(1.0)
        wait_until(qapp, lambda: False, 0.3)
        assert all(b.isEnabled() for b in win.player.nudge_buttons)
        base = win.player.current_pts()
        _button(win.player, "+1").click()
        assert win.player.current_pts() == pytest.approx(base + 1.0, abs=0.05)
        assert win.player.time_label.text().startswith("0:00:0")
        _button(win.player, "−10").click()
        assert win.player.time_label.text() == "0:00:00.000"
    finally:
        _close(win, db, qapp)
