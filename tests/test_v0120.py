"""0.12.0: plugins add entries to the Library's filter list (plugin API 5)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from runemark import plugins
from runemark.settings import Settings
from runemark.tagdb import TagDB

from test_v070 import FakeWindow
from test_v090 import needs_window, qapp  # noqa: F401 (fixture)


def test_plugin_api_adds_a_library_filter(tmp_path):
    db = TagDB(tmp_path / "t.db")
    win, host = FakeWindow(db, Settings()), plugins.PluginHost()
    win.columns_changed = 0
    win.library_columns_changed = lambda: setattr(win, "columns_changed", win.columns_changed + 1)
    api = plugins.PluginAPI(win, host, plugins.Plugin(tmp_path, "lore"))
    assert api.api_version >= 5
    api.add_library_filter("In Chris's Stash", lambda p: True)
    assert [f.key for f in host.library_filters] == ["plugin:lore:In Chris's Stash"] and win.columns_changed == 1
    db.close()


def test_library_window_shows_and_applies_a_plugin_filter(tmp_path, qapp):
    from runemark.ui.library_window import LibraryWindow
    db = TagDB(tmp_path / "tags.db")
    try:
        for i in range(3):
            db.index_file(tmp_path / f"c{i}.mp4", tags=["Lake"] if i else [], container="mp4", title=f"C{i}")
        known, prepared = {}, []
        host = SimpleNamespace(plugins=plugins.PluginHost())
        host.plugins.library_filters.append(plugins.LibraryFilter(
            "lore", "In Chris's Stash", lambda p: known.get(p.rsplit("c", 1)[-1]), prepared.append))
        w = LibraryWindow(db, Settings.load(), host=host)
        w.plugin_columns_changed()
        at = w.filter.findData("plugin:lore:In Chris's Stash")
        assert at > 0 and w.filter.itemText(at) == "In Chris's Stash"
        w.filter.setCurrentIndex(at)
        assert len(prepared[-1]) == 3 and w.model.rowCount() == 0     # nothing known yet: nothing shown
        known.update({"1.mp4": True, "2.mp4": False})
        w.plugin_columns_changed()                                    # the plugin says its answers are in
        assert w.model.rowCount() == 1
        w.search.tags = ["Lake"]
        w._apply()
        assert w.model.rowCount() == 1                                # with the search's tags too
        known["0.mp4"] = True
        w._apply()
        assert w.model.rowCount() == 1                                # c0 has no Lake tag
        w.plugin_columns_changed()                                    # added once, not again
        assert sum(1 for i in range(w.filter.count()) if w.filter.itemData(i) == "plugin:lore:In Chris's Stash") == 1
        w.close()
    finally:
        db.close()


# ---------------------------------------------------------------- the local copy of network videos (0.12.0)
def _cache(tmp_path, **kw):
    from runemark.media.cache import VideoCache
    share = tmp_path / "share"
    share.mkdir(exist_ok=True)
    return VideoCache(tmp_path / "cache", is_remote=lambda p: share in Path(p).parents, **kw), share


def test_a_network_video_is_copied_and_played_from_the_copy(tmp_path):
    cache, share = _cache(tmp_path)
    v = share / "a.mp4"
    v.write_bytes(b"x" * 1000)
    local = tmp_path / "elsewhere.mp4"
    local.write_bytes(b"y")
    assert cache.source_for(local) == local                  # not on the network: played where it is
    assert cache.source_for(v) == v                          # not copied yet: the original
    copy = cache.copy(v)
    assert copy.read_bytes() == v.read_bytes() and cache.source_for(v) == copy
    # a save changes the original: the older copy plays until a new one is made, which replaces it
    v.write_bytes(b"z" * 1001)
    import os
    os.utime(v, ns=(1, 1))
    assert cache.fresh_copy(v) is None and cache.source_for(v) == copy
    new = cache.copy(v)
    assert new != copy and not copy.exists() and new.read_bytes() == v.read_bytes()


def test_the_cache_stays_under_its_limit_dropping_the_oldest_but_never_the_one_playing(tmp_path):
    import os
    cache, share = _cache(tmp_path, limit_bytes=2500)
    videos = []
    for i in range(3):
        v = share / f"v{i}.mp4"
        v.write_bytes(bytes([i]) * 1000)
        videos.append(v)
    first = cache.copy(videos[0])
    os.utime(first, (1, 1))                                   # used longest ago
    second = cache.copy(videos[1])
    os.utime(second, (2, 2))
    cache.playing = {str(first)}
    cache.copy(videos[2])
    assert first.exists() and not second.exists() and cache.size() <= 2500
    big = share / "big.mp4"
    big.write_bytes(b"b" * 1300)
    assert cache.copy(big) is None                           # more than half the cache: not copied


def test_want_copies_the_open_one_and_the_next_ones_in_the_background(tmp_path):
    import threading
    done, ready = [], threading.Event()
    cache, share = _cache(tmp_path, ahead=2)
    cache.ready = lambda p: (done.append(p.name), len(done) == 3 and ready.set())
    videos = []
    for i in range(5):
        v = share / f"v{i}.mp4"
        v.write_bytes(b"x" * 10)
        videos.append(v)
    cache.want(videos)
    assert ready.wait(10)
    assert done == ["v0.mp4", "v1.mp4", "v2.mp4"] and cache.fresh_copy(videos[3]) is None
    assert cache.clear() == 30 and cache.size() == 0
    cache.stop()


def test_network_paths():
    from runemark.media.cache import is_network_path
    assert is_network_path(r"\\nas\share\a.mp4") and is_network_path(r"\\?\UNC\nas\share\a.mp4")
    assert not is_network_path(r"\\?\C:\v\a.mp4") and not is_network_path("/home/x/a.mp4")


def test_player_switches_to_the_copy_where_it_was(tmp_path, qapp, media):
    from runemark.media.probe import probe
    from runemark.ui.player import PlayerPanel
    import shutil
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    shutil.copy2(media["mp4"], a)
    shutil.copy2(media["mp4"], b)
    panel = PlayerPanel()
    panel.source_for = lambda p: b                            # "b" stands for a's local copy
    panel.load(a, probe(a))
    assert panel.path == a and panel.source == b
    assert not panel.switch_source(b)                         # that one already
    assert panel.switch_source(a) and panel.source == a and panel.path == a
    panel.clear()


@needs_window
def test_a_reject_moves_a_video_with_unsaved_changes(tmp_path, media, qapp):
    from test_v090 import _close, _window, wait_until
    win, db, (a, b) = _window(tmp_path, media)
    try:
        win.open_paths([a, b])
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == a)
        win._tags_edited(["Lake"])
        assert win.doc.dirty()
        got = []
        win.plugin_move(a, tmp_path / "_Rejected", None, lambda p, r: got.append((p, r)), open_after=False)
        assert wait_until(qapp, lambda: got, 10) and got[0][1] == "unsaved changes"
        got.clear()
        win.plugin_move(a, tmp_path / "_Rejected", None, lambda p, r: got.append((p, r)), open_after=False,
                        discard_changes=True)
        assert wait_until(qapp, lambda: got, 10) and got[0][0] == tmp_path / "_Rejected" / "a.mp4"
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == b)
    finally:
        _close(win, db, qapp)
