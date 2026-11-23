"""Settings tab: tool paths, addons sets for neutralize, dump folder, jobs, theme."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from ..settings import AppSettings
from ..ssh import openssh_version, supports_askpass
from ..store import Secrets
from ..theme import THEMES
from ..tools import PG_TOOLS, detect_ssh


def clean_addons_dir(raw: str) -> str | None:
    """Stripped absolute path ('~' expanded), or None when it is relative or blank."""
    text = raw.strip()
    if not text:
        return None
    path = Path(text).expanduser()
    return str(path) if path.is_absolute() else None


class SettingsTab(QWidget):
    theme_changed = Signal(str)

    def __init__(self, settings: AppSettings, secrets: Secrets, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.secrets = secrets
        self._detected: dict[str, QLabel] = {}
        self._overrides: dict[str, QLineEdit] = {}

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_tools_group())
        layout.addWidget(self._build_addons_group())
        layout.addWidget(self._build_general_group())
        layout.addStretch(1)
        self.refresh()
        self._load_addons()

    # --- tools -------------------------------------------------------------------

    def _build_tools_group(self) -> QGroupBox:
        box = QGroupBox("Tools")
        grid = QGridLayout(box)
        for row, name in enumerate((*PG_TOOLS, "ssh")):
            detected = QLabel()
            detected.setWordWrap(True)
            edit = QLineEdit(self.settings.ssh_override if name == "ssh"
                             else self.settings.tool_override(name))
            edit.setPlaceholderText("Override path (optional)")
            edit.editingFinished.connect(lambda n=name: self._save_override(n))
            browse = QPushButton("Browse…")
            browse.clicked.connect(lambda _=False, n=name: self._browse_tool(n))
            self._detected[name], self._overrides[name] = detected, edit
            grid.addWidget(QLabel(name), row, 0)
            grid.addWidget(detected, row, 1)
            grid.addWidget(edit, row, 2)
            grid.addWidget(browse, row, 3)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(2, 2)
        last = len(PG_TOOLS) + 1
        self.ssh_status = QLabel()
        self.password_ssh_status = QLabel()
        self.password_ssh_status.setObjectName("muted")
        self.password_ssh_status.setWordWrap(True)
        self.redetect_button = QPushButton("Re-detect")
        self.redetect_button.clicked.connect(self._redetect)
        grid.addWidget(self.ssh_status, last, 0, 1, 3)
        grid.addWidget(self.redetect_button, last, 3)
        grid.addWidget(self.password_ssh_status, last + 1, 0, 1, 4)
        return box

    def _save_override(self, name: str) -> None:
        text = self._overrides[name].text().strip()
        if name == "ssh":
            self.settings.ssh_override = text
        else:
            self.settings.set_tool_override(name, text)
        self.refresh()

    def _browse_tool(self, name: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, f"Select {name}")
        if path:
            self._overrides[name].setText(path)
            self._save_override(name)

    def _redetect(self) -> None:
        self.settings.clear_tool_cache()
        self.refresh()

    def refresh(self) -> None:
        """Re-read detected paths/versions and the SSH status lines."""
        for name in PG_TOOLS:
            info = self.settings.tool(name)
            self._detected[name].setText(f"{info.path}  ({info.version})" if info else "not found")
        ssh_bin = detect_ssh(self.settings.ssh_override)
        version = openssh_version(ssh_bin) if ssh_bin else None
        self._detected["ssh"].setText(ssh_bin or "not found")
        self.ssh_status.setText(
            f"OpenSSH {version[0]}.{version[1]}" if version else "OpenSSH not found")
        if not supports_askpass(version):
            self.password_ssh_status.setText(
                "Password SSH: unavailable, it needs OpenSSH 8.4 or newer (key auth still works).")
        elif not self.secrets.available:
            self.password_ssh_status.setText(
                "Password SSH: unavailable, no OS keychain found (key auth still works).")
        else:
            self.password_ssh_status.setText("Password SSH: available (OpenSSH and OS keychain found).")

    # --- addons sets -------------------------------------------------------------

    def _build_addons_group(self) -> QGroupBox:
        box = QGroupBox("Odoo addons sets (for neutralize)")
        layout = QVBoxLayout(box)
        hint = QLabel("One set per Odoo series; folders are searched for module/data/neutralize.sql.")
        hint.setObjectName("muted")
        layout.addWidget(hint)

        series_row = QHBoxLayout()
        self.series_combo = QComboBox()
        self.series_combo.currentTextChanged.connect(self._show_series)
        self.series_edit = QLineEdit()
        self.series_edit.setPlaceholderText("New series, e.g. 18.0")
        add_series = QPushButton("Add series")
        add_series.clicked.connect(lambda: self.add_series(self.series_edit.text()))
        rm_series = QPushButton("Remove series")
        rm_series.clicked.connect(self._remove_series)
        for w in (self.series_combo, self.series_edit, add_series, rm_series):
            series_row.addWidget(w)
        layout.addLayout(series_row)

        self.dirs_list = QListWidget()
        self.dirs_list.setMaximumHeight(110)
        layout.addWidget(self.dirs_list)
        dir_row = QHBoxLayout()
        self.dir_edit = QLineEdit()
        self.dir_edit.setPlaceholderText("Absolute folder path")
        add_dir = QPushButton("Add folder")
        add_dir.clicked.connect(self._add_typed_dir)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_dir)
        rm_dir = QPushButton("Remove folder")
        rm_dir.clicked.connect(self._remove_selected_dir)
        for w in (self.dir_edit, add_dir, browse, rm_dir):
            dir_row.addWidget(w)
        layout.addLayout(dir_row)
        self.addons_error = QLabel()
        self.addons_error.setObjectName("resultBad")
        self.addons_error.hide()
        layout.addWidget(self.addons_error)
        return box

    def _set_error(self, text: str) -> None:
        self.addons_error.setText(text)
        self.addons_error.setVisible(bool(text))

    def _load_addons(self) -> None:
        self.series_combo.blockSignals(True)
        self.series_combo.clear()
        self.series_combo.addItems(sorted(self.settings.addons_sets))
        self.series_combo.blockSignals(False)
        self._show_series(self.series_combo.currentText())

    def _show_series(self, series: str) -> None:
        self.dirs_list.clear()
        self.dirs_list.addItems(self.settings.addons_sets.get(series, []))

    def add_series(self, name: str) -> bool:
        name = name.strip()
        if not name:
            return False
        sets = self.settings.addons_sets
        sets.setdefault(name, [])
        self.settings.addons_sets = sets
        self.series_edit.clear()
        self._load_addons()
        self.series_combo.setCurrentText(name)
        return True

    def _remove_series(self) -> None:
        sets = self.settings.addons_sets
        if sets.pop(self.series_combo.currentText(), None) is not None:
            self.settings.addons_sets = sets
            self._load_addons()

    def add_addons_dir(self, series: str, raw: str) -> bool:
        """Add a folder to a series; blank entries are ignored, relative ones rejected."""
        if not raw.strip():
            return False
        path = clean_addons_dir(raw)
        if path is None:
            self._set_error(f"'{raw.strip()}' is not an absolute path; use a full folder path.")
            return False
        self._set_error("")
        sets = self.settings.addons_sets
        dirs = sets.setdefault(series, [])
        if path not in dirs:
            dirs.append(path)
        self.settings.addons_sets = sets
        if series == self.series_combo.currentText():
            self._show_series(series)
        return True

    def remove_addons_dir(self, series: str, path: str) -> None:
        sets = self.settings.addons_sets
        if path in sets.get(series, []):
            sets[series].remove(path)
            self.settings.addons_sets = sets
            self._show_series(self.series_combo.currentText())

    def _add_typed_dir(self) -> None:
        series = self.series_combo.currentText()
        if not series:
            self._set_error("Add a series first.")
        elif self.add_addons_dir(series, self.dir_edit.text()):
            self.dir_edit.clear()

    def _browse_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select addons folder")
        if path:
            self.dir_edit.setText(path)

    def _remove_selected_dir(self) -> None:
        item = self.dirs_list.currentItem()
        if item:
            self.remove_addons_dir(self.series_combo.currentText(), item.text())

    # --- general -----------------------------------------------------------------

    def _build_general_group(self) -> QGroupBox:
        box = QGroupBox("General")
        grid = QGridLayout(box)
        self.dump_dir_edit = QLineEdit(str(self.settings.dump_dir))
        self.dump_dir_edit.editingFinished.connect(self._save_dump_dir)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_dump_dir)
        self.jobs_spin = QSpinBox()
        self.jobs_spin.setRange(1, 32)
        self.jobs_spin.setValue(self.settings.jobs)
        self.jobs_spin.valueChanged.connect(lambda v: setattr(self.settings, "jobs", v))
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(THEMES)
        self.theme_combo.setCurrentText(self.settings.theme)
        self.theme_combo.currentTextChanged.connect(self._save_theme)
        grid.addWidget(QLabel("Default dump folder"), 0, 0)
        grid.addWidget(self.dump_dir_edit, 0, 1)
        grid.addWidget(browse, 0, 2)
        grid.addWidget(QLabel("Parallel restore jobs"), 1, 0)
        grid.addWidget(self.jobs_spin, 1, 1)
        grid.addWidget(QLabel("Theme"), 2, 0)
        grid.addWidget(self.theme_combo, 2, 1)
        grid.setColumnStretch(1, 1)
        return box

    def _save_dump_dir(self) -> None:
        text = self.dump_dir_edit.text().strip()
        if text:
            self.settings.dump_dir = text

    def _browse_dump_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select dump folder", str(self.settings.dump_dir))
        if path:
            self.dump_dir_edit.setText(path)
            self._save_dump_dir()

    def _save_theme(self, mode: str) -> None:
        self.settings.theme = mode
        self.theme_changed.emit(mode)
