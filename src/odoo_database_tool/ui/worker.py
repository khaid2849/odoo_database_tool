"""Runs one core job in a background thread and reports through signals."""
from __future__ import annotations

import traceback
from typing import Callable

from PySide6.QtCore import QThread, Signal

from ..jobs import JobContext, JobError
from ..runner import CancelToken, Redactor


class Worker(QThread):
    log = Signal(str)
    progress = Signal(object, str)  # percent (or None) and step text
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[JobContext], object], parent=None):
        super().__init__(parent)
        self._fn = fn
        self._token = CancelToken()
        self._redactor = Redactor()

    def cancel(self) -> None:
        self._token.cancel()

    def run(self) -> None:
        ctx = JobContext(log=self.log.emit,
                         progress=lambda percent, text: self.progress.emit(percent, text),
                         cancel=self._token, redactor=self._redactor)
        try:
            result = self._fn(ctx)
        except JobError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - nothing may escape the thread
            self.log.emit(self._redactor(traceback.format_exc()))
            self.failed.emit(self._redactor(f"Unexpected error: {type(exc).__name__}: {exc}"))
        else:
            self.finished_ok.emit(result)
