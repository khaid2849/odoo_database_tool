"""Main window: four tabs plus the shared log panel; runs one background job at a time."""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QApplication, QMainWindow, QSplitter, QTabWidget, QWidget

from ..commands import CommandBuilder
from ..common import APP_NAME
from ..jobs import JobContext
from ..settings import AppSettings
from ..store import ConnectionStore, Secrets
from ..theme import apply_theme, stylesheet
from ..tools import detect_ssh
from . import dialogs
from .connections_tab import ConnectionsTab
from .dump_tab import DumpTab
from .log_panel import LogPanel
from .restore_tab import RestoreTab
from .settings_tab import SettingsTab
from .worker import Worker


def apply_app_theme(mode: str) -> None:
    """Apply palette and stylesheet for a theme mode to the running app."""
    QApplication.instance().setStyleSheet(stylesheet(apply_theme(mode)))


class MainWindow(QMainWindow):
    job_running = Signal(bool)
    connections_changed = Signal()

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
        self._quit_when_done = False
        # Injectable seam: True to cancel the running job and quit.
        self.confirm_quit = lambda: dialogs.confirm(
            self, "Quit", "An operation is running. Cancel it and quit?", danger=True)

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

    # --- tab factories ---------------------------------------------------------------

    def _make_dump_tab(self) -> QWidget:
        self.dump_tab = DumpTab(self.store, self)
        return self.dump_tab

    def _make_restore_tab(self) -> QWidget:
        self.restore_tab = RestoreTab(self.store, self)
        return self.restore_tab

    def _make_connections_tab(self) -> QWidget:
        self.connections_tab = ConnectionsTab(self.store, self.secrets, self)
        self.connections_tab.connections_changed.connect(self.connections_changed)
        return self.connections_tab

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
        if self._quit_when_done:
            self.close()
        elif on_done:
            on_done(result)

    def _job_failed(self, message: str) -> None:
        _, on_failed = self._finish_job()
        self.log_panel.append(f"Failed: {message}")
        self.log_panel.flush()
        if self._quit_when_done:
            self.close()
        elif on_failed:
            on_failed(message)

    def closeEvent(self, event) -> None:
        if self._worker is not None:
            # Never destroy a running QThread: cancel, then close once the worker has finished
            # (follow-up callbacks are skipped so no dialog or next job starts meanwhile).
            event.ignore()
            if not self._quit_when_done and self.confirm_quit():
                self._quit_when_done = True
                self._cancel_job()
            return
        self.settings.window_geometry = self.saveGeometry()
        super().closeEvent(event)
