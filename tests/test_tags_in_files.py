import hashlib
import shutil
import subprocess

import pytest

from runemark import explorer
from runemark.media import atoms, mp4meta, xtra
from runemark.tagsync import FileState, read_state, route_fields, save_mp4

EXIFTOOL = shutil.which("exiftool")


def media_md5(path):
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?",
                          "-f", "md5", "-"], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def decodes_cleanly(path):
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
                         capture_output=True, text=True)
    return out.returncode == 0 and not out.stderr.strip()


@pytest.mark.parametrize("name", ["mp4", "mp4_faststart", "mp4_cover"])
def test_mp4_tag_roundtrip_keeps_media(copy_of, media, name):
    path = copy_of(name)
    before = media_md5(path)
    cover = media["cover_jpg"].read_bytes()
    mp4meta.write_mp4_tags(path, keywords=["Alpha", "Beta Gamma", "Ünïcode ✓"], genre=["Alpha"], cover=cover)
    tags = mp4meta.read_mp4_tags(path)
    assert tags.keywords == ["Alpha", "Beta Gamma", "Ünïcode ✓"]
    assert tags.genre == ["Alpha"]
    assert tags.cover == cover
    assert media_md5(path) == before
    assert decodes_cleanly(path)


def test_cover_unchanged_and_removed(copy_of, media):
    path = copy_of("mp4_cover")
    original = mp4meta.read_mp4_tags(path).cover
    assert original
    mp4meta.write_mp4_tags(path, keywords=["x"], genre=[])
    assert mp4meta.read_mp4_tags(path).cover == original
    mp4meta.write_mp4_tags(path, keywords=["x"], genre=[], cover=None)
    assert mp4meta.read_mp4_tags(path).cover is None


@pytest.mark.parametrize("name", ["mp4", "mp4_faststart"])
def test_xtra_written_in_place_and_survives_tag_edits(copy_of, name):
    path = copy_of(name)
    mp4meta.write_mp4_tags(path, keywords=["A"], genre=[])
    size = path.stat().st_size
    moov_before = atoms.read_layout(path).moov
    xtra.write_category(path, ["One", "Two words", "Ünï"])
    assert path.stat().st_size == size                      # nothing moved
    assert atoms.read_layout(path).moov.size == moov_before.size
    assert xtra.read_category(path) == ["One", "Two words", "Ünï"]
    mp4meta.write_mp4_tags(path, keywords=["A", "B"], genre=["B"])
    assert xtra.read_category(path) == ["One", "Two words", "Ünï"]  # mutagen keeps the Xtra box
    xtra.write_category(path, ["Only"])
    assert xtra.read_category(path) == ["Only"]
    xtra.write_category(path, [])
    assert xtra.read_category(path) == []
    assert decodes_cleanly(path)


def test_xtra_keeps_other_windows_entries():
    other = xtra.encode_entry("WM/SharedUserRating", ["99"])
    payload = other + xtra.encode_entry(xtra.CATEGORY, ["old"])
    new = xtra.replace_category(payload, ["a", "b"])
    names = [n for n, _, _ in xtra.parse_entries(new)]
    assert names == ["WM/SharedUserRating", xtra.CATEGORY]
    assert xtra.category_from_payload(new) == ["a", "b"]
    assert [n for n, _, _ in xtra.parse_entries(xtra.replace_category(payload, []))] == ["WM/SharedUserRating"]


def test_xtra_needs_padding_when_no_tag_area(copy_of):
    path = copy_of("mp4")
    with pytest.raises(xtra.NeedsPadding):
        xtra.write_category(path, ["x"])


@pytest.mark.skipif(not EXIFTOOL, reason="exiftool not installed")
def test_exiftool_reads_our_explorer_tags(copy_of):
    path = copy_of("mp4")
    mp4meta.write_mp4_tags(path, keywords=["Lake", "Aubree"], genre=[])
    xtra.write_category(path, ["Lake", "Aubree"])
    out = subprocess.run([EXIFTOOL, "-s", "-s", "-s", "-Microsoft:Category", str(path)],
                         capture_output=True, text=True).stdout.strip()
    assert out == "Lake, Aubree"


