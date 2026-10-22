"""Start-up: command line, logging, theme, tag database, main window."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import APP_NAME, __version__
from .paths import app_data_dir, log_dir

log = logging.getLogger(__name__)


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="runemark", description="Tag MP4/MKV videos and set their thumbnails.")
    parser.add_argument("files", nargs="*", help="videos to open")
    parser.add_argument("--self-test", nargs="?", const="", metavar="REPORT",
                        help="run the self-test without the window; write the report to REPORT")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return parser.parse_args(argv)


def _headless_self_test(target: str) -> int:
    from . import selftest
    results = selftest.run()
    text = selftest.report(results)
    out = Path(target) if target else log_dir() / "selftest.txt"
    out.write_text(text, encoding="utf-8")
    if sys.stdout is not None:
        try:
            print(text)
        except Exception:  # noqa: BLE001
            pass
    failed = [r for r in results if not r.ok]
    return 1 if failed else 0


def _daily_backup(db, settings) -> None:
    from .datastore import daily_backup
    try:
        daily_backup(db, settings)
    except Exception as exc:  # noqa: BLE001
        log.warning("tag database backup failed: %s", exc)


def main(argv: list[str] | None = None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    from .logsetup import setup
    setup()
    if args.self_test is not None:
        return _headless_self_test(args.self_test)

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication, QMessageBox

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("")

    from .settings import Settings
    from .tagdb import TagDB
    from .ui import theme
    from .ui.main_window import MainWindow

    settings = Settings.load()
    theme.apply(app, settings.theme)
    app.setWindowIcon(theme.app_icon())
    try:
        db = TagDB(app_data_dir() / "tags.db")
    except Exception as exc:  # noqa: BLE001
        QMessageBox.critical(None, APP_NAME, f"Couldn't open the tag database:\n\n{exc}")
        return 1
    _daily_backup(db, settings)

    from . import proc
    window = MainWindow(db, settings)
    window.show()
    try:
        proc.ffmpeg()
        proc.ffprobe()
    except proc.ToolMissing as exc:
        QMessageBox.critical(window, APP_NAME, str(exc))

    files = [Path(f) for f in args.files]
    if files:
        window.open_paths(files)
    code = app.exec()
    db.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
