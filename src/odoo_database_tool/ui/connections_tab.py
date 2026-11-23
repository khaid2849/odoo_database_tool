"""Connections tab: list, add / edit / duplicate / delete, and the Test button."""
from __future__ import annotations

import copy
import uuid
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QInputDialog, QLabel, QPlainTextEdit, QPushButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .. import ssh
from ..dump import DumpJob
from ..models import Connection, Kind
from ..store import ConnectionStore, Secrets
from . import dialogs
from .connection_dialog import KIND_LABELS, ConnectionDialog

BADGE_STYLE = ("background:#c0392b;color:white;border-radius:3px;padding:1px 6px;"
               "font-weight:bold;font-size:8pt;")


def format_checks(checks: list) -> str:
    """One line per check: ✓ ok, ! warning, ✗ failed (with its hint)."""
    lines = []
    for c in checks:
        mark = "!" if c.warning else ("✓" if c.ok else "✗")
        hint = f"  ({c.hint})" if c.hint and (c.warning or not c.ok) else ""
        lines.append(f"{mark} {c.name}: {c.detail}{hint}")
    return "\n".join(lines)


def ssh_targets(conn: Connection) -> list:
    """SSH targets Test will connect to: the login, and the tunnel of a direct connection."""
    targets = []
    if conn.ssh is not None:
        targets.append(conn.ssh)
    if conn.kind == Kind.POSTGRES and conn.tunnel is not None:
        targets.append(conn.tunnel)
    return targets


