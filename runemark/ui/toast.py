"""Toasts (0.9.0): short messages that float at the bottom of the window without stopping you, optionally with
a button (Retry, Undo, Details). Problems stay until you close them; everything else fades after a few seconds."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton, QWidget

KINDS = ("info", "warn", "error")


class Toast(QFrame):
    def __init__(self, host: "ToastHost", text: str, kind: str = "info",
                 actions: list[tuple[str, Callable[[], object]]] | None = None, seconds: float | None = None):
        super().__init__(host.parent_widget)
        self.host = host
        self.kind = kind if kind in KINDS else "info"
        self.setObjectName("Toast")
        self.setProperty("kind", self.kind)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 8, 8, 8)
        row.setSpacing(8)
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setObjectName("ToastText")
        self.label.setMaximumWidth(560)
        row.addWidget(self.label, 1)
        self.buttons: list[QPushButton] = []
        for caption, callback in actions or []:
            button = QPushButton(caption)
            button.setObjectName("ToastButton")
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, cb=callback: self._act(cb))
            row.addWidget(button)
            self.buttons.append(button)
        close = QToolButton()
        close.setText("✕")
        close.setObjectName("ToastClose")
        close.setToolTip("Close")
        close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close.setAutoRaise(True)
        close.clicked.connect(self.dismiss)
        row.addWidget(close)
        self.setStyleSheet(_style(self.kind))
        if seconds is None:
            seconds = 0 if self.kind == "error" else (9 if actions else 5)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.dismiss)
        if seconds:
            self.timer.start(int(seconds * 1000))

    @property
    def text(self) -> str:
        return self.label.text()

    def _act(self, callback):
        self.dismiss()
        callback()

    def dismiss(self):
        self.timer.stop()
        self.host.remove(self)

    def enterEvent(self, event):  # noqa: N802
        self.timer.stop()                     # don't fade while being read
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        if self.kind != "error":
            self.timer.start(4000)
        super().leaveEvent(event)


class ToastHost(QObject):
    """Keeps up to three toasts stacked at the bottom centre of `parent_widget`."""

    MAX = 3

    def __init__(self, parent_widget: QWidget, bottom_margin: int = 36):
        super().__init__(parent_widget)
        self.parent_widget = parent_widget
        self.bottom_margin = bottom_margin
        self.toasts: list[Toast] = []
        parent_widget.installEventFilter(self)

    def show(self, text: str, kind: str = "info", actions=None, seconds: float | None = None) -> Toast:
        # the same message again replaces the old one rather than stacking up
        for old in [t for t in self.toasts if t.text == text]:
            self.remove(old)
        toast = Toast(self, text, kind, actions, seconds)
        self.toasts.append(toast)
        while len(self.toasts) > self.MAX:
            self.remove(self.toasts[0])
        toast.show()
        toast.raise_()
        self.place()
        return toast

    def remove(self, toast: Toast):
        if toast in self.toasts:
            self.toasts.remove(toast)
            toast.hide()
            toast.deleteLater()
            self.place()

    def clear(self):
        for toast in list(self.toasts):
            self.remove(toast)

    def place(self):
        area = self.parent_widget.rect()
        y = area.bottom() - self.bottom_margin
        for toast in reversed(self.toasts):
            toast.adjustSize()
            width = min(max(toast.sizeHint().width(), 320), max(320, area.width() - 40))
            toast.resize(width, toast.heightForWidth(width) if toast.hasHeightForWidth() else toast.sizeHint().height())
            y -= toast.height()
            toast.move(area.left() + (area.width() - width) // 2, y)
            toast.raise_()
            y -= 8

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self.parent_widget and event.type() == QEvent.Type.Resize:
            self.place()
        return False


def _style(kind: str) -> str:
    from . import theme
    t = theme.current()
    edge = {"info": t.accent, "warn": t.warning, "error": t.danger}[kind]
    return (f"#Toast {{ background: {t.surface}; border: 1px solid {t.border}; border-left: 4px solid {edge};"
            f" border-radius: 8px; }}"
            f" #ToastText {{ color: {t.text}; }}"
            f" #ToastButton {{ padding: 3px 10px; }}")
