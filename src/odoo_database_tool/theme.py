"""Light / Dark / System theme.

Uses the Fusion style with our own palettes so the app looks the same on macOS, Windows and Linux.
With Qt ≥ 6.8 the window title bar follows too (macOS, Windows) via styleHints().setColorScheme().
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette

THEMES = ("system", "light", "dark")

_COLORS = {
    False: {  # light
        "window": "#f3f4f6", "base": "#ffffff", "alt": "#f9fafb", "text": "#111827", "muted": "#6b7280",
        "button": "#ffffff", "border": "#d1d5db", "highlight": "#2563eb", "disabled": "#9ca3af",
        "warn_bg": "#fff4e5", "warn_border": "#f5c26b", "warn_text": "#7a4b00",
        "ok_bg": "#e8f7ee", "ok_border": "#86d1a3", "ok_text": "#14532d",
        "bad_bg": "#fdecec", "bad_border": "#f1a3a3", "bad_text": "#7f1d1d",
    },
    True: {  # dark
        "window": "#1f2023", "base": "#2a2b2f", "alt": "#303136", "text": "#e5e7eb", "muted": "#9ca3af",
        "button": "#34353a", "border": "#4b4d55", "highlight": "#3b82f6", "disabled": "#6b7280",
        "warn_bg": "#3a2c12", "warn_border": "#8a6a2a", "warn_text": "#f5d9a8",
        "ok_bg": "#12301f", "ok_border": "#2f7a4d", "ok_text": "#b7ebc9",
        "bad_bg": "#3b1818", "bad_border": "#8a3a3a", "bad_text": "#f5c2c2",
    },
}

_STYLE = """
QGroupBox { font-weight: 600; border: 1px solid @border; border-radius: 8px; margin-top: 10px; padding-top: 6px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
#primaryBtn { font-weight: 600; padding: 7px 14px; border-radius: 8px;
             color: white; background: @highlight; border: none; }
#primaryBtn:hover { background: #1d4ed8; }
#primaryBtn:disabled { background: @disabled; }
#dangerBtn { font-weight: 600; padding: 7px 14px; border-radius: 8px;
             color: white; background: #dc2626; border: none; }
#dangerBtn:hover { background: #b91c1c; }
#dangerBtn:disabled { background: @disabled; }
#resultOk { background: @ok_bg; border: 1px solid @ok_border; border-radius: 8px; color: @ok_text; padding: 8px; }
#resultBad { background: @bad_bg; border: 1px solid @bad_border; border-radius: 8px; color: @bad_text; padding: 8px; }
#muted { color: @muted; font-size: 8pt; }
"""


def system_is_dark() -> bool:
    return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark


def _palette(c: dict) -> QPalette:
    p = QPalette()
    roles = {
        QPalette.Window: c["window"], QPalette.WindowText: c["text"], QPalette.Base: c["base"],
        QPalette.AlternateBase: c["alt"], QPalette.Text: c["text"], QPalette.Button: c["button"],
        QPalette.ButtonText: c["text"], QPalette.ToolTipBase: c["base"], QPalette.ToolTipText: c["text"],
        QPalette.PlaceholderText: c["muted"], QPalette.Highlight: c["highlight"],
        QPalette.HighlightedText: "#ffffff", QPalette.Link: c["highlight"], QPalette.BrightText: "#ffffff",
        QPalette.Mid: c["border"], QPalette.Dark: c["border"], QPalette.Midlight: c["alt"],
        QPalette.Light: c["base"], QPalette.Shadow: "#000000",
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(c["disabled"]))
    return p


def apply_theme(mode: str) -> bool:
    """Apply the theme to the whole app. Returns True if the result is dark."""
    app = QGuiApplication.instance()
    hints = app.styleHints()
    if hasattr(hints, "setColorScheme"):  # Qt 6.8+
        hints.setColorScheme({"light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}
                             .get(mode, Qt.ColorScheme.Unknown))
    dark = mode == "dark" or (mode == "system" and system_is_dark())
    app.setPalette(_palette(_COLORS[dark]))
    return dark


def stylesheet(dark: bool) -> str:
    css = _STYLE
    for key, val in _COLORS[dark].items():
        css = css.replace(f"@{key}", val)
    return css
