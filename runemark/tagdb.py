"""The tag database: tags organised in nested categories, plus the files Runemark has tagged.

Categories only organise tags inside the app; they are never written to video files.
A category can be flagged so its tags (and its subcategories' tags) also go to the Genre field.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 3
HISTORY_KEEP = 10      # earlier states remembered per file, for undo
MAX_NAME = 100

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS categories(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    write_genre INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tags(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    size INTEGER,
    mtime REAL,
    saved_at TEXT
);
CREATE TABLE IF NOT EXISTS file_tags(
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    tag_name TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY(file_id, tag_name)
);
CREATE INDEX IF NOT EXISTS idx_file_tags_name ON file_tags(tag_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_categories_parent ON categories(parent_id);
CREATE INDEX IF NOT EXISTS idx_tags_category ON tags(category_id);
-- Schema 3: a file's tags, genre and details as they were BEFORE each save, for undo (last HISTORY_KEEP per file).
-- batch groups the files of one bulk change. No cover or chapters: those live only in the video.
CREATE TABLE IF NOT EXISTS file_history(
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL,
    path TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    batch TEXT,
    label TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL,
    genre TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    year INTEGER,
    description TEXT NOT NULL DEFAULT '',
    rating INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_history_key ON file_history(key, id);
CREATE INDEX IF NOT EXISTS idx_history_batch ON file_history(batch);
"""

# Columns added in schema 2 for the library (what Runemark last read from each file)
LIBRARY_COLUMNS = {
    "container": "TEXT", "title": "TEXT", "year": "INTEGER", "rating": "INTEGER", "has_cover": "INTEGER",
    "duration": "REAL", "genre": "TEXT", "indexed_size": "INTEGER", "indexed_mtime": "REAL",
    "missing": "INTEGER NOT NULL DEFAULT 0",
}


@dataclass
class FileRecord:
    """A file in the library, as Runemark last read it."""
    path: str
    tags: list
    container: str
    title: str
    year: int | None
    rating: int
    has_cover: bool
    duration: float | None
    genre: list
    missing: bool
    saved_at: str | None


def _read_only_uri(path: str | os.PathLike) -> str:
    """SQLite URI that opens `path` read-only. Characters like # ? % and spaces are escaped (unescaped,
    SQLite reads "#1" as a fragment and opens the wrong file). UNC paths keep an empty authority."""
    from urllib.parse import quote
    p = Path(path).resolve().as_posix()          # /home/x, C:/x or //server/share/x
    if p.startswith("//"):
        p = "//" + p                             # file:////server/share/x
    elif not p.startswith("/"):
        p = "/" + p                              # file:///C:/x
    return "file:" + quote(p, safe="/:") + "?mode=ro"


class TagDBError(Exception):
    pass


class InvalidName(TagDBError):
    pass


class DuplicateName(TagDBError):
    def __init__(self, message: str, existing_id: int | None = None):
        super().__init__(message)
        self.existing_id = existing_id


@dataclass
class HistoryEntry:
    """What a file looked like before one save."""
    id: int
    path: str
    saved_at: str
    batch: str | None
    label: str
    keywords: list
    genre: list
    title: str
    year: int | None
    description: str
    rating: int

    def summary(self) -> str:
        bits = ["; ".join(self.keywords + [g for g in self.genre if g.casefold() not in
                                            {k.casefold() for k in self.keywords}]) or "no tags"]
        if self.title:
            bits.append(f"title “{self.title}”")
        if self.year:
            bits.append(str(self.year))
        if self.rating:
            bits.append("★" * self.rating)
        return " · ".join(bits)


@dataclass(frozen=True)
class Category:
    id: int
    name: str
    parent_id: int | None
    write_genre: bool


@dataclass(frozen=True)
class Tag:
    id: int
    name: str
    category_id: int | None


def clean_name(name: str, what: str = "Tag") -> str:
    text = " ".join(str(name).split())
    if not text:
        raise InvalidName(f"{what} names can't be empty.")
    if ";" in text:
        raise InvalidName(f"{what} names can't contain ';' (it separates tags inside video files).")
    if re.search(r"[\x00-\x1f\x7f]", text):
        raise InvalidName(f"{what} names can't contain control characters.")
    if len(text) > MAX_NAME:
        raise InvalidName(f"{what} names can be at most {MAX_NAME} characters.")
    return text


