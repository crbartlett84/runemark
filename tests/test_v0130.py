"""0.13.0: plugins decide who may join People (person checks)."""
from runemark.plugins import PluginHost


def test_person_verdict_no_checks_is_unknown():
    assert PluginHost().person_verdict("Anyone") is None


def test_person_verdict_any_no_wins():
    host = PluginHost()
    host.person_checks.append(("a", lambda n: True))
    host.person_checks.append(("b", lambda n: False if n == "Him" else None))
    assert host.person_verdict("Him") is False
    assert host.person_verdict("Her") is True


def test_person_verdict_unknown_and_failing_check():
    host = PluginHost()
    host.person_checks.append(("a", lambda n: None))

    def broken(_n):
        raise RuntimeError("boom")
    host.person_checks.append(("b", broken))
    assert host.person_verdict("Someone") is None


# ---------------------------------------------------------------- the move runs in the background; a plugin's mover
from test_v090 import needs_window, qapp  # noqa: E402,F401 (fixture)


@needs_window
def test_a_plugin_mover_does_the_move_and_runemark_keeps_its_records(tmp_path, media, qapp):
    import shutil
    from test_v090 import _close, _window, wait_until
    win, db, (a, b) = _window(tmp_path, media)
    try:
        win.open_paths([a, b])
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == a)
        calls = []

        def mover(path, folder, name):
            calls.append((path, folder, name))
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{name}{path.suffix}"
            shutil.move(str(path), str(target))
            return target, [(path, target)]
        got = []
        win.plugin_move(a, tmp_path / "Studios" / "X", "X - a", lambda p, r: got.append((p, r)), open_after=False,
                        mover=mover)
        assert wait_until(qapp, lambda: got, 10)
        assert got[0] == (tmp_path / "Studios" / "X" / "X - a.mp4", "") and calls[0][0] == a
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == b)
        assert all(p != a for p in win.queue)
    finally:
        _close(win, db, qapp)


@needs_window
def test_a_mover_that_fails_changes_nothing(tmp_path, media, qapp):
    from test_v090 import _close, _window, wait_until
    win, db, (a, b) = _window(tmp_path, media)
    try:
        win.open_paths([a, b])
        assert wait_until(qapp, lambda: win.doc is not None and win.doc.path == a)

        def mover(path, folder, name):
            raise OSError("RuneGrab said no")
        got = []
        win.plugin_move(a, tmp_path / "Y", None, lambda p, r: got.append((p, r)), open_after=False, mover=mover)
        assert wait_until(qapp, lambda: got, 10) and got[0] == (None, "RuneGrab said no")
        assert a.exists() and wait_until(qapp, lambda: win.doc is not None and win.doc.path == a)
    finally:
        _close(win, db, qapp)