@pytest.mark.parametrize("name", ["mp4", "mp4_faststart", "mp4_cover"])
def test_save_mp4_syncs_everything(copy_of, media, name):
    path = copy_of(name)
    before = media_md5(path)
    jpeg = media["cover_jpg"].read_bytes()
    outcome = save_mp4(path, keywords=["Lake", "Aubree", "Comedy"], genre=["Comedy"], cover=jpeg,
                       explorer_mode=explorer.MODE_BUILTIN)
    assert outcome.ok, outcome.problems
    state = read_state(path)
    assert state.keywords == ["Lake", "Aubree", "Comedy"]
    assert state.explorer == ["Lake", "Aubree", "Comedy"]
    assert state.genre == ["Comedy"]
    assert state.cover == jpeg
    assert state.sync().in_sync
    assert media_md5(path) == before
    # editing again shrinks and grows cleanly
    outcome = save_mp4(path, keywords=["Lake"], genre=[], explorer_mode=explorer.MODE_BUILTIN)
    assert outcome.ok, outcome.problems
    state = read_state(path)
    assert (state.keywords, state.explorer, state.genre, state.cover) == (["Lake"], ["Lake"], [], jpeg)
    assert decodes_cleanly(path)


def test_save_uses_working_copy_when_media_would_move(copy_of, media):
    path = copy_of("mp4_faststart")
    big_cover = media["cover_jpg"].read_bytes() + b"\x00" * 200_000  # forces the tag area to grow a lot
    outcome = save_mp4(path, keywords=["x"], genre=[], cover=big_cover, explorer_mode=explorer.MODE_BUILTIN)
    assert outcome.used_copy
    assert outcome.ok, outcome.problems
    assert not list(path.parent.glob("*.runemark-tmp*"))
    assert decodes_cleanly(path)


def test_preserve_modified_date(copy_of):
    import os
    path = copy_of("mp4")
    os.utime(path, (1_600_000_000, 1_600_000_000))
    save_mp4(path, keywords=["x"], genre=[], explorer_mode=explorer.MODE_BUILTIN, preserve_mtime=True)
    assert int(path.stat().st_mtime) == 1_600_000_000


def test_sync_report_and_union():
    state = FileState(path=None, container="mp4", keywords=["A", "B"], genre=["G"], explorer=["b", "C"], cover=None)
    report = state.sync()
    assert not report.in_sync
    assert report.only_in_keywords == ["A"]
    assert report.only_in_explorer == ["C"]
    assert state.all_tags() == ["A", "B", "C", "G"]


def test_route_fields_keeps_foreign_genres_until_filed():
    known = {"comedy", "lake"}
    genre_tags = {"comedy"}
    keywords, genre = route_fields(["Lake", "Comedy", "Western"],
                                   is_genre=lambda t: t.casefold() in genre_tags,
                                   is_known=lambda t: t.casefold() in known,
                                   original_genre=["Western"])
    assert keywords == ["Lake", "Comedy", "Western"]
    assert genre == ["Comedy", "Western"]
    known.add("western")  # once filed in a non-genre category, it leaves Genre
    _, genre = route_fields(["Lake", "Comedy", "Western"], is_genre=lambda t: t.casefold() in genre_tags,
                            is_known=lambda t: t.casefold() in known, original_genre=["Western"])
    assert genre == ["Comedy"]


def test_split_values():
    assert mp4meta.split_values(["a; b;;  c ", "B", "d"]) == ["a", "b", "c", "d"]


def test_media_hash_helper_is_stable(media):
    assert media_md5(media["mp4"]) == media_md5(media["mp4"])
    assert hashlib.sha256(b"x").hexdigest()
