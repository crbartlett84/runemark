"""0.10.1: scans that list the folders themselves, and save fingerprints."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from runemark import library as L, saving
from runemark.media.mp4meta import VideoMeta
from runemark.tagdb import TagDB
from runemark.tagsync import save_mp4


@pytest.fixture(autouse=True)
def no_handlers():
    saving.clear_save_handlers()
    L.clear_scan_providers()
    saving._written.clear()
    yield
    saving.clear_save_handlers()
    L.clear_scan_providers()
    saving._written.clear()


def _area(tmp_path, media, n=3):
    area = tmp_path / "share" / "Feed"
    (area / "sub").mkdir(parents=True)
    files = []
    for i in range(n):
        p = (area / "sub" if i % 2 else area) / f"v{i}.mp4"
        shutil.copy2(media["mp4"], p)
        save_mp4(p, keywords=[f"Tag {i}"], genre=[], meta=VideoMeta(f"Title {i}", 2000 + i))
        files.append(p)
    return area, files


def _record(path: Path, **extra):
    st = path.stat()
    rec = dict(L.read_for_index(path), path=str(path), size=st.st_size, mtime=st.st_mtime)
    rec.update(extra)
    return rec


def _counting(monkeypatch):
    opened = []
    real = L.read_for_index
    monkeypatch.setattr(L, "read_for_index", lambda p: (opened.append(Path(p).name), real(p))[1])
    return opened


def test_files_the_provider_does_not_list_yet_are_read_not_missing(tmp_path, media, monkeypatch):
    """A provider covering the whole area that hasn't indexed a new, just-moved or just-changed file yet: the file
    is read here, never marked missing."""
    area, files = _area(tmp_path, media)
    records = [_record(files[0]), _record(files[1])]          # v2 isn't in the provider's index yet
    L.add_scan_provider("Test", lambda given, cancel: L.ScanListing([str(area)], list(records)))
    opened = _counting(monkeypatch)
    db = TagDB(tmp_path / "tags.db")
    try:
        result = L.scan(db, [str(area)])
        rows = {r.path: r for r in db.library()}
        assert opened == ["v2.mp4"] and result.missing == 0 and result.read == 3
        assert not any(r.missing for r in rows.values()) and rows[str(files[2])].title == "Title 2"

        # a new download lands while the provider still has nothing for it
        new = area / "sub" / "new.mp4"
        shutil.copy2(files[0], new)
        opened.clear()
        result = L.scan(db, [str(area)])
        assert opened == ["new.mp4"] and result.missing == 0
        assert not {r.path: r for r in db.library()}[str(new)].missing
    finally:
        db.close()


def test_a_provider_time_rounded_by_the_other_route_still_matches(tmp_path, media, monkeypatch):
    area, files = _area(tmp_path, media, 2)
    for f in files:
        os.utime(f, (1_700_000_000.37, 1_700_000_000.37))
    # the other computer reports whole seconds (Samba, FAT and friends round)
    records = [_record(f, mtime=float(int(f.stat().st_mtime) + 1)) for f in files]
    L.add_scan_provider("Test", lambda given, cancel: L.ScanListing([str(area)], list(records)))
    opened = _counting(monkeypatch)
    db = TagDB(tmp_path / "tags.db")
    try:
        result = L.scan(db, [str(area)])
        assert opened == [] and result.read == 2
        # stored with the time seen here, so the next scan finds nothing to do
        assert L.scan(db, [str(area)]).read == 0 and opened == []
        # changed here (same size), the provider's record now 30 s off: read here
        later = files[0].stat().st_mtime + 30.5
        os.utime(files[0], (later, later))
        records[1] = dict(records[1], mtime=records[1]["mtime"] - 3)
        L.scan(db, [str(area)])
        assert opened == ["v0.mp4"]
    finally:
        db.close()


def test_a_stale_provider_record_after_a_local_save_is_not_used(tmp_path, media, monkeypatch):
    area, files = _area(tmp_path, media, 2)
    stale = [_record(f) for f in files]
    L.add_scan_provider("Test", lambda given, cancel: L.ScanListing([str(area)], list(stale)))
    db = TagDB(tmp_path / "tags.db")
    try:
        L.scan(db, [str(area)])
        save_mp4(files[0], keywords=["Saved here"], genre=[], meta=VideoMeta("Saved here", 2020))
        later = files[0].stat().st_mtime + 300         # a save comes well after the provider read the file
        os.utime(files[0], (later, later))
        opened = _counting(monkeypatch)
        L.scan(db, [str(area)])                        # the provider still has the old version: read here
        assert opened == ["v0.mp4"]
        assert {r.path: r for r in db.library()}[str(files[0])].title == "Saved here"
        L.reindex(db, files[0])                       # the editor does this after its own saves
        opened.clear()
        assert L.scan(db, [str(area)]).read == 0 and opened == []
    finally:
        db.close()


def test_skipped_and_left_out_folders_are_still_respected(tmp_path, media):
    area, files = _area(tmp_path, media, 3)
    L.add_scan_provider("Test", lambda given, cancel: L.ScanListing([str(area)], [_record(f) for f in files],
                                                                   [str(area / "sub")]))
    db = TagDB(tmp_path / "tags.db")
    try:
        L.scan(db, [str(area)], skip=[str(area / "sub")])
        assert {r.path for r in db.library()} == {str(files[0]), str(files[2])}
    finally:
        db.close()


# ---------------------------------------------------------------- fingerprints
def _expected_fp(data: bytes) -> str:
    part = 1 << 20
    return hashlib.sha256(data[:part] + data[max(0, len(data) - part):]).hexdigest()


def test_the_fingerprint_is_the_first_and_last_mib(tmp_path):
    for size in (0, 10, (1 << 20) - 1, (1 << 20) + 5, 3 * (1 << 20) + 7):
        data = os.urandom(size)
        p = tmp_path / f"f{size}.bin"
        p.write_bytes(data)
        assert saving.fingerprint(p) == _expected_fp(data)
    big = bytearray(os.urandom(3 * (1 << 20)))
    p = tmp_path / "big.bin"
    p.write_bytes(big)
    before = saving.fingerprint(p)
    big[(1 << 20) + 100] ^= 1                     # the middle isn't part of it
    p.write_bytes(big)
    assert saving.fingerprint(p) == before
    big[-1] ^= 1                                  # the end is
    p.write_bytes(big)
    assert saving.fingerprint(p) != before
    assert saving.fingerprint(tmp_path / "nothing.bin") is None


def test_the_open_stamp_carries_the_fingerprint_to_the_request(tmp_path, media):
    path = tmp_path / "a.mp4"
    shutil.copy2(media["mp4"], path)
    size, mtime, fp = saving.open_stamp(path)
    assert (size, mtime) == saving.stamp(path) and fp == saving.fingerprint(path)
    assert saving.read_stamp(path) == saving.stamp(path)             # no handler: the file isn't read for it
    saving.add_save_handler("X", lambda r, p: None)
    assert saving.read_stamp(path) == (size, mtime, fp)
    req = saving.SaveRequest(path, ["Lake"], [], expected_size=size, expected_mtime=mtime, expected_fp=fp)
    assert req.expected == {"size": size, "mtime": mtime, "fp": fp}
    back = saving.SaveRequest.from_dict(path, json.loads(json.dumps(req.to_dict())), json.loads(json.dumps(req.expected)))
    assert back.expected_fp == fp and back.expected == req.expected
    assert "fp" not in saving.SaveRequest(path, [], [], expected_size=1, expected_mtime=2.0).expected

    # right after a handler wrote it, the handler's values stand in while the share catches up
    saving._remember_written(req, saving.SaveResult.handled([], size + 10, mtime + 5, "f" * 64))
    assert saving.open_stamp(path) == (size + 10, mtime + 5, "f" * 64)
