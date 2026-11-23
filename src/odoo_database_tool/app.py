"""Application bootstrap (placeholder window until the UI tasks land)."""
from __future__ import annotations

from PySide6.QtWidgets import QApplication, QMainWindow

from .common import APP_NAME, ORG_NAME, make_icon
from .theme import apply_theme, stylesheet


def run(argv: list[str]) -> int:
    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setWindowIcon(make_icon())
    app.setStyle("Fusion")
    app.setStyleSheet(stylesheet(apply_theme("system")))
    win = QMainWindow()
    win.setWindowTitle(APP_NAME)
    win.resize(900, 600)
    win.show()
    return app.exec()
