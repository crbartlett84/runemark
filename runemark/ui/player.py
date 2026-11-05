"""Video player with exact frame stepping, and the filmstrip of suggested frames."""
from __future__ import annotations

import bisect
import logging
import math
import os
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QPoint, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QSlider,
                               QStackedLayout, QToolButton, QVBoxLayout, QWidget)

from ..media import frames
from ..media.probe import MediaInfo, fmt_duration, packet_times
from . import theme, workers
from .widgets import icon_button, text_button

log = logging.getLogger(__name__)


def pil_to_qimage(image: Image.Image) -> QImage:
    image = image.convert("RGB")
    data = image.tobytes("raw", "RGB")
    qimage = QImage(data, image.width, image.height, image.width * 3, QImage.Format.Format_RGB888)
    return qimage.copy()


def bytes_to_pixmap(data: bytes | None) -> QPixmap | None:
    if not data:
        return None
    pix = QPixmap()
    return pix if pix.loadFromData(data) else None




SPEEDS = (1.0, 1.5, 2.0, 4.0)


class Shuttle:
    """J/K/L (0.9.0). L plays forward, and each L again goes faster (1×, 1.5×, 2×, 4×); K pauses; J goes backward:
    Qt's player can't play in reverse, so going backward steps back through the video (about five steps a second),
    and each J again steps back further. J while playing forward slows down first, then backs up."""

    def __init__(self):
        self.direction = 0           # -1 backward, 0 paused, 1 forward
        self.index = 0               # into SPEEDS

    @property
    def rate(self) -> float:
        return SPEEDS[self.index]

    def press(self, key: str) -> tuple[int, float]:
        key = key.upper()
        if key == "K":
            self.direction = 0
        elif key == "L":
            if self.direction == 1:
                self.index = min(self.index + 1, len(SPEEDS) - 1)
            else:
                self.direction, self.index = 1, 0
        elif key == "J":
            if self.direction == 1 and self.index > 0:
                self.index -= 1
            elif self.direction == -1:
                self.index = min(self.index + 1, len(SPEEDS) - 1)
            else:
                self.direction, self.index = -1, 0
        return self.direction, self.rate

    def set_rate(self, rate: float):
        self.index = min(range(len(SPEEDS)), key=lambda i: abs(SPEEDS[i] - rate))

    def label(self) -> str:
        """What the speed readout shows: "" at normal speed playing or paused, else "2×", "◀ 1×"."""
        rate = f"{self.rate:g}×"
        if self.direction == -1:
            return f"◀ {rate}"
        return "" if self.rate == 1.0 else rate


def chapter_ticks(marks) -> list[float]:
    """Where chapter ticks go on the timeline: each chapter's start. Never its end (ends stay in the Chapters tab)."""
    return sorted({round(float(getattr(m, "start", m)), 3) for m in marks})


