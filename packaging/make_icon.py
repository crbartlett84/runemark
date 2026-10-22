"""Render the app icon (the rune in its tile) to packaging/runemark.ico."""
import io
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402
from PySide6.QtCore import QBuffer, QIODevice  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from runemark.ui.theme import rune_pixmap  # noqa: E402


def main() -> None:
    app = QApplication([])  # noqa: F841 - needed for QPixmap
    images = []
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        rune_pixmap(size).save(buffer, "PNG")
        images.append(Image.open(io.BytesIO(bytes(buffer.data()))).convert("RGBA"))
    target = Path(__file__).resolve().parent / "runemark.ico"
    images[-1].save(target, format="ICO", sizes=[im.size for im in images], append_images=images[:-1])
    images[-1].save(target.with_suffix(".png"))
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
