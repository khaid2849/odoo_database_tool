"""Bottom panel shared by all tabs: step text, progress bar, Cancel and the live log."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

MAX_LINES = 5000
FLUSH_MS = 100


class LogPanel(QWidget):
    cancel_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pending: list[str] = []

        self.step_label = QLabel("")
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(True)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("dangerBtn")
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(MAX_LINES)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.log.setPlaceholderText("Log")

        top = QHBoxLayout()
        top.addWidget(self.bar, 1)
        top.addWidget(self.step_label, 2)
        top.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(self.log, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(FLUSH_MS)
        self._timer.timeout.connect(self.flush)
        self._timer.start()
        self.set_running(False)

    def append(self, text: str) -> None:
        self._pending.append(text)

    def flush(self) -> None:
        if not self._pending:
            return
        lines, self._pending = self._pending, []
        # Only the last MAX_LINES can survive; skip the rest to keep floods cheap.
        self.log.appendPlainText("\n".join(lines[-MAX_LINES:]))

    def set_progress(self, percent: Optional[int], text: str) -> None:
        if percent is None:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 100)
            self.bar.setValue(max(0, min(100, int(percent))))
        self.step_label.setText(text)

    def set_running(self, running: bool) -> None:
        self.cancel_button.setEnabled(True)
        self.cancel_button.setVisible(running)
        self.bar.setVisible(running)
        self.step_label.setVisible(running)
        if running:
            self.set_progress(None, "Starting…")
        else:
            self.flush()
