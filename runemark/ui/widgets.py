"""Small reusable widgets: flow layout, tag chips, cards, banners, image boxes."""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics, QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLayout, QPushButton, QSizePolicy,
                               QStyle, QToolButton, QVBoxLayout, QWidget)

from . import theme


class FlowLayout(QLayout):
    """Lays widgets out left to right, wrapping onto new lines (Qt's classic example)."""

    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.min_line_height = 0   # keep at least one line's height even when empty
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):  # noqa: N802
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self):  # noqa: N802
        return True

    def heightForWidth(self, width):  # noqa: N802
        return self._layout(QRect(0, 0, width, 0), dry=True)

    def setGeometry(self, rect):  # noqa: N802
        super().setGeometry(rect)
        self._layout(rect, dry=False)

    def sizeHint(self):  # noqa: N802
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802
        size = QSize(0, self.min_line_height)
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _layout(self, rect, dry):
        margins = self.contentsMargins()
        effective = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, line_height = effective.x(), effective.y(), 0
        for item in self._items:
            if item.widget() is not None and not item.widget().isVisible() and not dry:
                pass
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing > effective.right() + 1 and line_height > 0:
                x = effective.x()
                y += line_height + self._spacing
                next_x = x + hint.width() + self._spacing
                line_height = 0
            if not dry:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        line_height = max(line_height, self.min_line_height)
        return y + line_height - rect.y() + margins.bottom()

    def clear(self):
        while self._items:
            item = self._items.pop()
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()


class Chip(QFrame):
    """A tag pill with a remove button. Unknown tags (not in the database) are dashed."""
    removeClicked = Signal(str)
    clicked = Signal(str)

    def __init__(self, text: str, *, unknown: bool = False, genre: bool = False, tooltip: str = "",
                 removable: bool = True, parent=None):
        super().__init__(parent)
        self.text = text
        self.setObjectName("Chip")
        self.setProperty("unknown", "true" if unknown else "false")
        self.setCursor(Qt.CursorShape.PointingHandCursor if unknown else Qt.CursorShape.ArrowCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(11, 3, 5 if removable else 11, 3)
        layout.setSpacing(5)
        label = QLabel(text)
        label.setObjectName("ChipText")
        layout.addWidget(label)
        if genre:
            badge = QLabel("GENRE")
            badge.setObjectName("ChipBadge")
            badge.setToolTip("Also written to the Genre field (Plex, Jellyfin, Explorer's Genre column)")
            layout.addWidget(badge)
        if removable:
            close = QToolButton()
            close.setObjectName("ChipClose")
            close.setIcon(theme.icon("close", theme.current().muted, 14))
            close.setIconSize(QSize(13, 13))
            close.setFixedSize(QSize(19, 19))
            close.setCursor(Qt.CursorShape.PointingHandCursor)
            close.setToolTip(f"Remove “{text}”")
            close.clicked.connect(lambda: self.removeClicked.emit(self.text))
            layout.addWidget(close)
        if tooltip:
            self.setToolTip(tooltip)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.text)
        super().mouseReleaseEvent(event)