def file_key(path: str | os.PathLike) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


class TagDB:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()
        self._genre_cache: dict[int, bool] | None = None

    def _migrate(self) -> None:
        self.conn.executescript(SCHEMA)
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(files)").fetchall()}
        for name, kind in LIBRARY_COLUMNS.items():
            if name not in have:
                self.conn.execute(f"ALTER TABLE files ADD COLUMN {name} {kind}")
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                          (str(SCHEMA_VERSION),))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def _changed(self) -> None:
        self.conn.commit()
        self._genre_cache = None

    # ------------------------------------------------------------------ categories
    def categories(self) -> list[Category]:
        rows = self.conn.execute("SELECT * FROM categories ORDER BY name COLLATE NOCASE").fetchall()
        return [Category(r["id"], r["name"], r["parent_id"], bool(r["write_genre"])) for r in rows]

    def category(self, category_id: int) -> Category:
        row = self.conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
        if row is None:
            raise TagDBError("That category no longer exists.")
        return Category(row["id"], row["name"], row["parent_id"], bool(row["write_genre"]))

    def children(self, parent_id: int | None) -> list[Category]:
        if parent_id is None:
            rows = self.conn.execute(
                "SELECT * FROM categories WHERE parent_id IS NULL ORDER BY name COLLATE NOCASE").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM categories WHERE parent_id = ? ORDER BY name COLLATE NOCASE", (parent_id,)).fetchall()
        return [Category(r["id"], r["name"], r["parent_id"], bool(r["write_genre"])) for r in rows]

    def _sibling_clash(self, name: str, parent_id: int | None, exclude: int | None = None) -> Category | None:
        for sibling in self.children(parent_id):
            if sibling.name.casefold() == name.casefold() and sibling.id != exclude:
                return sibling
        return None

    def add_category(self, name: str, parent_id: int | None = None, write_genre: bool = False) -> Category:
        name = clean_name(name, "Category")
        if parent_id is not None:
            self.category(parent_id)
        clash = self._sibling_clash(name, parent_id)
        if clash:
            raise DuplicateName(f"There's already a category called “{clash.name}” here.", clash.id)
        cur = self.conn.execute("INSERT INTO categories(name, parent_id, write_genre) VALUES(?, ?, ?)",
                                (name, parent_id, int(write_genre)))
        self._changed()
        return self.category(cur.lastrowid)

    def rename_category(self, category_id: int, name: str) -> Category:
        name = clean_name(name, "Category")
        cat = self.category(category_id)
        clash = self._sibling_clash(name, cat.parent_id, exclude=category_id)
        if clash:
            raise DuplicateName(f"There's already a category called “{clash.name}” here.", clash.id)
        self.conn.execute("UPDATE categories SET name = ? WHERE id = ?", (name, category_id))
        self._changed()
        return self.category(category_id)

    def descendants(self, category_id: int) -> set[int]:
        found: set[int] = set()
        stack = [category_id]
        while stack:
            current = stack.pop()
            for child in self.children(current):
                if child.id not in found:
                    found.add(child.id)
                    stack.append(child.id)
        return found

    def move_category(self, category_id: int, new_parent_id: int | None) -> Category:
        cat = self.category(category_id)
        if new_parent_id is not None:
            if new_parent_id == category_id or new_parent_id in self.descendants(category_id):
                raise TagDBError("A category can't be moved inside itself.")
            self.category(new_parent_id)
        clash = self._sibling_clash(cat.name, new_parent_id, exclude=category_id)
        if clash:
            raise DuplicateName(f"There's already a category called “{clash.name}” there.", clash.id)
        self.conn.execute("UPDATE categories SET parent_id = ? WHERE id = ?", (new_parent_id, category_id))
        self._changed()
        return self.category(category_id)

    def set_write_genre(self, category_id: int, value: bool) -> None:
        self.category(category_id)
        self.conn.execute("UPDATE categories SET write_genre = ? WHERE id = ?", (int(value), category_id))
        self._changed()

    def delete_category(self, category_id: int) -> None:
        """Delete a category. Its tags and subcategories move up to its parent (or the top)."""
        cat = self.category(category_id)
        for child in self.children(category_id):
            name = child.name
            if self._sibling_clash(name, cat.parent_id):
                name = self._unique_sibling_name(name, cat.parent_id)
                self.conn.execute("UPDATE categories SET name = ? WHERE id = ?", (name, child.id))
            self.conn.execute("UPDATE categories SET parent_id = ? WHERE id = ?", (cat.parent_id, child.id))
        self.conn.execute("UPDATE tags SET category_id = ? WHERE category_id = ?", (cat.parent_id, category_id))
        self.conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        self._changed()

    def _unique_sibling_name(self, name: str, parent_id: int | None) -> str:
        n = 2
        while self._sibling_clash(f"{name} ({n})", parent_id):
            n += 1
        return f"{name} ({n})"

    def category_path(self, category_id: int | None) -> list[str]:
        names: list[str] = []
        seen: set[int] = set()
        while category_id is not None and category_id not in seen:
            seen.add(category_id)
            cat = self.category(category_id)
            names.append(cat.name)
            category_id = cat.parent_id
        return list(reversed(names))

    def genre_categories(self) -> set[int]:
        """Categories whose tags go to Genre (flagged themselves or under a flagged parent)."""
        if self._genre_cache is None:
            cats = {c.id: c for c in self.categories()}
            cache: dict[int, bool] = {}

            def flagged(cid: int, depth: int = 0) -> bool:
                if cid in cache:
                    return cache[cid]
                cat = cats.get(cid)
                result = bool(cat and (cat.write_genre or (
                    cat.parent_id is not None and depth < 64 and flagged(cat.parent_id, depth + 1))))
                cache[cid] = result
                return result

            for cid in cats:
                flagged(cid)
            self._genre_cache = cache
        return {cid for cid, flag in self._genre_cache.items() if flag}

    # ------------------------------------------------------------------ tags
    def _tag(self, row) -> Tag:
        return Tag(row["id"], row["name"], row["category_id"])

    def tags(self) -> list[Tag]:
        rows = self.conn.execute("SELECT * FROM tags ORDER BY name COLLATE NOCASE").fetchall()
        return [self._tag(r) for r in rows]

    def tags_in(self, category_id: int | None) -> list[Tag]:
        if category_id is None:
            rows = self.conn.execute(
                "SELECT * FROM tags WHERE category_id IS NULL ORDER BY name COLLATE NOCASE").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM tags WHERE category_id = ? ORDER BY name COLLATE NOCASE", (category_id,)).fetchall()
        return [self._tag(r) for r in rows]

    def tag(self, tag_id: int) -> Tag:
        row = self.conn.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)).fetchone()
        if row is None:
            raise TagDBError("That tag no longer exists.")
        return self._tag(row)

    def find_tag(self, name: str) -> Tag | None:
        row = self.conn.execute("SELECT * FROM tags WHERE name = ? COLLATE NOCASE", (" ".join(name.split()),)).fetchone()
        return self._tag(row) if row else None

    def canonical(self, name: str) -> str:
        found = self.find_tag(name)
        return found.name if found else " ".join(name.split())

    def add_tag(self, name: str, category_id: int | None = None) -> Tag:
        name = clean_name(name)
        if category_id is not None:
            self.category(category_id)
        existing = self.find_tag(name)
        if existing:
            where = " / ".join(self.category_path(existing.category_id)) or "Uncategorized"
            raise DuplicateName(f"“{existing.name}” already exists (in {where}).", existing.id)
        cur = self.conn.execute("INSERT INTO tags(name, category_id, created_at) VALUES(?, ?, ?)",
                                (name, category_id, _now()))
        self._changed()
        return self.tag(cur.lastrowid)

    def rename_tag(self, tag_id: int, new_name: str) -> Tag:
        new_name = clean_name(new_name)
        tag = self.tag(tag_id)
        existing = self.find_tag(new_name)
        if existing and existing.id != tag_id:
            raise DuplicateName(f"“{existing.name}” already exists. Merge them instead?", existing.id)
        self.conn.execute("UPDATE tags SET name = ? WHERE id = ?", (new_name, tag_id))
        self.conn.execute("UPDATE OR REPLACE file_tags SET tag_name = ? WHERE tag_name = ? COLLATE NOCASE",
                          (new_name, tag.name))
        self._changed()
        return self.tag(tag_id)

    def merge_tags(self, source_id: int, target_id: int) -> Tag:
        """Fold one tag into another: the source disappears, files keep the target."""
        if source_id == target_id:
            return self.tag(target_id)
        source, target = self.tag(source_id), self.tag(target_id)
        self.conn.execute("UPDATE OR IGNORE file_tags SET tag_name = ? WHERE tag_name = ? COLLATE NOCASE",
                          (target.name, source.name))
        self.conn.execute("DELETE FROM file_tags WHERE tag_name = ? COLLATE NOCASE", (source.name,))
        self.conn.execute("DELETE FROM tags WHERE id = ?", (source_id,))
        self._changed()
        return target

    def move_tag(self, tag_id: int, category_id: int | None) -> Tag:
        self.tag(tag_id)
        if category_id is not None:
            self.category(category_id)
        self.conn.execute("UPDATE tags SET category_id = ? WHERE id = ?", (category_id, tag_id))
        self._changed()
        return self.tag(tag_id)

    def delete_tag(self, tag_id: int) -> Tag:
        tag = self.tag(tag_id)
        self.conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        self._changed()
        return tag

    def is_genre_tag(self, name: str) -> bool:
        tag = self.find_tag(name)
        return bool(tag and tag.category_id is not None and tag.category_id in self.genre_categories())

    def tags_under(self, category_id: int) -> list[Tag]:
        ids = {category_id} | self.descendants(category_id)
        return [t for t in self.tags() if t.category_id in ids]

    # ------------------------------------------------------------------ remembered files
    def remember_file(self, path: str | os.PathLike, tags: list[str]) -> None:
        key = file_key(path)
        try:
            stat = os.stat(path)
            size, mtime = stat.st_size, stat.st_mtime
        except OSError:
            size, mtime = None, None
        row = self.conn.execute("SELECT id FROM files WHERE key = ?", (key,)).fetchone()
        if row:
            file_id = row["id"]
            self.conn.execute("UPDATE files SET path = ?, size = ?, mtime = ?, saved_at = ?, indexed_mtime = NULL, "
                              "missing = 0 WHERE id = ?", (str(path), size, mtime, _now(), file_id))
            self.conn.execute("DELETE FROM file_tags WHERE file_id = ?", (file_id,))
        else:
            cur = self.conn.execute("INSERT INTO files(key, path, size, mtime, saved_at) VALUES(?, ?, ?, ?, ?)",
                                    (key, str(path), size, mtime, _now()))
            file_id = cur.lastrowid
        self.conn.executemany("INSERT OR IGNORE INTO file_tags(file_id, tag_name) VALUES(?, ?)",
                              [(file_id, t) for t in tags])
        self.conn.commit()

    def forget_file(self, path: str | os.PathLike) -> None:
        self.conn.execute("DELETE FROM files WHERE key = ?", (file_key(path),))
        self.conn.commit()

    # ------------------------------------------------------------------ history (undo)
    def record_history(self, path: str | os.PathLike, *, keywords: list[str], genre: list[str], title: str = "",
                       year: int | None = None, description: str = "", rating: int = 0, label: str = "",
                       batch: str | None = None) -> None:
        """Remember a file's state from just before a save. Keeps the newest HISTORY_KEEP per file."""
        key = file_key(path)
        self.conn.execute(
            "INSERT INTO file_history(key, path, saved_at, batch, label, keywords, genre, title, year, description,"
            " rating) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, str(path), _now(), batch, label, json.dumps(list(keywords)), json.dumps(list(genre)),
             title or "", year, description or "", int(rating or 0)))
        self.conn.execute(
            "DELETE FROM file_history WHERE key = ? AND id NOT IN "
            "(SELECT id FROM file_history WHERE key = ? ORDER BY id DESC LIMIT ?)", (key, key, HISTORY_KEEP))
        self.conn.commit()

    @staticmethod
    def _history_entry(row) -> HistoryEntry:
        return HistoryEntry(row["id"], row["path"], row["saved_at"], row["batch"], row["label"],
                            json.loads(row["keywords"]), json.loads(row["genre"]), row["title"], row["year"],
                            row["description"], row["rating"])

    def history(self, path: str | os.PathLike) -> list[HistoryEntry]:
        """Earlier states of a file, newest first."""
        rows = self.conn.execute("SELECT * FROM file_history WHERE key = ? ORDER BY id DESC",
                                 (file_key(path),)).fetchall()
        return [self._history_entry(r) for r in rows]

    def last_batch(self) -> list[HistoryEntry]:
        """The entries of the most recent bulk change (empty if there's none)."""
        row = self.conn.execute("SELECT batch FROM file_history WHERE batch IS NOT NULL ORDER BY id DESC LIMIT 1"
                                ).fetchone()
        if row is None:
            return []
        rows = self.conn.execute("SELECT * FROM file_history WHERE batch = ? ORDER BY id", (row["batch"],)).fetchall()
        return [self._history_entry(r) for r in rows]

    def changed_since(self, entry: HistoryEntry) -> bool:
        """True when the file was saved again after this entry was recorded."""
        row = self.conn.execute("SELECT 1 FROM file_history WHERE key = ? AND id > ? LIMIT 1",
                                (file_key(entry.path), entry.id)).fetchone()
        return row is not None

    def drop_history(self, entry_id: int) -> None:
        self.conn.execute("DELETE FROM file_history WHERE id = ?", (entry_id,))
        self.conn.commit()

    def move_file(self, old: str | os.PathLike, new: str | os.PathLike) -> None:
        self.conn.execute("UPDATE file_history SET key = ?, path = ? WHERE key = ?",
                          (file_key(new), str(new), file_key(old)))
        row = self.conn.execute("SELECT id FROM files WHERE key = ?", (file_key(old),)).fetchone()
        if not row:
            return
        self.conn.execute("DELETE FROM files WHERE key = ? AND id != ?", (file_key(new), row["id"]))
        self.conn.execute("UPDATE files SET key = ?, path = ? WHERE id = ?", (file_key(new), str(new), row["id"]))
        self.conn.commit()

    def remembered_files(self) -> list[str]:
        return [r["path"] for r in self.conn.execute("SELECT path FROM files ORDER BY path").fetchall()]

    def file_tags(self, path: str | os.PathLike) -> list[str]:
        rows = self.conn.execute(
            "SELECT tag_name FROM file_tags JOIN files ON files.id = file_tags.file_id WHERE files.key = ?",
            (file_key(path),)).fetchall()
        return [r["tag_name"] for r in rows]

    def files_with_tags(self, names: list[str]) -> list[str]:
        if not names:
            return []
        marks = ",".join("?" for _ in names)
        rows = self.conn.execute(
            f"SELECT DISTINCT files.path FROM files JOIN file_tags ON files.id = file_tags.file_id "
            f"WHERE file_tags.tag_name COLLATE NOCASE IN ({marks}) ORDER BY files.path", names).fetchall()
        return [r["path"] for r in rows]

    def tag_usage(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT tag_name, COUNT(*) AS n FROM file_tags GROUP BY tag_name COLLATE NOCASE").fetchall()
        return {r["tag_name"].casefold(): r["n"] for r in rows}

    # ------------------------------------------------------------------ library index
    def index_state(self, path: str | os.PathLike) -> tuple[int | None, float | None] | None:
        """(size, mtime) of the file when it was last read, or None if it isn't indexed yet."""
        row = self.conn.execute("SELECT indexed_size, indexed_mtime FROM files WHERE key = ?",
                                (file_key(path),)).fetchone()
        if row is None or row["indexed_mtime"] is None:
            return None
        return row["indexed_size"], row["indexed_mtime"]

    def index_file(self, path: str | os.PathLike, *, tags: list[str], container: str, title: str = "",
                   year: int | None = None, rating: int = 0, has_cover: bool = False,
                   duration: float | None = None, genre: list[str] | None = None,
                   size: int | None = None, mtime: float | None = None, commit: bool = True) -> None:
        """Record what a file holds (from a library scan)."""
        key = file_key(path)
        row = self.conn.execute("SELECT id FROM files WHERE key = ?", (key,)).fetchone()
        values = (str(path), size, mtime, container, title, year, rating, int(has_cover), duration,
                  "; ".join(genre or []), size, mtime)
        if row:
            file_id = row["id"]
            self.conn.execute("UPDATE files SET path = ?, size = ?, mtime = ?, container = ?, title = ?, year = ?, "
                              "rating = ?, has_cover = ?, duration = ?, genre = ?, indexed_size = ?, "
                              "indexed_mtime = ?, missing = 0 WHERE id = ?", values + (file_id,))
            self.conn.execute("DELETE FROM file_tags WHERE file_id = ?", (file_id,))
        else:
            cur = self.conn.execute("INSERT INTO files(key, path, size, mtime, container, title, year, rating, "
                                    "has_cover, duration, genre, indexed_size, indexed_mtime) "
                                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (key,) + values)
            file_id = cur.lastrowid
        self.conn.executemany("INSERT OR IGNORE INTO file_tags(file_id, tag_name) VALUES(?, ?)",
                              [(file_id, t) for t in tags])
        if commit:
            self.conn.commit()

    def mark_missing(self, path: str | os.PathLike, missing: bool = True, commit: bool = True) -> None:
        self.conn.execute("UPDATE files SET missing = ? WHERE key = ?", (int(missing), file_key(path)))
        if commit:
            self.conn.commit()

    def commit(self) -> None:
        self.conn.commit()

    def index_states(self) -> dict[str, tuple[int | None, float | None]]:
        """{file key: (size, mtime) when last read} for every indexed file, in one query."""
        rows = self.conn.execute("SELECT key, indexed_size, indexed_mtime FROM files "
                                 "WHERE indexed_mtime IS NOT NULL").fetchall()
        return {r["key"]: (r["indexed_size"], r["indexed_mtime"]) for r in rows}

    def forget_folder(self, folder: str | os.PathLike) -> int:
        """Drop every file under `folder` from the library (the files themselves aren't touched)."""
        prefix = file_key(folder).rstrip("\\/") + os.sep
        rows = self.conn.execute("SELECT id, key FROM files").fetchall()
        gone = [r["id"] for r in rows if r["key"].startswith(prefix)]
        self.conn.executemany("DELETE FROM files WHERE id = ?", [(i,) for i in gone])
        self.conn.commit()
        return len(gone)

    def library(self) -> list[FileRecord]:
        tags: dict[int, list[str]] = {}
        for r in self.conn.execute("SELECT file_id, tag_name FROM file_tags").fetchall():
            tags.setdefault(r["file_id"], []).append(r["tag_name"])
        out = []
        for r in self.conn.execute("SELECT * FROM files ORDER BY path COLLATE NOCASE").fetchall():
            container = r["container"] or ("mkv" if str(r["path"]).lower().endswith(".mkv") else "mp4")
            out.append(FileRecord(r["path"], sorted(tags.get(r["id"], []), key=str.casefold), container,
                                  r["title"] or "", r["year"], r["rating"] or 0, bool(r["has_cover"]),
                                  r["duration"], [g for g in (r["genre"] or "").split("; ") if g],
                                  bool(r["missing"]), r["saved_at"]))
        return out

    # ------------------------------------------------------------------ backup / import / export
    def export_data(self) -> dict:
        def node(cat: Category) -> dict:
            return {
                "name": cat.name,
                "write_genre": cat.write_genre,
                "tags": [t.name for t in self.tags_in(cat.id)],
                "categories": [node(c) for c in self.children(cat.id)],
            }
        return {
            "format": "runemark-tags",
            "version": 1,
            "exported_at": _now(),
            "uncategorized": [t.name for t in self.tags_in(None)],
            "categories": [node(c) for c in self.children(None)],
        }

    def export_json(self, path: str | os.PathLike) -> None:
        Path(path).write_text(json.dumps(self.export_data(), indent=2, ensure_ascii=False), encoding="utf-8")

    def import_data(self, data: dict, replace: bool = False) -> tuple[int, int]:
        """Add categories and tags from an export. Returns (categories added, tags added).

        Merge mode keeps everything already here; a tag that already exists stays where it is.
        """
        if data.get("format") != "runemark-tags":
            raise TagDBError("That file isn't a Runemark tag export.")
        added_cats = added_tags = 0
        with self.conn:
            if replace:
                self.conn.execute("DELETE FROM tags")
                self.conn.execute("DELETE FROM categories")

            def walk(nodes: list, parent_id: int | None) -> None:
                nonlocal added_cats, added_tags
                for item in nodes:
                    name = clean_name(item.get("name", ""), "Category")
                    existing = self._sibling_clash(name, parent_id)
                    if existing:
                        cat_id = existing.id
                        if item.get("write_genre"):
                            self.conn.execute("UPDATE categories SET write_genre = 1 WHERE id = ?", (cat_id,))
                    else:
                        cur = self.conn.execute(
                            "INSERT INTO categories(name, parent_id, write_genre) VALUES(?, ?, ?)",
                            (name, parent_id, int(bool(item.get("write_genre")))))
                        cat_id = cur.lastrowid
                        added_cats += 1
                    for tag_name in item.get("tags", []):
                        added_tags += self._import_tag(tag_name, cat_id)
                    walk(item.get("categories", []), cat_id)

            for tag_name in data.get("uncategorized", []):
                added_tags += self._import_tag(tag_name, None)
            walk(data.get("categories", []), None)
        self._changed()
        return added_cats, added_tags

    def _import_tag(self, name: str, category_id: int | None) -> int:
        try:
            name = clean_name(name)
        except InvalidName:
            return 0
        if self.find_tag(name):
            return 0
        self.conn.execute("INSERT INTO tags(name, category_id, created_at) VALUES(?, ?, ?)",
                          (name, category_id, _now()))
        return 1

    def import_json(self, path: str | os.PathLike, replace: bool = False) -> tuple[int, int]:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return self.import_data(data, replace=replace)

    def backup_to(self, target: str | os.PathLike) -> Path:
        """A complete, closed copy of the database at `target` (safe to sync with OneDrive)."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".part")
        dest = sqlite3.connect(str(tmp))
        try:
            self.conn.backup(dest)
            dest.execute("PRAGMA journal_mode = DELETE")
        finally:
            dest.close()
        os.replace(tmp, target)
        return target

    @staticmethod
    def check_backup(path: str | os.PathLike) -> dict:
        """Counts from a backup file, or TagDBError if it isn't a Runemark tag database."""
        try:
            conn = sqlite3.connect(_read_only_uri(path), uri=True)
        except sqlite3.Error as exc:
            raise TagDBError(f"Couldn't open that file: {exc}") from exc
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if not {"meta", "categories", "tags", "files"} <= names:
                raise TagDBError("That file isn't a Runemark tag database.")
            return {"tags": conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0],
                    "categories": conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0],
                    "files": conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]}
        except sqlite3.DatabaseError as exc:
            raise TagDBError(f"That file isn't a Runemark tag database ({exc}).") from exc
        finally:
            conn.close()

    def restore_from(self, path: str | os.PathLike) -> None:
        """Replace everything in this database with a backup's contents."""
        self.check_backup(path)
        source = sqlite3.connect(_read_only_uri(path), uri=True)
        try:
            source.backup(self.conn)
        finally:
            source.close()
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()
        self._genre_cache = None

    def backup(self, folder: str | os.PathLike, keep: int = 10) -> Path:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.backup_to(folder / f"tags-{stamp}.db")
        backups = sorted(folder.glob("tags-*.db"))
        for old in backups[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass
        return target
