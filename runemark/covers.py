"""Small copies of videos' cover images for the Library's grid, cached on disk so they're read from a video once."""
from __future__ import annotations

import hashlib
import io
import logging
import os
from pathlib import Path

from .media.mp4meta import read_mp4_tags
from .media.probe import MP4_EXTENSIONS
from .paths import app_data_dir
from .tagdb import file_key

log = logging.getLogger(__name__)

EDGE = 320


def cache_dir() -> Path:
    path = app_data_dir() / "covers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def thumbnail(path: str | os.PathLike, folder: Path | None = None) -> bytes | None:
    """A small JPEG of the video's cover, from the cache or made now. None when it has no cover."""
    path = Path(path)
    if path.suffix.lower() not in MP4_EXTENSIONS:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    name = hashlib.sha1(f"{file_key(path)}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest() + ".jpg"
    cached = (folder or cache_dir()) / name
    if cached.is_file():
        data = cached.read_bytes()
        return data or None                      # an empty file means "no cover"
    try:
        cover = read_mp4_tags(path).cover
    except Exception as exc:  # noqa: BLE001
        log.info("no cover from %s: %s", path, exc)
        return None
    data = b""
    if cover:
        from PIL import Image
        try:
            image = Image.open(io.BytesIO(cover))
            image.thumbnail((EDGE, EDGE))
            out = io.BytesIO()
            image.convert("RGB").save(out, "JPEG", quality=82)
            data = out.getvalue()
        except Exception as exc:  # noqa: BLE001
            log.info("unreadable cover in %s: %s", path, exc)
    try:
        cached.write_bytes(data)
    except OSError:
        pass
    return data or None
