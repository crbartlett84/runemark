"""Library → More → Stats."""
from __future__ import annotations

from html import escape

from PySide6.QtWidgets import QDialog, QPushButton, QTextBrowser, QVBoxLayout

from .. import stats as S
from . import theme


def _pct(n: int, total: int) -> str:
    return f"{round(n * 100 / total)}%" if total else "—"


def html(s: S.Stats) -> str:
    t = theme.current()
    accent = t.accent

    def bar(n: int, most: int) -> str:
        width = int(220 * n / most) if most else 0
        return (f'<table cellspacing="0" cellpadding="0"><tr><td bgcolor="{accent}" width="{max(width, 2)}" '
                f'height="10"></td><td>&nbsp;{n}</td></tr></table>')

    def ranked(title: str, items: list) -> str:
        if not items:
            return f"<h3>{title}</h3><p>None yet.</p>"
        most = items[0][1]
        rows = "".join(f"<tr><td>{escape(str(name))}&nbsp;&nbsp;</td><td>{bar(n, most)}</td></tr>" for name, n in items)
        return f"<h3>{title}</h3><table>{rows}</table>"

    done = [("Tagged", s.tagged), ("With people", s.with_people), ("With a thumbnail", s.with_cover),
            ("With chapters", s.with_chapters), ("With a studio", s.with_studio), ("Rated", s.rated)]
    done_rows = "".join(f"<tr><td>{k}&nbsp;&nbsp;</td><td>{bar(v, s.total)}</td><td>&nbsp;{_pct(v, s.total)}</td></tr>"
                        for k, v in done)
    month_most = max((n for _m, n in s.added_by_month), default=0)
    months = "".join(f"<tr><td>{m}&nbsp;&nbsp;</td><td>{bar(n, month_most)}</td></tr>" for m, n in s.added_by_month)
    stars = "".join(f"<tr><td>{'★' * k or 'Not rated'}&nbsp;&nbsp;</td><td>{bar(s.ratings.get(k, 0), s.total)}</td></tr>"
                    for k in range(5, -1, -1))
    return f"""<h2>{s.total} videos · {s.hours:.0f} hours</h2>
<p>{s.inbox} in the inbox · {s.missing} missing (moved or deleted)</p>
<h3>How much is done</h3><table>{done_rows}</table>
{ranked("Most used tags", s.top_tags)}
{ranked("People in the most videos", s.top_people)}
{ranked("Studios", s.top_studios)}
<h3>Ratings</h3><table>{stars}</table>
<h3>New to the library, by month</h3>{f"<table>{months}</table>" if months else "<p>Shown from version 0.5.0 on.</p>"}"""


class StatsDialog(QDialog):
    def __init__(self, records: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Library stats — Runemark")
        self.resize(640, 720)
        layout = QVBoxLayout(self)
        view = QTextBrowser()
        view.setHtml(html(S.compute(records)))
        layout.addWidget(view, 1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close)
