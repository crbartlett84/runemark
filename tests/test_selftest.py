from runemark import selftest


def test_self_test_passes_here():
    results = selftest.run()
    text = selftest.report(results)
    failed = [r for r in results if not r.ok]
    assert not failed, text
    assert "checks passed" in text
