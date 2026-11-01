"""Colours, fonts, the Qt stylesheet and the line icons, for light and dark mode."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QByteArray, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QIcon, QLinearGradient, QPainter,
                           QPainterPath, QPalette, QPen, QPixmap)
from PySide6.QtSvg import QSvgRenderer

from ..paths import IS_WINDOWS


@dataclass(frozen=True)
class Palette:
    dark: bool
    bg: str
    surface: str
    surface2: str
    surface3: str
    border: str
    border_strong: str
    text: str
    muted: str
    faint: str
    accent: str
    accent_hover: str
    accent_text: str
    accent_soft: str
    chip: str
    chip_text: str
    genre: str
    success: str
    warning: str
    danger: str
    video_bg: str


DARK = Palette(
    dark=True, bg="#16181d", surface="#1d2027", surface2="#252932", surface3="#2e333e",
    border="#2f3441", border_strong="#3d4352", text="#e9ebf1", muted="#a1a9b9", faint="#6f7789",
    accent="#4cc2ff", accent_hover="#74d0ff", accent_text="#06121c", accent_soft="#173246",
    chip="#1f3347", chip_text="#d6ecff", genre="#c4a7ff", success="#5ad08a", warning="#f2c14e",
    danger="#ff7b72", video_bg="#0b0c0f",
)

LIGHT = Palette(
    dark=False, bg="#f3f4f7", surface="#ffffff", surface2="#f6f7f9", surface3="#eceef3",
    border="#dfe2e8", border_strong="#c9ced8", text="#1a1e26", muted="#5b6374", faint="#8a92a3",
    accent="#0a63c9", accent_hover="#0b70e3", accent_text="#ffffff", accent_soft="#e3effc",
    chip="#e4eefb", chip_text="#0b3a6c", genre="#6a3fd1", success="#1f8a4c", warning="#a86a00",
    danger="#c4352b", video_bg="#0b0c0f",
)

_current: Palette = DARK


def current() -> Palette:
    return _current


def system_prefers_dark() -> bool:
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except Exception:  # noqa: BLE001
        return True


def resolve(mode: str) -> Palette:
    if mode == "light":
        return LIGHT
    if mode == "dark":
        return DARK
    return DARK if system_prefers_dark() else LIGHT


def ui_font(point_size: float = 10.0, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    families = ["Segoe UI Variable Text", "Segoe UI", "Inter", "Noto Sans", "DejaVu Sans"] if IS_WINDOWS \
        else ["Inter", "Noto Sans", "DejaVu Sans", "Segoe UI"]
    font = QFont()
    font.setFamilies(families)
    font.setPointSizeF(point_size)
    font.setWeight(weight)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return font


def apply(app, mode: str) -> Palette:
    global _current
    pal = resolve(mode)
    _current = pal
    app.setStyle("Fusion")
    app.setFont(ui_font(10))
    qpal = QPalette()
    qpal.setColor(QPalette.ColorRole.Window, QColor(pal.bg))
    qpal.setColor(QPalette.ColorRole.WindowText, QColor(pal.text))
    qpal.setColor(QPalette.ColorRole.Base, QColor(pal.surface2))
    qpal.setColor(QPalette.ColorRole.AlternateBase, QColor(pal.surface))
    qpal.setColor(QPalette.ColorRole.Text, QColor(pal.text))
    qpal.setColor(QPalette.ColorRole.Button, QColor(pal.surface2))
    qpal.setColor(QPalette.ColorRole.ButtonText, QColor(pal.text))
    qpal.setColor(QPalette.ColorRole.Highlight, QColor(pal.accent))
    qpal.setColor(QPalette.ColorRole.HighlightedText, QColor(pal.accent_text))
    qpal.setColor(QPalette.ColorRole.ToolTipBase, QColor(pal.surface3))
    qpal.setColor(QPalette.ColorRole.ToolTipText, QColor(pal.text))
    qpal.setColor(QPalette.ColorRole.PlaceholderText, QColor(pal.faint))
    qpal.setColor(QPalette.ColorRole.Link, QColor(pal.accent))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        qpal.setColor(QPalette.ColorGroup.Disabled, role, QColor(pal.faint))
    app.setPalette(qpal)
    app.setStyleSheet(stylesheet(pal))
    return pal


def stylesheet(p: Palette) -> str:
    check_path = _check_image(p)
    return f"""
