"""Application bootstrap."""
from __future__ import annotations

from PySide6.QtWidgets import QApplication

from .common import APP_NAME, ORG_NAME, make_icon
from .settings import AppSettings
from .ui.main_window import MainWindow, apply_app_theme


def run(argv: list[str]) -> int:
    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setWindowIcon(make_icon())
    app.setStyle("Fusion")
    settings = AppSettings()
    apply_app_theme(settings.theme)
    win = MainWindow(settings=settings)
    win.show()
    return app.exec()
