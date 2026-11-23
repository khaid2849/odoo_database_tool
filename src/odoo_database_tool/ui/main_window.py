"""Main window: four tabs plus the shared log panel; runs one background job at a time."""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QLabel, QMainWindow, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from ..commands import CommandBuilder
from ..common import APP_NAME
from ..jobs import JobContext
from ..settings import AppSettings
from ..store import ConnectionStore, Secrets
from ..theme import apply_theme, stylesheet
from ..tools import detect_ssh
from .log_panel import LogPanel
from .settings_tab import SettingsTab
from .worker import Worker


def apply_app_theme(mode: str) -> None:
    """Apply palette and stylesheet for a theme mode to the running app."""
    QApplication.instance().setStyleSheet(stylesheet(apply_theme(mode)))


def _placeholder(text: str) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    label = QLabel(text)
    label.setAlignment(Qt.AlignCenter)
    layout.addWidget(label)
    return page


class MainWindow(QMainWindow):
    job_running = Signal(bool)

    def __init__(self, settings: Optional[AppSettings] = None,
                 store: Optional[ConnectionStore] = None,
                 secrets: Optional[Secrets] = None):
        super().__init__()
        self.settings = settings or AppSettings()
        self.store = store or ConnectionStore()
        self.secrets = secrets or Secrets()
        self._worker: Optional[Worker] = None
        self._on_done: Optional[Callable[[object], None]] = None
        self._on_failed: Optional[Callable[[str], None]] = None

        self.setWindowTitle(APP_NAME)
        self.resize(1000, 700)
        geometry = self.settings.window_geometry
        if not geometry.isEmpty():
            self.restoreGeometry(geometry)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._make_dump_tab(), "Dump")
        self.tabs.addTab(self._make_restore_tab(), "Restore")
        self.tabs.addTab(self._make_connections_tab(), "Connections")
        self.settings_tab = self._make_settings_tab()
        self.tabs.addTab(self.settings_tab, "Settings")

        self.log_panel = LogPanel()
        self.log_panel.cancel_requested.connect(self._cancel_job)
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.tabs)
        splitter.addWidget(self.log_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

    # --- tab factories (replaced by the Dump / Restore / Connections tasks) ------------

    def _make_dump_tab(self) -> QWidget:
        return _placeholder("Dump")

    def _make_restore_tab(self) -> QWidget:
        return _placeholder("Restore")

    def _make_connections_tab(self) -> QWidget:
        return _placeholder("Connections")

    def _make_settings_tab(self) -> QWidget:
        tab = SettingsTab(self.settings, self.secrets)
        tab.theme_changed.connect(apply_app_theme)
        return tab

    # --- helpers for tabs ------------------------------------------------------------

    def builder(self) -> CommandBuilder:
        """A fresh command builder using the current tool settings."""
        return CommandBuilder(self.settings, self.secrets,
                              detect_ssh(self.settings.ssh_override) or "ssh")

    @property
    def job_active(self) -> bool:
        return self._worker is not None

    # --- background job ----------------------------------------------------------------

    def run_job(self, fn: Callable[[JobContext], object],
                on_done: Callable[[object], None],
                on_failed: Optional[Callable[[str], None]] = None) -> bool:
        """Run fn in a worker thread; False (and nothing started) when a job is running."""
        if self._worker is not None:
            return False
        worker = Worker(fn)
        worker.log.connect(self.log_panel.append)
        worker.progress.connect(self.log_panel.set_progress)
        worker.finished_ok.connect(self._job_succeeded)
        worker.failed.connect(self._job_failed)
        self._worker, self._on_done, self._on_failed = worker, on_done, on_failed
        self.log_panel.set_running(True)
        self.job_running.emit(True)
        worker.start()
        return True

    def _cancel_job(self) -> None:
        if self._worker is not None:
            self.log_panel.cancel_button.setEnabled(False)
            self.log_panel.append("Cancelling…")
            self._worker.cancel()

    def _finish_job(self) -> tuple:
        worker, on_done, on_failed = self._worker, self._on_done, self._on_failed
        worker.wait()  # run() has already emitted its last signal
        self._worker = self._on_done = self._on_failed = None
        self.log_panel.set_running(False)
        self.job_running.emit(False)
        return on_done, on_failed

    def _job_succeeded(self, result: object) -> None:
        on_done, _ = self._finish_job()
        if on_done:
            on_done(result)

    def _job_failed(self, message: str) -> None:
        _, on_failed = self._finish_job()
        self.log_panel.append(f"Failed: {message}")
        self.log_panel.flush()
        if on_failed:
            on_failed(message)

    def closeEvent(self, event) -> None:
        self.settings.window_geometry = self.saveGeometry()
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(10000)
        super().closeEvent(event)