class ConnectionsTab(QWidget):
    connections_changed = Signal()

    def __init__(self, store: ConnectionStore, secrets: Secrets, window, parent=None):
        """`window` provides settings, builder(), run_job() and the job_running signal."""
        super().__init__(parent)
        self.store, self.secrets, self.window = store, secrets, window
        # Injectable seams (tests replace these; none of them touch the network by default).
        self.pick_kind = self._pick_kind
        self.open_dialog = self._open_dialog
        self.confirm = dialogs.confirm
        self.confirm_host_key = dialogs.confirm_host_key
        self.warn = dialogs.warn
        self.resolve_host_port = ssh.resolve_host_port
        self.host_key_known = ssh.host_key_known
        self.fetch_host_keys = ssh.fetch_host_keys
        self.add_known_host = ssh.add_known_host
        self.job_factory = lambda ctx: DumpJob(self.window.builder(), self.window.settings, ctx)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Type", ""])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 260)
        self.tree.setColumnWidth(1, 180)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.itemDoubleClicked.connect(lambda *_: self.edit())

        self.add_button = QPushButton("Add…")
        self.edit_button = QPushButton("Edit…")
        self.duplicate_button = QPushButton("Duplicate")
        self.delete_button = QPushButton("Delete")
        self.test_button = QPushButton("Test")
        self.add_button.clicked.connect(self.add)
        self.edit_button.clicked.connect(self.edit)
        self.duplicate_button.clicked.connect(self.duplicate)
        self.delete_button.clicked.connect(self.delete)
        self.test_button.clicked.connect(self.test)

        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        self.results = QPlainTextEdit()
        self.results.setReadOnly(True)
        self.results.setPlaceholderText("Test results appear here")
        self.results.setMaximumHeight(160)

        buttons = QHBoxLayout()
        for b in (self.add_button, self.edit_button, self.duplicate_button, self.delete_button):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(self.test_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tree, 1)
        layout.addLayout(buttons)
        layout.addWidget(self.status)
        layout.addWidget(self.results)

        self._job_active = False
        job_running = getattr(window, "job_running", None)
        if job_running is not None:
            job_running.connect(self._on_job_running)
        self.refresh()

    # --- list --------------------------------------------------------------------

    def refresh(self, select_id: Optional[str] = None) -> None:
        keep = select_id or self.selected_id()
        self.tree.clear()
        for conn in self.store.load():
            item = QTreeWidgetItem([conn.name, KIND_LABELS[conn.kind], ""])
            item.setData(0, Qt.UserRole, conn.id)
            self.tree.addTopLevelItem(item)
            if conn.production:
                badge = QLabel("PRODUCTION")
                badge.setStyleSheet(BADGE_STYLE)
                self.tree.setItemWidget(item, 2, badge)
            if conn.id == keep:
                self.tree.setCurrentItem(item)
        self._update_buttons()

    def selected_id(self) -> Optional[str]:
        item = self.tree.currentItem()
        return item.data(0, Qt.UserRole) if item is not None else None

    def selected(self) -> Optional[Connection]:
        conn_id = self.selected_id()
        return self.store.get(conn_id) if conn_id else None

    def _on_job_running(self, running: bool) -> None:
        self._job_active = running
        self._update_buttons()

    def _update_buttons(self) -> None:
        has = self.selected_id() is not None
        for b in (self.edit_button, self.duplicate_button, self.delete_button):
            b.setEnabled(has)
        self.test_button.setEnabled(has and not self._job_active)

    def _changed(self, select_id: Optional[str] = None) -> None:
        self.refresh(select_id)
        self.connections_changed.emit()

    # --- add / edit / duplicate / delete -----------------------------------------

    def _pick_kind(self) -> Optional[Kind]:
        labels = list(KIND_LABELS.values())
        label, ok = QInputDialog.getItem(self, "Add connection", "Connection type:", labels, 0, False)
        if not ok:
            return None
        return next(k for k, v in KIND_LABELS.items() if v == label)

    def _open_dialog(self, conn: Connection) -> Optional[Connection]:
        ssh_bin = self.window.builder().ssh_bin
        dlg = ConnectionDialog(conn, self.secrets, ssh.openssh_version(ssh_bin), self)
        return dlg.result_connection() if dlg.exec() == ConnectionDialog.Accepted else None

    def add(self) -> None:
        kind = self.pick_kind()
        if kind is None:
            return
        result = self.open_dialog(Connection.new("", kind))
        if result is not None:
            self.store.upsert(result)
            self._changed(result.id)

    def edit(self) -> None:
        conn = self.selected()
        if conn is None:
            return
        result = self.open_dialog(conn)
        if result is not None:
            self.store.upsert(result)
            self._changed(result.id)

    def duplicate(self) -> None:
        conn = self.selected()
        if conn is None:
            return
        clone = Connection.from_dict(copy.deepcopy(conn.to_dict()))
        clone.id = uuid.uuid4().hex
        clone.name = f"{conn.name} (copy)"
        self.store.upsert(clone)  # secrets are deliberately not copied
        self._changed(clone.id)

    def delete(self) -> None:
        conn = self.selected()
        if conn is None:
            return
        if not self.confirm(self, "Delete connection",
                            f"Delete '{conn.name}' and its saved passwords?", danger=True):
            return
        self.store.delete(conn.id)
        self.secrets.delete_all(conn.id)
        self._changed()

    # --- test --------------------------------------------------------------------

    def test(self) -> None:
        conn = self.selected()
        if conn is None or self._job_active:
            return
        self.results.clear()
        targets = ssh_targets(conn)
        if not targets:
            self._run_test(conn)
            return
        ssh_bin = self.window.builder().ssh_bin

        def find_unknown(ctx) -> list:
            pending = []
            for target in targets:
                host, port = self.resolve_host_port(ssh_bin, target)
                if not self.host_key_known(host, port, ssh_bin):
                    lines, fingerprints = self.fetch_host_keys(host, port, ssh_bin)
                    pending.append((host, port, lines, fingerprints))
            return pending

        self._start(find_unknown, lambda pending: self._accept_host_keys(conn, pending),
                    "Checking SSH host keys…", "Cannot fetch the SSH host key")

    def _accept_host_keys(self, conn: Connection, pending: list) -> None:
        for host, port, lines, fingerprints in pending:
            if not self.confirm_host_key(self, host, port, fingerprints):
                self.status.setText("Test cancelled: the host key was not accepted.")
                return
            try:
                self.add_known_host(lines)
            except OSError as exc:
                self.warn(self, "Cannot save host key",
                          f"known_hosts could not be updated: {type(exc).__name__}: {exc}")
                return
        self._run_test(conn)

    def _run_test(self, conn: Connection) -> None:
        self._start(lambda ctx: self.job_factory(ctx).test_connection(conn), self._show_results,
                    f"Testing {conn.name}…", "Test failed")

    def _start(self, fn, on_done, status: str, failure: str) -> None:
        def failed(message: str) -> None:
            self.status.setText(f"{failure}: {message}")
            self.results.setPlainText(f"✗ {failure}: {message}")

        self.status.setText(status)  # before run_job: a quick job may finish synchronously
        if not self.window.run_job(fn, on_done, failed):
            self.status.setText("Another operation is running.")

    def _show_results(self, checks: list) -> None:
        self.results.setPlainText(format_checks(checks))
        bad = [c for c in checks if not c.ok]
        self.status.setText(f"{len(bad)} check(s) failed." if bad else "All checks passed.")
