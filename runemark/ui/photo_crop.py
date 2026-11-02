"""Cropping a person's photo: a square box over a frame or picture, dragged into place and resized."""
from __future__ import annotations

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget

PHOTO_SIZE = 240          # pixels square, as saved
HANDLE = 12               # corner grip, in screen pixels
MIN_SIDE = 24             # smallest crop, in image pixels


def to_jpeg(image: QImage, size: int = PHOTO_SIZE, quality: int = 85) -> bytes:
    scaled = image.scaled(size, size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    scaled.convertToFormat(QImage.Format.Format_RGB888).save(buf, "JPEG", quality)
    buf.close()
    return bytes(data)


class CropView(QWidget):
    """Shows the image with a square crop box. Drag inside the box to move it, drag a corner or use the
    mouse wheel to resize it. The box is kept in image coordinates."""
    changed = Signal()

    def __init__(self, image: QImage, parent=None):
        super().__init__(parent)
        self.image = image
        side = min(image.width(), image.height()) * 0.5
        self.box = QRectF((image.width() - side) / 2, (image.height() - side) / 3, side, side)
        self._drag: tuple[str, QPointF, QRectF] | None = None
        self.setMinimumSize(480, 320)
        self.setMouseTracking(True)

    # ---- geometry
    def _scale(self) -> tuple[float, QPointF]:
        s = min(self.width() / self.image.width(), self.height() / self.image.height())
        off = QPointF((self.width() - self.image.width() * s) / 2, (self.height() - self.image.height() * s) / 2)
        return s, off

    def _to_screen(self, r: QRectF) -> QRectF:
        s, off = self._scale()
        return QRectF(off.x() + r.x() * s, off.y() + r.y() * s, r.width() * s, r.height() * s)

    def _to_image(self, p: QPointF) -> QPointF:
        s, off = self._scale()
        return QPointF((p.x() - off.x()) / s, (p.y() - off.y()) / s)

    def _clamp(self, r: QRectF) -> QRectF:
        side = max(MIN_SIDE, min(r.width(), self.image.width(), self.image.height()))
        x = min(max(0.0, r.x()), self.image.width() - side)
        y = min(max(0.0, r.y()), self.image.height() - side)
        return QRectF(x, y, side, side)

    def cropped(self) -> QImage:
        return self.image.copy(self.box.toAlignedRect())

    # ---- painting
    def paintEvent(self, _event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(self._to_screen(QRectF(0, 0, self.image.width(), self.image.height())), self.image)
        box = self._to_screen(self.box)
        shade = QPainterPath()
        shade.addRect(QRectF(self.rect()))
        hole = QPainterPath()
        hole.addRect(box)
        p.fillPath(shade.subtracted(hole), QColor(0, 0, 0, 140))
        p.setPen(QPen(QColor(255, 255, 255), 2))
        p.drawRect(box)
        p.setPen(QPen(QColor(255, 255, 255, 120), 1, Qt.PenStyle.DashLine))
        p.drawEllipse(box)                         # how it looks as a round face
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255))
        for corner in (box.topLeft(), box.topRight(), box.bottomLeft(), box.bottomRight()):
            p.drawRect(QRectF(corner.x() - HANDLE / 2, corner.y() - HANDLE / 2, HANDLE, HANDLE))
        p.end()

    # ---- mouse
    def _corner_at(self, pos: QPointF) -> str | None:
        box = self._to_screen(self.box)
        for name, corner in (("tl", box.topLeft()), ("tr", box.topRight()), ("bl", box.bottomLeft()),
                             ("br", box.bottomRight())):
            if abs(pos.x() - corner.x()) <= HANDLE and abs(pos.y() - corner.y()) <= HANDLE:
                return name
        return None

    def mousePressEvent(self, event):  # noqa: N802
        pos = event.position()
        corner = self._corner_at(pos)
        if corner:
            self._drag = (corner, self._to_image(pos), QRectF(self.box))
        elif self._to_screen(self.box).contains(pos):
            self._drag = ("move", self._to_image(pos), QRectF(self.box))

    def mouseMoveEvent(self, event):  # noqa: N802
        pos = event.position()
        if self._drag is None:
            corner = self._corner_at(pos)
            self.setCursor(Qt.CursorShape.SizeFDiagCursor if corner in ("tl", "br") else
                           Qt.CursorShape.SizeBDiagCursor if corner else
                           Qt.CursorShape.SizeAllCursor if self._to_screen(self.box).contains(pos) else
                           Qt.CursorShape.ArrowCursor)
            return
        kind, start, box = self._drag
        now = self._to_image(pos)
        dx, dy = now.x() - start.x(), now.y() - start.y()
        if kind == "move":
            self.box = self._clamp(box.translated(dx, dy))
        else:
            # resize from the dragged corner, keeping the opposite corner fixed and the box square
            grow = {"br": max(dx, dy), "tl": max(-dx, -dy), "tr": max(dx, -dy), "bl": max(-dx, dy)}[kind]
            side = max(MIN_SIDE, box.width() + grow)
            x = box.x() if kind in ("br", "tr") else box.right() - side
            y = box.y() if kind in ("br", "bl") else box.bottom() - side
            self.box = self._clamp(QRectF(x, y, side, side))
        self.update()
        self.changed.emit()

    def mouseReleaseEvent(self, _event):  # noqa: N802
        self._drag = None

    def wheelEvent(self, event):  # noqa: N802
        factor = 0.92 if event.angleDelta().y() > 0 else 1.08
        c = self.box.center()
        side = self.box.width() * factor
        self.box = self._clamp(QRectF(c.x() - side / 2, c.y() - side / 2, side, side))
        self.update()
        self.changed.emit()


class PhotoCropDialog(QDialog):
    def __init__(self, image: QImage, name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Photo for {name}")
        self.resize(820, 600)
        layout = QVBoxLayout(self)
        intro = QLabel("Drag the box over the face. Drag a corner, or scroll, to make it bigger or smaller.")
        intro.setObjectName("Muted")
        layout.addWidget(intro)
        self.view = CropView(image)
        layout.addWidget(self.view, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Use this photo")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def jpeg(self) -> bytes:
        return to_jpeg(self.view.cropped())


def crop_photo(parent, image: QImage, name: str) -> bytes | None:
    """Ask for a crop of `image`; the JPEG, or None if cancelled."""
    if image.isNull():
        return None
    dialog = PhotoCropDialog(image, name, parent)
    return dialog.jpeg() if dialog.exec() == QDialog.DialogCode.Accepted else None
