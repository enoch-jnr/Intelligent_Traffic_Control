"""The only place colours, fonts and sizes live.

Data widgets never contain a colour literal. `stylesheet()` substitutes tokens
into one Qt stylesheet; switching theme rebuilds that stylesheet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

UI_FONT = '"Segoe UI", "Inter", sans-serif'
MONO_FONT = '"Cascadia Mono", "Consolas", monospace'

DARK_TOKENS: Mapping[str, str] = MappingProxyType(
    {
        "bg-chrome": "#1C323C",
        "bg-base": "#222B38",
        "bg-panel": "#2A3846",
        "bg-panel-raised": "#32424E",
        "bg-field": "#1A202F",
        "border": "#425560",
        "text-primary": "#D3DDE6",
        "text-secondary": "#A6B2BF",
        "text-muted": "#6B7D8B",
        "accent": "#3E6787",
        "accent-text": "#7FB3D5",
        "selection": "#1A2A47",
        "status-ok": "#5ACBA3",
        "status-warn": "#E5B84A",
        "status-critical": "#F06A6E",
        "status-critical-text": "#FF8A8D",
        "status-unknown": "#A6B2BF",
        "approach-main-a": "#5CC8D8",
        "approach-main-b": "#7AA7F0",
        "approach-sec": "#A78BFA",
        "approach-main-b-label": "#8FB4FF",
        "approach-sec-label": "#C4B5FD",
        "focus-ring": "#93C5FD",
        "white": "#FFFFFF",
        "overlay": "rgba(9, 14, 20, 215)",
        "overlay-text": "#E8EEF4",
        "banner-bg": "#E5B84A",
        "banner-text": "#14212E",
        "badge-bg": "#3E6787",
        "badge-text": "#FFFFFF",
        "tile-dim": "#B3090E14",
        "overlay-panel": "#D9162330",
        "text-outline": "#0B1118",
    }
)

LIGHT_TOKENS: Mapping[str, str] = MappingProxyType(
    {
        "bg-chrome": "#DDE5EC",
        "bg-base": "#EEF2F5",
        "bg-panel": "#FFFFFF",
        "bg-panel-raised": "#E6EDF3",
        "bg-field": "#F4F7FA",
        "border": "#CBD5DF",
        "text-primary": "#14212E",
        "text-secondary": "#46576A",
        "text-muted": "#7A8896",
        "accent": "#2F5F84",
        "accent-text": "#245C86",
        "selection": "#DCE7F2",
        "status-ok": "#0F6B4F",
        "status-warn": "#7A5A00",
        "status-critical": "#B3261E",
        "status-critical-text": "#A11D18",
        "status-unknown": "#46576A",
        "approach-main-a": "#0E6B7A",
        "approach-main-b": "#245C86",
        "approach-sec": "#5B3FBF",
        "approach-main-b-label": "#245C86",
        "approach-sec-label": "#5B3FBF",
        "focus-ring": "#2563A8",
        "white": "#FFFFFF",
        "overlay": "rgba(20, 33, 46, 225)",
        "overlay-text": "#F4F8FB",
        "banner-bg": "#7A5A00",
        "banner-text": "#FFFFFF",
        "badge-bg": "#245C86",
        "badge-text": "#FFFFFF",
        "tile-dim": "#B30B1622",
        "overlay-panel": "#D91C2933",
        "text-outline": "#0B1118",
    }
)


@dataclass(frozen=True)
class Theme:
    name: str
    tokens: Mapping[str, str]


DARK = Theme("dark", DARK_TOKENS)
LIGHT = Theme("light", LIGHT_TOKENS)
THEMES = {"dark": DARK, "light": LIGHT}

_TOKEN_RE = re.compile(r"\{\{([a-z0-9-]+)\}\}")

_STYLESHEET = """
QWidget {
    background-color: {{bg-base}};
    color: {{text-primary}};
    font-family: {{ui-font}};
    font-size: 13px;
}
QMainWindow, QDialog { background-color: {{bg-base}}; }
QLabel#modeBadge {
    background-color: {{badge-bg}};
    color: {{badge-text}};
    border-radius: 4px;
    padding: 3px 9px;
    font-size: 11px;
    font-weight: 700;
}
QLabel#modeBadge[tone="simulation"] {
    background-color: {{status-warn}};
    color: {{banner-text}};
}
QLabel#modeBadge[tone="live"] {
    background-color: {{status-ok}};
    color: {{bg-field}};
}
QMenuBar {
    background-color: {{bg-chrome}};
    color: {{text-secondary}};
    padding: 0 4px;
    border-bottom: 1px solid {{border}};
}
QMenuBar::item { padding: 3px 8px; background: transparent; }
QMenuBar::item:selected { background: {{bg-panel-raised}}; color: {{text-primary}}; }
QMenu {
    background-color: {{bg-panel}};
    color: {{text-primary}};
    border: 1px solid {{border}};
    padding: 4px;
}
QMenu::item { padding: 6px 26px 6px 18px; border-radius: 4px; }
QMenu::item:selected { background: {{selection}}; color: {{accent-text}}; }
QMenu::separator { height: 1px; background: {{border}}; margin: 4px 8px; }
QStatusBar { background-color: {{bg-base}}; border-top: 1px solid {{border}}; }
QStatusBar::item { border: none; }
QStatusBar QPushButton, QStatusBar .QPushButton { min-height: 26px; max-height: 26px; }
QLabel#session {
    font-family: {{mono-font}};
    color: {{text-secondary}};
    padding: 0 8px;
}
QLabel#helper { color: {{text-secondary}}; font-size: 11px; }
QLabel#largeReadout {
    color: {{text-primary}};
    font-size: 20px;
    font-weight: 600;
}
QLabel#largeReadout[tone="ok"] { color: {{status-ok}}; }
QLabel#largeReadout[tone="warn"] { color: {{status-warn}}; }
QLabel#largeReadout[tone="critical"] { color: {{status-critical-text}}; }
QLabel#mono {
    font-family: {{mono-font}};
    color: {{text-secondary}};
    font-size: 11px;
}
QFrame#chip {
    background-color: {{bg-panel}};
    border: 1px solid {{border}};
    border-radius: 10px;
}
QLabel#chipText { color: {{text-secondary}}; font-size: 11px; }
QLabel#chipIcon { font-size: 12px; font-weight: 700; color: {{status-unknown}}; }
QLabel#chipIcon[tone="ok"] { color: {{status-ok}}; }
QLabel#chipIcon[tone="warn"] { color: {{status-warn}}; }
QLabel#chipIcon[tone="critical"] { color: {{status-critical}}; }
QLabel#chipIcon[tone="unknown"] { color: {{status-unknown}}; }
QLabel#chipIcon[tone="info"] { color: {{accent-text}}; }
QPushButton {
    background-color: {{bg-panel-raised}};
    color: {{text-primary}};
    border: 1px solid {{border}};
    border-radius: 6px;
    min-height: 40px;
    padding: 0 14px;
}
QPushButton:hover { border-color: {{accent-text}}; }
QPushButton[compact="true"] { min-height: 24px; padding: 0 8px; }
QPushButton:pressed { background-color: {{selection}}; }
QPushButton:disabled { color: {{text-muted}}; border-color: {{border}}; }
QPushButton:focus { border: 2px solid {{focus-ring}}; }
QPushButton[variant="primary"] {
    background-color: {{accent}};
    color: {{white}};
    border: 1px solid {{accent}};
    font-weight: 600;
}
QPushButton[variant="primary"]:hover { border-color: {{focus-ring}}; }
QPushButton[variant="destructive"] {
    background-color: transparent;
    color: {{status-critical-text}};
    border: 1px solid {{status-critical}};
}
QPushButton[variant="caution"] {
    background-color: transparent;
    color: {{status-warn}};
    border: 1px solid {{status-warn}};
}
QToolButton#sectionHeader {
    background-color: {{bg-panel}};
    color: {{text-primary}};
    border: none;
    border-top: 1px solid {{border}};
    text-align: left;
    padding: 8px 10px;
    font-size: 15px;
    font-weight: 600;
}
QToolButton#sectionHeader:hover { background-color: {{bg-panel-raised}}; }
QToolButton#sectionHeader:focus { border: 2px solid {{focus-ring}}; }
QWidget#sectionBody { background-color: {{bg-base}}; }
QFrame#card {
    background-color: {{bg-panel}};
    border: 1px solid {{border}};
    border-radius: 10px;
}
QFrame#tile {
    background-color: {{bg-field}};
    border: 1px solid {{border}};
}
QLabel#tileName {
    background-color: {{bg-panel-raised}};
    color: {{text-primary}};
    padding: 4px 8px;
    font-size: 11px;
    font-weight: 600;
}
QLabel#tileEmpty { color: {{text-secondary}}; font-size: 13px; }
QScrollArea { border: none; background-color: {{bg-base}}; }
QScrollArea > QWidget > QWidget { background-color: {{bg-base}}; }
QSplitter::handle { background-color: {{border}}; }
QSplitter::handle:horizontal { width: 2px; }
QSplitter::handle:vertical { height: 2px; }
QPlainTextEdit, QTextEdit {
    background-color: {{bg-field}};
    color: {{text-primary}};
    border: 1px solid {{border}};
    border-radius: 4px;
    font-family: {{mono-font}};
    font-size: 12px;
    selection-background-color: {{selection}};
    selection-color: {{accent-text}};
}
QLineEdit {
    background-color: {{bg-field}};
    color: {{text-primary}};
    border: 1px solid {{border}};
    border-radius: 6px;
    min-height: 32px;
    padding: 0 8px;
    selection-background-color: {{selection}};
    selection-color: {{accent-text}};
}
QLineEdit:focus { border: 2px solid {{focus-ring}}; }
QTableWidget, QTreeWidget {
    background-color: {{bg-field}};
    alternate-background-color: {{bg-panel}};
    color: {{text-primary}};
    border: 1px solid {{border}};
    gridline-color: {{border}};
    selection-background-color: {{selection}};
    selection-color: {{accent-text}};
}
QTableWidget::item:selected, QTreeWidget::item:selected {
    background-color: {{selection}};
    color: {{accent-text}};
}
QHeaderView::section {
    background-color: {{bg-panel-raised}};
    color: {{text-secondary}};
    border: none;
    border-right: 1px solid {{border}};
    padding: 5px 8px;
    font-size: 11px;
    font-weight: 600;
}
QTabWidget::pane { border: 1px solid {{border}}; background: {{bg-base}}; }
QTabBar::tab {
    background: {{bg-chrome}};
    color: {{text-secondary}};
    padding: 4px 10px;
    border: 1px solid {{border}};
    border-bottom: none;
}
QTabBar::tab:selected { background: {{bg-base}}; color: {{accent-text}}; }
QCheckBox { color: {{text-primary}}; spacing: 6px; }
QComboBox {
    background-color: {{bg-field}};
    color: {{text-primary}};
    border: 1px solid {{border}};
    border-radius: 6px;
    min-height: 32px;
    padding: 0 8px;
}
QToolTip {
    background-color: {{bg-panel-raised}};
    color: {{text-primary}};
    border: 1px solid {{border}};
    padding: 4px;
}
QScrollBar:vertical { background: {{bg-base}}; width: 10px; margin: 0; }
QScrollBar::handle:vertical {
    background: {{border}};
    min-height: 24px;
    border-radius: 5px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: {{bg-base}}; height: 10px; margin: 0; }
QScrollBar::handle:horizontal {
    background: {{border}};
    min-width: 24px;
    border-radius: 5px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
"""


def stylesheet(theme: Theme) -> str:
    values = dict(theme.tokens)
    values["ui-font"] = UI_FONT
    values["mono-font"] = MONO_FONT
    return _TOKEN_RE.sub(lambda match: values[match.group(1)], _STYLESHEET)


def token(theme: Theme, name: str) -> str:
    return theme.tokens[name]
