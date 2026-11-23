"""Dump tab: pick a source, list its databases, then Check or Dump in the background."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from ..dump import DumpJob, DumpOptions
from ..jobs import human_bytes
from ..models import Connection
from ..store import ConnectionStore
from . import dialogs
from .connection_dialog import KIND_LABELS

FILESTORE_TOOLTIP = "This connection has no filestore access (direct Postgres, or no filestore path set)."


def source_label(conn: Connection) -> str:
    label = f"{conn.name} ({KIND_LABELS[conn.kind]})"
    return f"{label} PRODUCTION" if conn.production else label


def format_check_lines(checks: list) -> str:
    """One line per check (✓ ok, ⚠ warning, ✗ failed) plus its hint on a following line."""
    lines = []
    for c in checks:
        mark = "⚠" if c.warning else ("✓" if c.ok else "✗")
        lines.append(f"{mark} {c.name} — {c.detail}" if c.detail else f"{mark} {c.name}")
        if c.hint:
            lines.append(f"    {c.hint}")
    return "\n".join(lines)


class DumpTab(QWidget):
    def __init__(self, store: ConnectionStore, window, job_factory=None, parent=None):
        """`window` provides settings, builder(), run_job() and the job_running signal."""
        super().__init__(parent)
        self.store, self.window = store, window
        # Injectable seams (tests replace these).
        self.job_factory = job_factory or (
            lambda ctx: DumpJob(self.window.builder(), self.window.settings, ctx))
        self.confirm = dialogs.confirm
        self.pick_folder = lambda start: QFileDialog.getExistingDirectory(
            self, "Save dumps to", start)
        self.open_url = QDesktopServices.openUrl
        self._job_active = False
        self._last_folder: Optional[str] = None

        self.source_combo = QComboBox()
        self.load_button = QPushButton("Load databases")
        self.db_combo = QComboBox()
        self.db_check = QCheckBox("Database")
        self.db_check.setChecked(True)
        self.filestore_check = QCheckBox("Filestore")
        self.gzip_check = QCheckBox("Gzip the filestore")
        self.dest_edit = QLineEdit(str(window.settings.dump_dir))
        self.browse_button = QPushButton("Browse…")
        self.check_button = QPushButton("Check")
        self.dump_button = QPushButton("Dump")
        self.dump_button.setObjectName("primaryBtn")
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.result.hide()
        self.open_button = QPushButton("Open folder")
        self.open_button.hide()

        source_row = QHBoxLayout()
        source_row.addWidget(self.source_combo, 1)
        source_row.addWidget(self.load_button)
        what_row = QHBoxLayout()
        for w in (self.db_check, self.filestore_check, self.gzip_check):
            what_row.addWidget(w)
        what_row.addStretch(1)
        dest_row = QHBoxLayout()
        dest_row.addWidget(self.dest_edit, 1)
        dest_row.addWidget(self.browse_button)
        form = QFormLayout()
        form.addRow("Source", source_row)
        form.addRow("Database", self.db_combo)
        form.addRow("Include", what_row)
        form.addRow("Save to", dest_row)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(self.check_button)
        actions.addWidget(self.dump_button)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(actions)
        layout.addWidget(self.status)
        layout.addWidget(self.result)
        layout.addWidget(self.open_button, 0, Qt.AlignLeft)
        layout.addStretch(1)

        self.load_button.clicked.connect(self.load_databases)
        self.browse_button.clicked.connect(self.browse)
        self.check_button.clicked.connect(self.check)
        self.dump_button.clicked.connect(self.dump)
        self.open_button.clicked.connect(self.open_folder)
        self.source_combo.currentIndexChanged.connect(self._source_changed)
        self.db_combo.currentIndexChanged.connect(self._update_buttons)
        self.db_check.toggled.connect(self._update_buttons)
        self.filestore_check.toggled.connect(self._update_buttons)
        job_running = getattr(window, "job_running", None)
        if job_running is not None:
            job_running.connect(self._on_job_running)
        connections_changed = getattr(window, "connections_changed", None)
        if connections_changed is not None:
            connections_changed.connect(self.refresh_sources)
        self.refresh_sources()

    # --- source / databases ------------------------------------------------------

    def selected_connection(self) -> Optional[Connection]:
        conn_id = self.source_combo.currentData()
        return self.store.get(conn_id) if conn_id else None

    def selected_db(self) -> Optional[str]:
        return self.db_combo.currentData() if self.db_combo.count() else None

    def refresh_sources(self) -> None:
        keep = self.source_combo.currentData()
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        for conn in self.store.load():
            self.source_combo.addItem(source_label(conn), conn.id)
        index = self.source_combo.findData(keep) if keep else -1
        self.source_combo.setCurrentIndex(index if index >= 0 else (0 if self.source_combo.count() else -1))
        self.source_combo.blockSignals(False)
        if self.source_combo.currentData() != keep:
            self._source_changed()
        else:
            self._update_filestore()
            self._update_buttons()

    def _source_changed(self) -> None:
        self.db_combo.clear()
        self._update_filestore()
        self._update_buttons()

    def _update_filestore(self) -> None:
        conn = self.selected_connection()
        supported = conn is not None and conn.supports_filestore()
        if not supported:
            self.filestore_check.setChecked(False)
        self.filestore_check.setEnabled(supported)
        self.filestore_check.setToolTip("" if supported else FILESTORE_TOOLTIP)

    def load_databases(self) -> None:
        conn = self.selected_connection()
        if conn is None or self._job_active:
            return
        self.db_combo.clear()
        self._update_buttons()
        self.status.setText(f"Loading databases from {conn.name}…")
        self._start(lambda ctx: self.job_factory(ctx).list_databases(conn),
                    self._databases_loaded, "Cannot list databases")

    def _databases_loaded(self, databases: list) -> None:
        self.db_combo.clear()
        for name, size in databases:
            self.db_combo.addItem(f"{name} — {human_bytes(size)}", name)
        self.status.setText(f"{len(databases)} database(s).")
        self._update_buttons()

    # --- buttons -----------------------------------------------------------------

    def _on_job_running(self, running: bool) -> None:
        self._job_active = running
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.gzip_check.setEnabled(self.filestore_check.isChecked())
        has_db = self.selected_db() is not None
        wants = self.db_check.isChecked() or self.filestore_check.isChecked()
        idle = not self._job_active
        self.load_button.setEnabled(idle and self.selected_connection() is not None)
        self.check_button.setEnabled(idle and has_db)
        self.dump_button.setEnabled(idle and has_db and wants)

    def browse(self) -> None:
        folder = self.pick_folder(self.dest_edit.text())
        if folder:
            self.dest_edit.setText(folder)

    # --- check / dump ------------------------------------------------------------

    def _options(self) -> Optional[DumpOptions]:
        conn, db = self.selected_connection(), self.selected_db()
        if conn is None or db is None:
            return None
        return DumpOptions(conn, db, self.db_check.isChecked(), self.filestore_check.isChecked(),
                           self.gzip_check.isChecked() and self.filestore_check.isChecked(),
                           Path(self.dest_edit.text().strip()))

    def check(self) -> None:
        opts = self._options()
        if opts is None or self._job_active:
            return
        self._clear_result()
        self.status.setText(f"Checking {opts.conn.name}…")
        self._start(lambda ctx: self.job_factory(ctx).preflight(opts),
                    self._checks_done, "Check failed")

    def _checks_done(self, checks: list) -> None:
        bad = [c for c in checks if not c.ok]
        self._show("resultBad" if bad else "resultOk", format_check_lines(checks))
        self.status.setText(f"{len(bad)} check(s) failed." if bad else "All checks passed.")

    def dump(self) -> None:
        opts = self._options()
        if opts is None or self._job_active or not (opts.with_db or opts.with_filestore):
            return
        production = opts.conn.production
        if production and not self.confirm(
                self, "Dump production",
                f"'{opts.conn.name}' is a PRODUCTION connection. Dumping '{opts.db}' puts "
                "live load on the server and copies real customer data to this computer. "
                "Continue?", danger=True):
            return
        self._clear_result()
        self.status.setText(f"Dumping {opts.db} from {opts.conn.name}…")
        self._start(lambda ctx: self.job_factory(ctx).run(opts, confirmed_production=production),
                    lambda res: self._dump_done(res, opts.dest), "Dump failed")

    def _dump_done(self, result, dest: Path) -> None:
        lines = ["Done."]
        for f in result.files:
            size = human_bytes(f.stat().st_size) if f.exists() else "?"
            lines.append(f"{f.name} — {size}")
        lines += [f"⚠ {w}" for w in result.warnings]
        self._show("resultOk", "\n".join(lines))
        self._last_folder = str(result.files[0].parent if result.files else dest)
        self.open_button.show()
        self.status.setText("Dump finished.")

    def open_folder(self) -> None:
        if self._last_folder:
            self.open_url(QUrl.fromLocalFile(self._last_folder))

    # --- helpers -----------------------------------------------------------------

    def _start(self, fn, on_done, failure: str) -> None:
        def failed(message: str) -> None:
            self.status.setText("")
            self._show("resultBad", f"{failure}: {message}")

        if not self.window.run_job(fn, on_done, failed):
            self.status.setText("Another operation is running.")

    def _show(self, name: str, text: str) -> None:
        self.result.setObjectName(name)
        self.result.setText(text)
        self.result.style().unpolish(self.result)
        self.result.style().polish(self.result)
        self.result.show()
        self.open_button.hide()

    def _clear_result(self) -> None:
        self.result.hide()
        self.open_button.hide()