class JumpSlider(QSlider):
    """The timeline: a click anywhere on the line jumps there (0.8.1; a plain slider only moves a page towards the
    click), and the press carries straight on into a drag, so click-and-slide scrubs from that spot.
    0.9.0: chapter starts are drawn as ticks on it (never their ends), suggested cuts as faint dots under it, and
    hovering reports the time under the mouse (for the preview frame)."""

    hovered = Signal(int, object)      # ms under the mouse, global QPoint
    hoverEnded = Signal()

    def __init__(self, *args):
        super().__init__(*args)
        self.chapter_starts: list[float] = []      # seconds
        self.cuts: list[float] = []
        self.setMouseTracking(True)

    def set_chapter_starts(self, starts: list[float]):
        self.chapter_starts = chapter_ticks(starts)
        self.update()

    def set_cuts(self, cuts: list[float]):
        self.cuts = sorted(cuts or [])
        self.update()

    def _geometry(self):
        from PySide6.QtWidgets import QStyle, QStyleOptionSlider
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderGroove,
                                             self)
        handle = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderHandle,
                                             self)
        return opt, groove, handle

    def x_for_ms(self, ms: float) -> int:
        from PySide6.QtWidgets import QStyle
        opt, groove, handle = self._geometry()
        span = max(1, groove.width() - handle.width())
        return groove.x() + handle.width() // 2 + QStyle.sliderPositionFromValue(
            self.minimum(), self.maximum(), int(ms), span, opt.upsideDown)

    def ms_at(self, x: float) -> int:
        from PySide6.QtWidgets import QStyle
        opt, groove, handle = self._geometry()
        span = max(1, groove.width() - handle.width())
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(),
                                              max(0, int(x) - groove.x() - handle.width() // 2), span, opt.upsideDown)

    def paintEvent(self, event):  # noqa: N802
        super().paintEvent(event)
        if self.maximum() <= self.minimum() or not (self.chapter_starts or self.cuts):
            return
        from PySide6.QtGui import QColor, QPainter, QPen
        painter = QPainter(self)
        t = theme.current()
        _opt, groove, _handle = self._geometry()
        mid = groove.center().y()
        if self.cuts:
            painter.setPen(Qt.PenStyle.NoPen)
            faint = QColor(t.faint)
            faint.setAlpha(170)
            painter.setBrush(faint)
            for cut in self.cuts:
                painter.drawEllipse(QPoint(self.x_for_ms(cut * 1000), min(self.height() - 3, mid + 7)), 1, 1)
        if self.chapter_starts:
            pen = QPen(QColor(t.accent))
            pen.setWidth(2)
            painter.setPen(pen)
            for start in self.chapter_starts:
                x = self.x_for_ms(start * 1000)
                painter.drawLine(x, mid - 6, x, mid + 6)
        painter.end()

    def leaveEvent(self, event):  # noqa: N802
        self.hoverEnded.emit()
        super().leaveEvent(event)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.maximum() > self.minimum():
            from PySide6.QtWidgets import QStyle, QStyleOptionSlider
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            handle = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                                 QStyle.SubControl.SC_SliderHandle, self)
            if not handle.contains(event.position().toPoint()):
                groove = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                                     QStyle.SubControl.SC_SliderGroove, self)
                span = max(1, groove.width() - handle.width())
                x = int(event.position().x()) - groove.x() - handle.width() // 2
                value = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), x, span, opt.upsideDown)
                self.setSliderDown(True)          # sliderPressed: playback position stops following
                self.setValue(value)
                self.sliderMoved.emit(value)      # seek now
                self._jumped = True
                return
        self._jumped = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self.maximum() > self.minimum():
            self.hovered.emit(self.ms_at(event.position().x()), event.globalPosition().toPoint())
        if getattr(self, "_jumped", False):
            from PySide6.QtWidgets import QStyle, QStyleOptionSlider
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            groove = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                                 QStyle.SubControl.SC_SliderGroove, self)
            handle = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                                 QStyle.SubControl.SC_SliderHandle, self)
            span = max(1, groove.width() - handle.width())
            x = int(event.position().x()) - groove.x() - handle.width() // 2
            value = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), max(0, x), span, opt.upsideDown)
            if value != self.value():
                self.setValue(value)
                self.sliderMoved.emit(value)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if getattr(self, "_jumped", False):
            self._jumped = False
            self.setSliderDown(False)             # sliderReleased: seek to the final spot, follow playback again
            return
        super().mouseReleaseEvent(event)

class FrameTimes:
    """Cache of frame timestamps around the playhead, for exact frame stepping."""

    def __init__(self):
        self.times: list[float] = []
        self.lo = self.hi = None
        self.end_reached = False

    def clear(self):
        self.times, self.lo, self.hi, self.end_reached = [], None, None, False

    def covers(self, t: float) -> bool:
        if self.lo is None:
            return False
        low_ok = self.lo <= 0.0 or t >= self.lo + 0.3
        high_ok = self.end_reached or t <= self.hi - 0.3
        return low_ok and high_ok and bool(self.times)

    def add(self, start: float, end: float, times: list[float], duration: float | None):
        self.times = sorted(set(self.times) | set(times))
        self.lo = start if self.lo is None else min(self.lo, start)
        self.hi = end if self.hi is None else max(self.hi, end)
        if duration and end >= duration:
            self.end_reached = True

    def neighbour(self, pts: float, direction: int) -> float | None:
        if direction > 0:
            i = bisect.bisect_right(self.times, pts + 1e-6)
            return self.times[i] if i < len(self.times) else None
        i = bisect.bisect_left(self.times, pts - 1e-6)
        return self.times[i - 1] if i > 0 else None


