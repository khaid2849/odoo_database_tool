"""Small modal dialogs; every function takes an optional `prompt(dialog) -> bool` for tests."""
from __future__ import annotations

from typing import Callable, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QLineEdit, QMessageBox, QVBoxLayout, QWidget,
)

Prompt = Optional[Callable[[QDialog], bool]]


def _exec(dialog: QDialog) -> bool:
    return dialog.exec() == QDialog.Accepted


def _base(parent: Optional[QWidget], title: str, message: str) -> tuple:
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    layout = QVBoxLayout(dlg)
    label = QLabel(message)
    label.setWordWrap(True)
    layout.addWidget(label)
    return dlg, layout


def _buttons(dlg: QDialog, ok_text: str = "OK") -> QDialogButtonBox:
    box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    box.button(QDialogButtonBox.Ok).setText(ok_text)
    box.accepted.connect(dlg.accept)
    box.rejected.connect(dlg.reject)
    dlg.layout().addWidget(box)
    return box


def confirm(parent: Optional[QWidget], title: str, message: str, danger: bool = False,
            prompt: Prompt = None) -> bool:
    """OK / Cancel question; danger styles OK red and makes Cancel the default."""
    dlg, _ = _base(parent, title, message)
    box = _buttons(dlg)
    if danger:
        box.button(QDialogButtonBox.Ok).setObjectName("dangerBtn")
        box.button(QDialogButtonBox.Cancel).setDefault(True)
    return (prompt or _exec)(dlg)


def confirm_typed(parent: Optional[QWidget], title: str, message: str, expected: str,
                  prompt: Prompt = None) -> bool:
    """OK stays disabled until the user types `expected` exactly."""
    dlg, layout = _base(parent, title, f"{message}\n\nType {expected} to confirm.")
    edit = QLineEdit()
    edit.setObjectName("typedEdit")
    layout.addWidget(edit)
    box = _buttons(dlg)
    ok = box.button(QDialogButtonBox.Ok)
    ok.setObjectName("dangerBtn")
    ok.setEnabled(False)
    edit.textChanged.connect(lambda text: ok.setEnabled(text == expected))
    return (prompt or _exec)(dlg)


def confirm_host_key(parent: Optional[QWidget], host: str, port: int,
                     fingerprints: Sequence[str], prompt: Prompt = None) -> bool:
    """Show the fingerprints of an unknown SSH host; True when the user trusts them."""
    dlg, layout = _base(parent, "Unknown SSH host",
                        f"The authenticity of {host}:{port} cannot be established. "
                        "Compare these fingerprints with the server admin's, then accept to trust it.")
    keys = QLabel("\n".join(fingerprints))
    keys.setObjectName("fingerprints")
    keys.setWordWrap(True)
    keys.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
    keys.setTextInteractionFlags(Qt.TextSelectableByMouse)
    layout.addWidget(keys)
    _buttons(dlg, "Accept")
    return (prompt or _exec)(dlg)


def warn(parent: Optional[QWidget], title: str, message: str) -> None:
    """Modal warning; tests monkeypatch this."""
    QMessageBox.warning(parent, title, message)