class ChipArea(QFrame):
    """Holds tag chips. Always at least one chip tall, with a hint shown in that row while it's
    empty, so the controls below don't jump when the first tag is added."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.flow = FlowLayout(self, spacing=6)
        sample = Chip("Ag", genre=True)       # the tallest kind of chip
        sample.ensurePolished()
        self.flow.min_line_height = sample.sizeHint().height()
        sample.deleteLater()
        self.placeholder = QLabel(self)
        self.placeholder.setObjectName("Faint")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._placeholder_text = ""

    def set_placeholder(self, text: str):
        self._placeholder_text = text
        self.refresh()

    def refresh(self):
        show = bool(self._placeholder_text) and self.flow.count() == 0
        if show:
            metrics = QFontMetrics(self.placeholder.font())
            self.placeholder.setText(metrics.elidedText(self._placeholder_text, Qt.TextElideMode.ElideRight,
                                                        max(10, self.width())))
            self.placeholder.setToolTip(self._placeholder_text)
            self.placeholder.setGeometry(0, 0, self.width(), self.flow.min_line_height)
        self.placeholder.setVisible(show)
        self.updateGeometry()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self.refresh()


class Card(QFrame):
    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(14, 12, 14, 14)
        self.body.setSpacing(8)
        if title:
            label = QLabel(title.upper())
            label.setObjectName("CardTitle")
            self.body.addWidget(label)


class Banner(QFrame):
    """A one-line notice with an icon, text and optional buttons."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Banner")
        self.setProperty("kind", "info")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 8, 8)
        layout.setSpacing(10)
        self.icon = QLabel()
        self.icon.setFixedSize(20, 20)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.TextFormat.RichText)
        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(6)
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.text, 1)
        layout.addLayout(self.buttons)

    def show_message(self, kind: str, html: str, buttons: list[QPushButton] | None = None):
        pal = theme.current()
        colors = {"ok": pal.success, "warn": pal.warning, "error": pal.danger, "info": pal.accent}
        names = {"ok": "check", "warn": "alert", "error": "alert", "info": "info"}
        self.setProperty("kind", kind)
        self.style().unpolish(self)
        self.style().polish(self)
        self.icon.setPixmap(theme.icon(names[kind], colors[kind], 18).pixmap(18, 18))
        self.text.setText(html)
        while self.buttons.count():
            item = self.buttons.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
        for button in buttons or []:
            self.buttons.addWidget(button)
        self.show()


class ImageBox(QLabel):
    """Shows an image scaled to fit, or placeholder text when empty."""

    def __init__(self, placeholder: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("ImageBox")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(120, 68)
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self._pixmap: QPixmap | None = None
        self._placeholder = placeholder
        self.setWordWrap(True)
        self.set_pixmap(None)

    def set_placeholder(self, text: str):
        self._placeholder = text
        if self._pixmap is None:
            self.setText(text)

    def set_pixmap(self, pixmap: QPixmap | None):
        self._pixmap = pixmap if pixmap is not None and not pixmap.isNull() else None
        self.setProperty("filled", "true" if self._pixmap else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        if self._pixmap is None:
            self.setPixmap(QPixmap())
            self.setText(self._placeholder)
        else:
            self.setText("")
            self._rescale()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        wanted = int(self.width() * 9 / 16)
        if abs(self.minimumHeight() - wanted) > 1:
            self.setMinimumHeight(max(68, wanted))
        self._rescale()

    def _rescale(self):
        if self._pixmap is None:
            return
        ratio = self.devicePixelRatioF()
        target = self.contentsRect().size() * ratio
        scaled = self._pixmap.scaled(target, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(ratio)
        super().setPixmap(scaled)

    def sizeHint(self):  # noqa: N802
        return QSize(320, 180)

    def heightForWidth(self, width):  # noqa: N802
        return int(width * 9 / 16)

    def hasHeightForWidth(self):  # noqa: N802
        return True


class ElidedLabel(QLabel):
    def __init__(self, text: str = "", mode=Qt.TextElideMode.ElideMiddle, parent=None):
        super().__init__(parent)
        self._full = text
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text):  # noqa: N802
        self._full = text
        self.setToolTip(text if text else "")
        self._update()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._update()

    def _update(self):
        metrics = QFontMetrics(self.font())
        super().setText(metrics.elidedText(self._full, self._mode, max(10, self.width())))


def icon_button(name: str, tooltip: str, size: int = 20, checkable: bool = False) -> QToolButton:
    button = QToolButton()
    button.setObjectName("Icon")
    button.setIcon(theme.icon(name, size=size))
    button.setIconSize(QSize(size, size))
    button.setToolTip(tooltip)
    button.setCheckable(checkable)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setAutoRaise(True)
    return button


def text_button(text: str, icon_name: str | None = None, primary: bool = False) -> QPushButton:
    button = QPushButton(text)
    if icon_name:
        pal = theme.current()
        button.setIcon(theme.icon(icon_name, pal.accent_text if primary else pal.text, 18))
        button.setIconSize(QSize(18, 18))
    if primary:
        button.setObjectName("Primary")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet(f"color: {theme.current().border};")
    return line


def spinner_style(widget: QWidget) -> None:
    widget.setStyle(widget.style())


__all__ = ["FlowLayout", "Chip", "ChipArea", "Card", "Banner", "ImageBox", "ElidedLabel", "icon_button",
           "text_button", "separator", "QStyle"]