class PlayerPanel(QWidget):
    captureRequested = Signal()
    loadFailed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.path: Path | None = None
        self.info: MediaInfo | None = None
        self._frame_us: int | None = None
        self._pending_seek_ms: int | None = None
        self._dragging = False
        self._clock = FrameTimes()
        self._pending_steps = 0
        self._step_job = None
        self._first_frame_pending = False
        self.loaded = False                       # a file is open in the player (it holds the file open)
        self.shuttle = Shuttle()
        self._reverse = QTimer(self)               # J: stepping backward, since Qt can't play in reverse
        self._reverse.setInterval(200)
        self._reverse.timeout.connect(self._reverse_tick)
        self.preview = HoverPreview(self)

        self.player = QMediaPlayer(self)
        # RUNEMARK_NO_AUDIO is for test machines without sound hardware
        self.audio = None if os.environ.get("RUNEMARK_NO_AUDIO") else QAudioOutput(self)
        if self.audio is not None:
            self.player.setAudioOutput(self.audio)
        self.video = QVideoWidget()
        self.video.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.video.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.player.setVideoOutput(self.video)
        self.video.videoSink().videoFrameChanged.connect(self._on_frame)

        self.frame = QFrame()
        self.frame.setObjectName("VideoFrame")
        # One page at a time: on Windows the video widget is a native window that would cover
        # anything stacked on top of it, so the message page replaces it rather than overlaying it.
        stack = QStackedLayout(self.frame)
        self.empty = QWidget()
        self.empty.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        empty_layout = QVBoxLayout(self.empty)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rune = QLabel()
        rune.setPixmap(theme.rune_pixmap(72))
        rune.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_title = QLabel("Open a video to get started")
        self.empty_title.setObjectName("EmptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_text = QLabel("MP4 and MKV files. Drag one here, or press Ctrl+O.")
        self.empty_text.setObjectName("EmptyState")
        self.empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_text.setWordWrap(True)
        empty_layout.addWidget(rune)
        empty_layout.addSpacing(8)
        empty_layout.addWidget(self.empty_title)
        empty_layout.addWidget(self.empty_text)
        stack.addWidget(self.video)
        stack.addWidget(self.empty)
        stack.setCurrentWidget(self.empty)
        self._stack = stack

        # controls
        self.play_button = icon_button("play", "Play / pause (Space)", 22)
        self.prev_button = icon_button("frame_prev", "Previous frame (, or ←)", 20)
        self.next_button = icon_button("frame_next", "Next frame (. or →)", 20)
        self.play_button.clicked.connect(self.toggle_play)
        self.prev_button.clicked.connect(lambda: self.step(-1))
        self.next_button.clicked.connect(lambda: self.step(1))
        self.time_label = QLabel("0:00:00.000")
        self.time_label.setObjectName("TimeLabel")
        self.duration_label = QLabel("/ 0:00:00")
        self.duration_label.setObjectName("TimeLabel")
        self.slider = JumpSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.setSingleStep(1000)
        self.slider.setPageStep(10000)
        self.slider.sliderPressed.connect(self._drag_start)
        self.slider.sliderReleased.connect(self._drag_end)
        self.slider.sliderMoved.connect(self._drag_move)
        self.slider.actionTriggered.connect(self._slider_action)
        self.mute_button = icon_button("volume", "Mute", 20, checkable=True)
        self.mute_button.toggled.connect(self._mute_toggled)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(84)
        self.volume.valueChanged.connect(self._volume_changed)
        self.capture_button = text_button("Use this frame", "camera")
        self.capture_button.setToolTip("Make the frame on screen this file's thumbnail (Ctrl+T)")
        self.capture_button.clicked.connect(self.captureRequested)
        self.speed_button = QToolButton()
        self.speed_button.setObjectName("SpeedButton")
        self.speed_button.setText("1×")
        self.speed_button.setToolTip("Playback speed. J backward · K pause · L forward; press J or L again to go "
                                     "faster")
        self.speed_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        from PySide6.QtWidgets import QMenu
        speed_menu = QMenu(self.speed_button)
        for rate in SPEEDS:
            speed_menu.addAction(f"{rate:g}×", lambda r=rate: self.set_speed(r))
        self.speed_button.setMenu(speed_menu)
        self.slider.hovered.connect(self._hovered)
        self.slider.hoverEnded.connect(self.close_preview)

        for widget in (self.play_button, self.prev_button, self.next_button, self.mute_button, self.volume,
                       self.capture_button, self.slider, self.speed_button):
            widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # Space and arrows stay with the video
        controls = QHBoxLayout()
        controls.setSpacing(4)
        controls.addWidget(self.prev_button)
        controls.addWidget(self.play_button)
        controls.addWidget(self.next_button)
        controls.addSpacing(6)
        controls.addWidget(self.time_label)
        controls.addWidget(self.duration_label)
        controls.addSpacing(8)
        controls.addWidget(self.slider, 1)
        controls.addSpacing(4)
        controls.addWidget(self.speed_button)
        controls.addSpacing(4)
        controls.addWidget(self.mute_button)
        controls.addWidget(self.volume)
        controls.addSpacing(10)
        controls.addWidget(self.capture_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self.frame, 1)
        layout.addLayout(controls)

        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.errorOccurred.connect(self._on_error)
        self._set_enabled(False)

    # ---------------------------------------------------------------- loading
    def set_volume(self, value: int, muted: bool):
        self.volume.setValue(value)
        self._volume_changed(value)
        self.mute_button.setChecked(muted)

    def _volume_changed(self, value: int):
        if self.audio is not None:
            self.audio.setVolume(value / 100)

    def load(self, path: Path, info: MediaInfo, position_ms: int = 0):
        self.path, self.info = path, info
        self._frame_us = None
        self._clock.clear()
        self._pending_seek_ms = position_ms or None
        self._first_frame_pending = True
        self._stop_shuttle()
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.setPlaybackRate(self.shuttle.rate)
        self.loaded = True
        self.preview.forget()
        self._stack.setCurrentWidget(self.video)
        self._set_enabled(True)

    def unload(self) -> int:
        """Release the file (needed before writing to it on Windows). Returns the position in ms."""
        position = self.player.position()
        self._stop_shuttle()
        self.close_preview()
        self.player.stop()
        self.player.setSource(QUrl())
        self.loaded = False
        return position

    def clear(self):
        self.unload()
        self.path = self.info = None
        self._frame_us = None
        self._clock.clear()
        self.slider.setRange(0, 0)
        self.time_label.setText("0:00:00.000")
        self.duration_label.setText("/ 0:00:00")
        self._stack.setCurrentWidget(self.empty)
        self._set_enabled(False)

    def show_message(self, title: str, text: str):
        self.empty_title.setText(title)
        self.empty_text.setText(text)
        self._stack.setCurrentWidget(self.empty)

    def reset_message(self):
        self.empty_title.setText("Open a video to get started")
        self.empty_text.setText("MP4 and MKV files. Drag one here, or press Ctrl+O.")

    def _set_enabled(self, enabled: bool):
        for widget in (self.play_button, self.prev_button, self.next_button, self.slider, self.capture_button,
                       self.speed_button):
            widget.setEnabled(enabled)

    # ---------------------------------------------------------------- timeline marks (0.9.0)
    def set_chapter_starts(self, starts: list[float]):
        """Ticks on the timeline where chapters start. Their ends are never drawn there."""
        self.slider.set_chapter_starts(starts)

    def set_cuts(self, cuts: list[float]):
        self.slider.set_cuts(cuts)

    # ---------------------------------------------------------------- speed and J/K/L (0.9.0)
    def set_speed(self, rate: float):
        self._stop_shuttle()
        self.shuttle.set_rate(rate)
        if self.shuttle.direction == -1:
            self.shuttle.direction = 0
        self.player.setPlaybackRate(self.shuttle.rate)
        self._show_speed()

    def shuttle_key(self, key: str):
        if self.path is None:
            return
        direction, rate = self.shuttle.press(key)
        if direction == 0:
            self._stop_shuttle()
            self.pause()
        elif direction == 1:
            self._reverse.stop()
            self.player.setPlaybackRate(rate)
            if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                self.player.play()
        else:
            self.pause()
            if not self._reverse.isActive():
                self._reverse_tick()
                self._reverse.start()
        self._show_speed()

    def _reverse_tick(self):
        if self.path is None or not self.loaded:
            self._stop_shuttle()
            return
        base = self.current_pts()
        if base is None:
            base = self.player.position() / 1000
        if base <= 0.001:
            self._stop_shuttle()
            return
        # about the speed of playing backward at the chosen rate, in steps of 0.2 s
        self.seek_seconds(max(0.0, base - self.shuttle.rate * self._reverse.interval() / 1000))

    def _stop_shuttle(self):
        self._reverse.stop()
        if getattr(self, "shuttle", None) is not None and self.shuttle.direction == -1:
            self.shuttle.direction = 0
        if getattr(self, "speed_button", None) is not None:
            self._show_speed()

    def _show_speed(self):
        label = self.shuttle.label()
        self.speed_button.setText(label or "1×")
        self.speed_button.setProperty("active", bool(label))
        self.speed_button.style().unpolish(self.speed_button)
        self.speed_button.style().polish(self.speed_button)

    # ---------------------------------------------------------------- hover preview (0.9.0)
    def _hovered(self, ms: int, global_pos):
        if not self.loaded or self.info is None or self.info.main_video is None or self._dragging:
            return
        self.preview.want(self.path, self.info, ms / 1000, global_pos, self.slider)

    def close_preview(self):
        self.preview.hide_now()

    # ---------------------------------------------------------------- frames
    def _on_frame(self, frame: QVideoFrame):
        if frame.isValid() and frame.startTime() >= 0:
            self._frame_us = frame.startTime()
            self._update_time_label()

    def current_pts(self) -> float | None:
        """Presentation time (seconds) of the frame on screen."""
        if self._frame_us is None:
            return None
        return self._frame_us / 1_000_000

    def _update_time_label(self):
        pts = self.current_pts()
        if pts is None:
            pts = self.player.position() / 1000
        self.time_label.setText(fmt_duration(pts, millis=True))

    # ---------------------------------------------------------------- transport
    def toggle_play(self):
        if self.path is None:
            return
        if self._reverse.isActive():
            self._stop_shuttle()
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.shuttle.direction = 1
            self.player.setPlaybackRate(self.shuttle.rate)
            self.player.play()

    def pause(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()

    def stop_moving(self):
        """Pause, and stop stepping backward (before marking a chapter or capturing a frame)."""
        self._stop_shuttle()
        self.pause()

    def seek_seconds(self, seconds: float):
        """Go to the frame on screen at `seconds`, including a frame that starts exactly then."""
        if self.path is None:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState:
            self.player.pause()
        # Qt shows the last frame that starts *before* the position, so aim just past it
        self.player.setPosition(max(0, int(math.ceil(seconds * 1000)) + 1))
        # Until the new frame arrives (it replaces this with its own time), the frame on screen is the one
        # asked for: M, E or a capture pressed straight after a jump must not use the frame from before it.
        target = max(0.0, seconds)
        if self._clock.covers(target):           # the frame's own start time, when the frame times are known
            i = bisect.bisect_right(self._clock.times, target + 1e-6)
            if i > 0:
                target = self._clock.times[i - 1]
        self._frame_us = int(round(target * 1_000_000))

    def nudge(self, seconds: float):
        """Jump by `seconds` from the frame on screen (Shift+arrows)."""
        base = self.current_pts()
        if base is None or self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            base = self.player.position() / 1000
        target = max(0.0, base + seconds)
        end = self.player.duration() / 1000
        if end > 0:
            target = min(target, max(0.0, end - 0.002))   # stop on the last frame, not past it
        self.seek_seconds(target)

    def step(self, direction: int):
        """Move exactly one frame forward or back, using the file's real frame timestamps."""
        if self.path is None or self.info is None or self.info.main_video is None:
            return
        self._stop_shuttle()
        self.pause()
        self._pending_steps += 1 if direction > 0 else -1
        self._run_steps()

    def _run_steps(self):
        if not self._pending_steps or self.info is None:
            return
        pts = self.current_pts()
        if pts is None:
            pts = self.player.position() / 1000
        if self._clock.covers(pts):
            direction = 1 if self._pending_steps > 0 else -1
            target = pts
            while self._pending_steps:
                nxt = self._clock.neighbour(target, direction)
                if nxt is None:
                    self._pending_steps = 0
                    break
                target = nxt
                self._pending_steps -= direction
                if not self._clock.covers(target):
                    break
            if target != pts:
                # Qt shows the last frame that starts *before* the position, so land just past it
                self._frame_us = int(round(target * 1_000_000))
                self.player.setPosition(int(math.ceil(target * 1000)) + 1)
                self._update_time_label()
            if self._pending_steps:
                QTimer.singleShot(0, self._run_steps)
            return
        if self._step_job is not None:
            return
        path, stream, duration = self.path, self.info.main_video, self.info.duration
        start, end = max(0.0, pts - 8.0), pts + 8.0

        def fetch(job):
            return start, end, packet_times(path, stream.index, start, end)

        def done(result):
            self._step_job = None
            if self.path != path:
                return
            lo, hi, times = result
            self._clock.add(lo, hi, times, duration)
            if not times:
                self._pending_steps = 0
                return
            self._run_steps()

        def failed(exc, _tb):
            self._step_job = None
            self._pending_steps = 0
            log.info("frame step lookup failed: %s", exc)

        self._step_job = workers.start(fetch, on_done=done, on_error=failed)

    # ---------------------------------------------------------------- slider
    def _drag_start(self):
        self._dragging = True

    def _drag_end(self):
        self._dragging = False
        self.player.setPosition(self.slider.value())

    def _drag_move(self, value: int):
        self.player.setPosition(value)
        self.time_label.setText(fmt_duration(value / 1000, millis=True))

    def _slider_action(self, action):
        if action in (QSlider.SliderAction.SliderPageStepAdd, QSlider.SliderAction.SliderPageStepSub,
                      QSlider.SliderAction.SliderSingleStepAdd, QSlider.SliderAction.SliderSingleStepSub):
            QTimer.singleShot(0, lambda: self.player.setPosition(self.slider.value()))

    def _mute_toggled(self, muted: bool):
        if self.audio is not None:
            self.audio.setMuted(muted)
        self.mute_button.setIcon(theme.icon("mute" if muted else "volume", size=20))

    # ---------------------------------------------------------------- player events
    def _on_position(self, ms: int):
        if not self._dragging:
            self.slider.blockSignals(True)
            self.slider.setValue(ms)
            self.slider.blockSignals(False)
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._update_time_label()

    def _on_duration(self, ms: int):
        self.slider.setRange(0, max(0, ms))
        # The file's own length (as the Library and the details show it), not the playback engine's estimate
        known = self.info.duration if getattr(self, "info", None) is not None else None
        self.duration_label.setText("/ " + fmt_duration(known if known else ms / 1000))

    def _on_status(self, status):
        if status == QMediaPlayer.MediaStatus.LoadedMedia and self._first_frame_pending:
            # Show a frame straight away instead of a black box. Done once per load, and
            # outside this signal, because pause() can re-emit the status change.
            self._first_frame_pending = False
            QTimer.singleShot(0, self._show_first_frame)
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self.loadFailed.emit(self.player.errorString() or "This file can't be played.")

    def _show_first_frame(self):
        if self.path is None:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState:
            self.player.pause()
        if self._pending_seek_ms:
            self.player.setPosition(self._pending_seek_ms)
            self._pending_seek_ms = None

    def _on_state(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_button.setIcon(theme.icon("pause" if playing else "play", size=22))
        if not playing and self.shuttle.direction == 1:
            self.shuttle.direction = 0

    def _on_error(self, error, message):
        if error != QMediaPlayer.Error.NoError:
            log.warning("player error %s: %s", error, message)
            self.loadFailed.emit(message or "This file can't be played.")


class HoverPreview(QFrame):
    """The small picture over the timeline while the mouse hovers on it (0.9.0). Light on purpose: one quick
    key-frame grab at a time (the latest place asked for wins), pictures kept for this video, nothing while the
    mouse moves fast (it waits for a short pause)."""

    WIDTH = 192
    BUCKET = 2.0               # seconds: hovering within the same couple of seconds reuses the picture

    def __init__(self, owner):
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("HoverPreview")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)
        self.picture = QLabel()
        self.picture.setFixedSize(self.WIDTH, int(self.WIDTH * 9 / 16))
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time = QLabel("")
        self.time.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.picture)
        layout.addWidget(self.time)
        t = theme.current()
        self.setStyleSheet(f"#HoverPreview {{ background: {t.surface}; border: 1px solid {t.border}; }}"
                           f" QLabel {{ color: {t.text}; }}")
        self.cache: dict[float, QPixmap] = {}
        self._path = None
        self._info = None
        self._wanted: float | None = None
        self._job = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._fetch)
        self.grab = frames.grab_preview          # replaceable in tests

    def key(self, seconds: float) -> float:
        return round(seconds / self.BUCKET) * self.BUCKET

    def forget(self):
        self.cache.clear()
        self._wanted = None
        self._path = None

    def want(self, path, info, seconds: float, global_pos, slider):
        if self._path != path:
            self.forget()
            self._path, self._info = path, info
        self.time.setText(fmt_duration(seconds))
        key = self.key(seconds)
        self._wanted = key
        pix = self.cache.get(key)
        if pix is not None:
            self.picture.setPixmap(pix)
        elif not self.isVisible():
            self.picture.clear()
        self.adjustSize()
        top = slider.mapToGlobal(QPoint(0, 0)).y()
        self.move(global_pos.x() - self.width() // 2, top - self.height() - 6)
        self.show()
        if pix is None:
            self._timer.start()

    def _fetch(self):
        if self._job is not None or self._wanted is None or self._path is None:
            return
        key, path, info = self._wanted, self._path, self._info
        if key in self.cache:
            return
        grab = self.grab

        def work(job):
            return key, pil_to_qimage(grab(path, info, key, max_edge=self.WIDTH))

        def done(result):
            self._job = None
            got, image = result
            if path != self._path:
                return
            pix = QPixmap.fromImage(image)
            if len(self.cache) > 150:
                self.cache.pop(next(iter(self.cache)))
            self.cache[got] = pix
            if got == self._wanted and self.isVisible():
                self.picture.setPixmap(pix)
            if self._wanted is not None and self._wanted not in self.cache:
                self._timer.start()              # the mouse moved on meanwhile

        def failed(_exc, _tb):
            self._job = None

        self._job = workers.start(work, on_done=done, on_error=failed)

    def hide_now(self):
        self._timer.stop()
        self._wanted = None
        self.hide()


class Filmstrip(QScrollArea):
    """A row of evenly spaced frames; click one to jump there."""
    seekRequested = Signal(float)

    THUMB_W = 150

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFixedHeight(130)
        self._inner = QWidget()
        self._row = QHBoxLayout(self._inner)
        self._row.setContentsMargins(0, 2, 0, 2)
        self._row.setSpacing(8)
        self._row.addStretch(1)
        self.setWidget(self._inner)
        self._job = None
        self._token = 0
        self._buttons: list[QToolButton] = []

    def clear(self):
        self._token += 1
        if self._job is not None:
            self._job.cancel.set()
            self._job = None
        for button in self._buttons:
            button.setParent(None)
            button.deleteLater()
        self._buttons = []

    def load(self, path: Path, info: MediaInfo, count: int):
        self.clear()
        times = frames.filmstrip_frames(info, count)   # presentation times of real frames
        if not times or info.main_video is None:
            return
        token = self._token
        for t in times:
            button = QToolButton()
            button.setObjectName("FrameThumb")
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setText(_strip_label(t, info.duration or 0))
            button.setIconSize(QSize(self.THUMB_W, int(self.THUMB_W * 9 / 16)))
            button.setFixedSize(QSize(self.THUMB_W + 8, int(self.THUMB_W * 9 / 16) + 30))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setToolTip(f"Jump to {fmt_duration(t, millis=True)}")
            button.clicked.connect(lambda _=False, s=t: self.seekRequested.emit(s))
            self._row.insertWidget(self._row.count() - 1, button)
            self._buttons.append(button)

        def work(job):
            for index, t in enumerate(times):
                if job.cancel.is_set():
                    return
                try:
                    image = frames.grab_at_pts(path, info, t, max_edge=self.THUMB_W * 2, tonemap=True)
                except Exception as exc:  # noqa: BLE001
                    log.info("filmstrip frame %s failed: %s", t, exc)
                    continue
                job.emit((token, index, pil_to_qimage(image)))

        def show(item):
            tok, index, qimage = item
            if tok != self._token or index >= len(self._buttons):
                return
            pix = QPixmap.fromImage(qimage)
            target = QSize(self.THUMB_W, int(self.THUMB_W * 9 / 16))
            self._buttons[index].setIcon(pix.scaled(target * 2, Qt.AspectRatioMode.KeepAspectRatio,
                                                    Qt.TransformationMode.SmoothTransformation))

        self._job = workers.start(work, on_item=show)


def _strip_label(seconds: float, duration: float) -> str:
    """Short time labels: tenths of a second for short clips, h:mm:ss for long videos."""
    if duration < 60:
        return f"{seconds:.1f}s"
    if duration < 3600:
        return f"{int(seconds // 60)}:{int(seconds % 60):02d}"
    return fmt_duration(seconds)