* {{ outline: 0; }}
QWidget {{ color: {p.text}; }}
QMainWindow, QDialog {{ background: {p.bg}; }}
QToolTip {{ background: {p.surface3}; color: {p.text}; border: 1px solid {p.border_strong};
            padding: 6px 8px; border-radius: 6px; }}

#HeaderBar {{ background: {p.surface}; border-bottom: 1px solid {p.border}; }}
#AppTitle {{ font-size: 15px; font-weight: 600; }}
#FileTitle {{ font-size: 13px; color: {p.text}; }}
#FileSubtitle {{ color: {p.muted}; font-size: 12px; }}
#DirtyDot {{ color: {p.accent}; font-size: 16px; }}

#Card, QFrame#Card {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 10px; }}
#CardTitle {{ font-weight: 600; font-size: 12px; color: {p.muted}; letter-spacing: 0.3px; }}
#SectionLabel {{ color: {p.muted}; font-size: 12px; }}
#Muted {{ color: {p.muted}; }}
#Faint {{ color: {p.faint}; }}
#ValueLabel {{ color: {p.text}; }}
#EmptyState {{ color: {p.muted}; font-size: 14px; }}
#EmptyTitle {{ color: {p.text}; font-size: 18px; font-weight: 600; }}

QPushButton, QToolButton {{
    background: {p.surface2}; border: 1px solid {p.border}; border-radius: 7px;
    padding: 6px 12px; color: {p.text};
}}
QPushButton:hover, QToolButton:hover {{ background: {p.surface3}; border-color: {p.border_strong}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {p.border}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {p.faint}; background: {p.surface}; border-color: {p.border}; }}
QPushButton:checked, QToolButton:checked {{ background: {p.accent_soft}; border-color: {p.accent}; }}
QPushButton#Primary {{ background: {p.accent}; color: {p.accent_text}; border: 1px solid {p.accent}; font-weight: 600; }}
QPushButton#Primary:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
QPushButton#Primary:disabled {{ background: {p.surface2}; color: {p.faint}; border-color: {p.border}; }}
QPushButton#Danger {{ color: {p.danger}; }}
QToolButton#Icon, QPushButton#Icon {{ background: transparent; border: 1px solid transparent; padding: 5px; border-radius: 7px; }}
QToolButton#Icon:hover, QPushButton#Icon:hover {{ background: {p.surface3}; border-color: {p.border}; }}
QToolButton#Icon:disabled {{ background: transparent; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}

QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {p.surface2}; border: 1px solid {p.border}; border-radius: 7px;
    padding: 6px 9px; selection-background-color: {p.accent}; selection-color: {p.accent_text};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{ border: 1px solid {p.accent}; }}
QComboBox::drop-down {{ border: 0; width: 22px; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: 0; }}
QComboBox QAbstractItemView {{ background: {p.surface}; border: 1px solid {p.border_strong};
    selection-background-color: {p.accent_soft}; selection-color: {p.text}; padding: 4px; }}

QTabWidget::pane {{ border: 0; top: -1px; }}
QTabBar {{ qproperty-drawBase: 0; }}
QTabBar::tab {{ background: transparent; color: {p.muted}; padding: 8px 14px; margin-right: 2px;
    border: 0; border-bottom: 2px solid transparent; font-weight: 500; }}
QTabBar::tab:hover {{ color: {p.text}; }}
QTabBar::tab:selected {{ color: {p.text}; border-bottom: 2px solid {p.accent}; }}

QScrollArea {{ border: 0; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {p.border_strong}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {p.faint}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {p.border_strong}; border-radius: 3px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QTreeWidget, QTreeView, QListWidget, QTableWidget, QTableView {{
    background: {p.surface}; border: 1px solid {p.border}; border-radius: 8px; padding: 4px;
    alternate-background-color: {p.surface2};
}}
QTreeView::item, QListWidget::item {{ padding: 4px 2px; border-radius: 5px; }}
QTreeView::item:hover, QListWidget::item:hover {{ background: {p.surface3}; }}
QTreeView::item:selected, QListWidget::item:selected, QTableView::item:selected {{
    background: {p.accent_soft}; color: {p.text}; }}
QHeaderView::section {{ background: {p.surface}; color: {p.muted}; border: 0;
    border-bottom: 1px solid {p.border}; padding: 6px 8px; font-weight: 600; }}
QTableView {{ gridline-color: {p.border}; }}

QSlider::groove:horizontal {{ height: 4px; background: {p.border_strong}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {p.accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {p.text}; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}
QSlider::handle:horizontal:hover {{ background: {p.accent_hover}; }}

QProgressBar {{ background: {p.surface2}; border: 0; border-radius: 3px; height: 6px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {p.accent}; border-radius: 3px; }}

QMenu {{ background: {p.surface}; border: 1px solid {p.border_strong}; border-radius: 8px; padding: 5px; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 5px; }}
QMenu::item:selected {{ background: {p.surface3}; }}
QMenu::item:disabled {{ color: {p.faint}; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: 4px 6px; }}

QStatusBar {{ background: {p.surface}; border-top: 1px solid {p.border}; color: {p.muted}; }}
QStatusBar::item {{ border: 0; }}
QStatusBar QLabel {{ padding: 2px 10px; }}
QSplitter::handle {{ background: {p.bg}; }}
QSplitter::handle:horizontal {{ width: 6px; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator, QTreeView::indicator {{ width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid {p.border_strong}; background: {p.surface2}; }}
QCheckBox::indicator:checked, QTreeView::indicator:checked {{ background: {p.accent}; border-color: {p.accent};
    image: url("{check_path}"); }}
QGroupBox {{ border: 1px solid {p.border}; border-radius: 8px; margin-top: 14px; padding: 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {p.muted}; }}

#Banner {{ border-radius: 8px; padding: 2px; }}
#Banner[kind="ok"] {{ background: transparent; border: 0; }}
#Banner[kind="warn"] {{ background: {_mix(p.warning, p.surface, 0.14)}; border: 1px solid {_mix(p.warning, p.surface, 0.45)}; }}
#Banner[kind="error"] {{ background: {_mix(p.danger, p.surface, 0.14)}; border: 1px solid {_mix(p.danger, p.surface, 0.45)}; }}
#Banner[kind="info"] {{ background: {p.accent_soft}; border: 1px solid {_mix(p.accent, p.surface, 0.45)}; }}

#Chip {{ background: {p.chip}; border: 1px solid {_mix(p.accent, p.chip, 0.25)}; border-radius: 13px; }}
#Chip[unknown="true"] {{ background: transparent; border: 1px dashed {p.faint}; }}
#ChipText {{ color: {p.chip_text}; }}
#Chip[unknown="true"] #ChipText {{ color: {p.muted}; }}
#Chip[profile="true"] {{ background: transparent; border: 1px solid {_mix(p.accent, p.chip, 0.45)}; }}
#Chip[profile="true"] #ChipText {{ color: {p.muted}; font-style: italic; }}
#Chip[skipped="true"] {{ background: transparent; border: 1px dashed {p.faint}; }}
#Chip[skipped="true"] #ChipText {{ color: {p.faint}; text-decoration: line-through; }}
#Chip[person="true"] {{ border: 1px solid {p.accent}; }}
#ChipBadge {{ color: {p.genre}; font-size: 10px; font-weight: 700; }}
QToolButton#ChipClose {{ background: transparent; border: 0; padding: 0; border-radius: 9px; }}
QToolButton#ChipClose:hover {{ background: {_mix(p.text, p.chip, 0.15)}; }}

#VideoFrame {{ background: {p.video_bg}; border-radius: 10px; border: 1px solid {p.border}; }}
#TimeLabel {{ font-family: "Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace; color: {p.muted}; }}
#FrameThumb {{ background: {p.video_bg}; color: #c3c9d4; border: 2px solid transparent; border-radius: 6px; padding: 0; }}
#FrameThumb:hover {{ border-color: {p.accent}; }}
#FrameTime {{ color: {p.faint}; font-size: 11px; }}
#ImageBox {{ background: {p.surface2}; border: 1px dashed {p.border_strong}; border-radius: 8px; color: {p.faint}; }}
#ImageBox[filled="true"] {{ background: {p.video_bg}; border: 1px solid {p.border}; }}
#DropOverlay {{ background: {_mix(p.accent, p.bg, 0.12)}; border: 2px dashed {p.accent}; border-radius: 12px; }}
"""


def _mix(color: str, base: str, amount: float) -> str:
    """Blend `color` into `base` (amount 0..1 of color)."""
    a, b = QColor(color), QColor(base)
    r = round(a.red() * amount + b.red() * (1 - amount))
    g = round(a.green() * amount + b.green() * (1 - amount))
    bl = round(a.blue() * amount + b.blue() * (1 - amount))
    return QColor(r, g, bl).name()


# --------------------------------------------------------------------------
# Line icons (24 × 24, drawn with the current text colour)
# --------------------------------------------------------------------------

_ICONS = {
    "open": '<path d="M3 7.5a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "save": '<path d="M5 4h11l3 3v12a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1z"/><path d="M8 4v5h7V4"/><rect x="8" y="13" width="8" height="5" rx="1"/>',
    "play": '<path d="M8 5.5v13a.6.6 0 0 0 .9.5l10.4-6.5a.6.6 0 0 0 0-1L8.9 5a.6.6 0 0 0-.9.5z" fill="currentColor"/>',
    "pause": '<rect x="6.5" y="5" width="3.6" height="14" rx="1" fill="currentColor"/><rect x="13.9" y="5" width="3.6" height="14" rx="1" fill="currentColor"/>',
    "frame_prev": '<path d="M6 5v14"/><path d="M18 6.5v11a.5.5 0 0 1-.8.4l-8-5.5a.5.5 0 0 1 0-.8l8-5.5a.5.5 0 0 1 .8.4z"/>',
    "frame_next": '<path d="M18 5v14"/><path d="M6 6.5v11a.5.5 0 0 0 .8.4l8-5.5a.5.5 0 0 0 0-.8l-8-5.5a.5.5 0 0 0-.8.4z"/>',
    "volume": '<path d="M4 9.5h3l5-4v13l-5-4H4z"/><path d="M15.5 9a4 4 0 0 1 0 6"/><path d="M18 6.5a7.5 7.5 0 0 1 0 11"/>',
    "mute": '<path d="M4 9.5h3l5-4v13l-5-4H4z"/><path d="M16 9.5l5 5M21 9.5l-5 5"/>',
    "camera": '<path d="M4 8a2 2 0 0 1 2-2h2l1.5-2h5L16 6h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/><circle cx="12" cy="12.5" r="3.5"/>',
    "image": '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><circle cx="9" cy="9.5" r="1.6"/><path d="M4 17l5-5 4 4 2.5-2.5L20 18"/>',
    "tag": '<path d="M3.5 12.2V5a1.5 1.5 0 0 1 1.5-1.5h7.2a1.5 1.5 0 0 1 1.1.4l7.3 7.3a1.5 1.5 0 0 1 0 2.1l-7.2 7.2a1.5 1.5 0 0 1-2.1 0L3.9 13.3a1.5 1.5 0 0 1-.4-1.1z"/><circle cx="8.3" cy="8.3" r="1.5"/>',
    "info": '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5"/><circle cx="12" cy="7.8" r=".6" fill="currentColor"/>',
    "more": '<circle cx="5.5" cy="12" r="1.3" fill="currentColor"/><circle cx="12" cy="12" r="1.3" fill="currentColor"/><circle cx="18.5" cy="12" r="1.3" fill="currentColor"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "close": '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "alert": '<path d="M12 4l9 15.5H3z"/><path d="M12 10v4.5"/><circle cx="12" cy="17" r=".6" fill="currentColor"/>',
    "sync": '<path d="M19.5 9A8 8 0 0 0 5.6 7.4L4 9"/><path d="M4 4.5V9h4.5"/><path d="M4.5 15a8 8 0 0 0 13.9 1.6L20 15"/><path d="M20 19.5V15h-4.5"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6"/><path d="M15 15l5 5"/>',
    "trash": '<path d="M4.5 7h15"/><path d="M9.5 7V5h5v2"/><path d="M6.5 7l1 12.5h9l1-12.5"/>',
    "folder_plus": '<path d="M3 7.5a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 11v5M9.5 13.5h5"/>',
    "database": '<ellipse cx="12" cy="6" rx="7.5" ry="2.8"/><path d="M4.5 6v12c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8V6"/><path d="M4.5 12c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8"/>',
    "settings": '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
    "chev_left": '<path d="M14.5 6l-6 6 6 6"/>',
    "chev_right": '<path d="M9.5 6l6 6-6 6"/>',
    "chev_down": '<path d="M6 9.5l6 6 6-6"/>',
    "film": '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M7.5 4.5v15M16.5 4.5v15M3.5 9h4M3.5 15h4M16.5 9h4M16.5 15h4"/>',
    "revert": '<path d="M5 8h9.5a5 5 0 0 1 0 10H8"/><path d="M8.5 4.5L5 8l3.5 3.5"/>',
    "copy": '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
    "folder": '<path d="M3 7.5a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "edit": '<path d="M5 19l1-4 10-10 3 3-10 10z"/><path d="M14 7l3 3"/>',
    "merge": '<path d="M7 4v5a4 4 0 0 0 4 4h6"/><path d="M14 9.5l3.5 3.5-3.5 3.5"/><path d="M7 20v-4"/>',
    "upload": '<path d="M12 16V4.5M7.5 9L12 4.5 16.5 9"/><path d="M5 15v3.5A1.5 1.5 0 0 0 6.5 20h11a1.5 1.5 0 0 0 1.5-1.5V15"/>',
    "download": '<path d="M12 4.5V16M7.5 11.5L12 16l4.5-4.5"/><path d="M5 15v3.5A1.5 1.5 0 0 0 6.5 20h11a1.5 1.5 0 0 0 1.5-1.5V15"/>',
    "star": '<path d="M12 3.8l2.5 5.2 5.7.8-4.1 4 1 5.7-5.1-2.7-5.1 2.7 1-5.7-4.1-4 5.7-.8z"/>',
    "star_fill": '<path d="M12 3.8l2.5 5.2 5.7.8-4.1 4 1 5.7-5.1-2.7-5.1 2.7 1-5.7-4.1-4 5.7-.8z" fill="currentColor"/>',
    "library": '<rect x="3.5" y="4" width="4" height="16" rx="1"/><rect x="9.5" y="4" width="4" height="16" rx="1"/><path d="M16 5.2l3.8-1 3 15.2-3.8 1z"/>',
    "chapters": '<path d="M5 6h14M5 12h14M5 18h9"/><circle cx="3" cy="6" r=".6" fill="currentColor"/><circle cx="3" cy="12" r=".6" fill="currentColor"/><circle cx="3" cy="18" r=".6" fill="currentColor"/>',
    "sparkle": '<path d="M12 3.5l1.8 5.2 5.2 1.8-5.2 1.8L12 17.5l-1.8-5.2L5 10.5l5.2-1.8z"/><path d="M18.5 16l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7z"/>',
    "person": '<circle cx="12" cy="8.5" r="3.8"/><path d="M4.5 20c.8-3.8 3.8-6 7.5-6s6.7 2.2 7.5 6"/>',
    "shield": '<path d="M12 3.5l7 2.8v5.2c0 4.3-3 7.6-7 9-4-1.4-7-4.7-7-9V6.3z"/><path d="M8.8 12.2l2.3 2.3 4.3-4.6"/>',
}


def icon(name: str, color: str | None = None, size: int = 20) -> QIcon:
    color = color or _current.text
    body = _ICONS.get(name, _ICONS["info"])
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
           f'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" color="{color}">'
           f'{body.replace("currentColor", color)}</svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    result = QIcon()
    ratio = 2
    for logical in (size, 16, 24):
        pix = QPixmap(QSize(logical * ratio, logical * ratio))
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter)
        painter.end()
        pix.setDevicePixelRatio(ratio)
        result.addPixmap(pix)
    return result


def _check_image(p: Palette) -> str:
    """Stylesheets need a file for the tick inside checked boxes; draw one into the app folder."""
    from ..paths import app_data_dir
    folder = app_data_dir() / "ui-cache"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"check-{p.accent_text.lstrip('#')}.png"
    if not target.exists():
        pix = icon("check", p.accent_text, 16).pixmap(QSize(16, 16), 2.0)
        pix.save(str(target), "PNG")
    return target.as_posix()


# --------------------------------------------------------------------------
# App icon: the rune ᚱ (raidō) in a rounded tile
# --------------------------------------------------------------------------

def rune_pixmap(size: int) -> QPixmap:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    rect = QRectF(size * 0.04, size * 0.04, size * 0.92, size * 0.92)
    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0.0, QColor("#1d3a5c"))
    gradient.setColorAt(1.0, QColor("#0c1622"))
    path = QPainterPath()
    path.addRoundedRect(rect, size * 0.22, size * 0.22)
    painter.fillPath(path, gradient)
    pen = QPen(QColor("#5fd3ff"))
    pen.setWidthF(max(1.5, size * 0.085))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    s = size
    stem_x = s * 0.36
    rune = QPainterPath()
    rune.moveTo(QPointF(stem_x, s * 0.80))
    rune.lineTo(QPointF(stem_x, s * 0.20))
    rune.lineTo(QPointF(s * 0.64, s * 0.36))
    rune.lineTo(QPointF(stem_x, s * 0.50))
    rune.lineTo(QPointF(s * 0.66, s * 0.80))
    painter.drawPath(rune)
    painter.end()
    return pix


def app_icon() -> QIcon:
    result = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        result.addPixmap(rune_pixmap(size))
    return result
