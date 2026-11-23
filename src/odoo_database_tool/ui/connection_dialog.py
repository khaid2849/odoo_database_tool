"""Add / edit dialog for one connection; type-specific pages per spec §9.2."""
from __future__ import annotations

import copy
from typing import Optional

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QPushButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from ..jobs import SSH_HINTS, JobError, last_line
from ..models import CONTAINER_FILESTORE_ROOT, Connection, Kind, PgEndpoint, SshTarget, validate_connection
from ..runner import run_capture
from ..ssh import classify_ssh_error, reason_text, supports_askpass
from ..store import Secrets
from . import dialogs

KIND_LABELS = {
    Kind.SSH_DOCKER: "SSH + Docker",
    Kind.SSH_HOST: "SSH + host Postgres",
    Kind.POSTGRES: "Direct Postgres",
    Kind.LOCAL: "Local Postgres",
}
SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
UNCHANGED = "(unchanged)"


def _port_spin(value: int) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(1, 65535)
    spin.setValue(int(value))
    return spin


class PasswordField(QWidget):
    """Masked password with a Clear action; empty input keeps the stored secret."""

    def __init__(self, secrets: Secrets, conn_id: str, field: str, parent=None):
        super().__init__(parent)
        self.field = field
        self.cleared = False
        self._stored = bool(secrets.get(conn_id, field))
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.Password)
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self._clear)
        self.clear_button.setEnabled(self._stored)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.edit, 1)
        row.addWidget(self.clear_button)
        self._refresh()

    def _refresh(self) -> None:
        self.edit.setPlaceholderText(UNCHANGED if self._stored and not self.cleared else "")

    def _clear(self) -> None:
        self.cleared = True
        self.edit.clear()
        self.clear_button.setEnabled(False)
        self._refresh()

    def text(self) -> str:
        return self.edit.text()

    def apply(self, secrets: Secrets, conn_id: str) -> None:
        """Write or remove the secret; leaves it alone when untouched. May raise."""
        if self.edit.text():
            secrets.set(conn_id, self.field, self.edit.text())
        elif self.cleared:
            secrets.delete(conn_id, self.field)


class SshGroup(QGroupBox):
    """SSH target fields (§9.2 note 1), optionally checkable (tunnel)."""

    def __init__(self, title: str, target: SshTarget, secrets: Secrets, conn_id: str,
                 field: str, password_ok: bool, why_not: str, checkable: bool = False):
        super().__init__(title)
        self.alias = QLineEdit(target.alias)
        self.alias.setPlaceholderText("Alias from ~/.ssh/config, or fill host below")
        self.host = QLineEdit(target.host)
        self.port = _port_spin(target.port)
        self.user = QLineEdit(target.user)
        self.identity = QLineEdit(target.identity_file)
        self.proxy = QLineEdit(target.proxy_jump)
        self.expected = QLineEdit(target.expected_hostname)
        self.expected.setPlaceholderText("Optional: refuse if `hostname` differs")
        self.sudo = QCheckBox("Use sudo -n (passwordless sudo only)")
        self.sudo.setChecked(target.use_sudo)
        self.use_password = QCheckBox("Use password")
        self.use_password.setChecked(target.use_password)
        self.password = PasswordField(secrets, conn_id, field)
        if not password_ok:
            self.use_password.setEnabled(False)
            self.use_password.setToolTip(why_not)
        self.password.setEnabled(password_ok and target.use_password)
        self.use_password.toggled.connect(lambda on: self.password.setEnabled(password_ok and on))
        form = QFormLayout(self)
        form.addRow("Alias", self.alias)
        form.addRow("Host", self.host)
        form.addRow("Port", self.port)
        form.addRow("User", self.user)
        form.addRow("Identity file", self.identity)
        form.addRow("ProxyJump", self.proxy)
        form.addRow("Expected hostname", self.expected)
        form.addRow("", self.sudo)
        form.addRow("", self.use_password)
        form.addRow("Password", self.password)
        if checkable:
            self.setCheckable(True)
            self.setChecked(True)

    def target(self) -> SshTarget:
        return SshTarget(
            alias=self.alias.text().strip(), host=self.host.text().strip(), port=self.port.value(),
            user=self.user.text().strip(), identity_file=self.identity.text().strip(),
            proxy_jump=self.proxy.text().strip(), use_password=self.use_password.isChecked(),
            expected_hostname=self.expected.text().strip(), use_sudo=self.sudo.isChecked())

    def password_active(self) -> bool:
        return self.use_password.isEnabled() and self.use_password.isChecked()


class PgGroup(QGroupBox):
    """Postgres endpoint fields; SSL mode only for direct connections."""

    def __init__(self, pg: PgEndpoint, secrets: Secrets, conn_id: str, with_ssl: bool):
        super().__init__("Postgres")
        self.host = QLineEdit(pg.host)
        self.port = _port_spin(pg.port)
        self.user = QLineEdit(pg.user)
        self.no_password = QCheckBox("No password (socket / peer / trust auth)")
        self.no_password.setChecked(pg.no_password)
        self.password = PasswordField(secrets, conn_id, "pg_password")
        self.password.setEnabled(not pg.no_password)
        self.no_password.toggled.connect(lambda on: self.password.setEnabled(not on))
        self.sslmode: Optional[QComboBox] = None
        form = QFormLayout(self)
        form.addRow("Host", self.host)
        form.addRow("Port", self.port)
        form.addRow("User", self.user)
        form.addRow("", self.no_password)
        form.addRow("Password", self.password)
        if with_ssl:
            self.sslmode = QComboBox()
            self.sslmode.addItems(SSL_MODES)
            self.sslmode.setCurrentText(pg.sslmode if pg.sslmode in SSL_MODES else "prefer")
            form.addRow("SSL mode", self.sslmode)

    def endpoint(self) -> PgEndpoint:
        return PgEndpoint(host=self.host.text().strip(), port=self.port.value(),
                          user=self.user.text().strip(), no_password=self.no_password.isChecked(),
                          sslmode=self.sslmode.currentText() if self.sslmode else "prefer")


class ConnectionDialog(QDialog):
    def __init__(self, conn: Connection, secrets: Secrets,
                 openssh_version: Optional[tuple], parent=None, run_job=None, make_builder=None):
        """`run_job`/`make_builder` (the main window's) enable the "List…" containers helper."""
        super().__init__(parent)
        self._conn = copy.deepcopy(conn)
        self.secrets = secrets
        self.run_job, self.make_builder = run_job, make_builder
        # Injectable seams (tests replace these).
        self.capture = run_capture
        self.pick_container = self._pick_container
        self.setWindowTitle(f"{KIND_LABELS[conn.kind]} connection")
        self.setMinimumWidth(480)
        kind = conn.kind

        if not supports_askpass(openssh_version):
            self._why_not = "Password SSH needs OpenSSH 8.4 or newer; use a key or ssh-agent."
        elif not secrets.available:
            self._why_not = "Password SSH needs the OS keychain, which was not found."
        else:
            self._why_not = ""

        self.name_edit = QLineEdit(conn.name)
        self.production = QCheckBox("Production (never a restore target; confirms before dumping)")
        self.production.setChecked(conn.production)
        head = QFormLayout()
        head.addRow("Name", self.name_edit)
        head.addRow("Type", QLabel(KIND_LABELS[kind]))
        head.addRow("", self.production)

        self.ssh_group: Optional[SshGroup] = None
        self.tunnel_group: Optional[SshGroup] = None
        self.pg_group: Optional[PgGroup] = None
        self.container_edit: Optional[QLineEdit] = None
        self.list_button: Optional[QPushButton] = None
        self.container_status: Optional[QLabel] = None
        self.env_edits: dict = {}
        self.odoo_conf_edit: Optional[QLineEdit] = None
        self.os_user_edit: Optional[QLineEdit] = None
        self.filestore_edit: Optional[QLineEdit] = None

        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        if kind in (Kind.SSH_DOCKER, Kind.SSH_HOST):
            self.ssh_group = self._ssh_group("SSH target", self._conn.ssh or SshTarget(), "ssh_password")
            page_layout.addWidget(self.ssh_group)
        if kind == Kind.SSH_DOCKER:
            page_layout.addWidget(self._docker_group())
        else:
            self.pg_group = PgGroup(self._conn.pg or PgEndpoint(), secrets, conn.id,
                                    with_ssl=kind == Kind.POSTGRES)
            page_layout.addWidget(self.pg_group)
        if kind == Kind.POSTGRES:
            self.tunnel_group = self._ssh_group("Use SSH tunnel", self._conn.tunnel or SshTarget(),
                                                "tunnel_password", checkable=True)
            self.tunnel_group.setChecked(self._conn.tunnel is not None)
            page_layout.addWidget(self.tunnel_group)
        if kind != Kind.POSTGRES:
            page_layout.addWidget(self._extras_group())
        page_layout.addStretch(1)

        # One page per kind, keyed by Kind; only the current kind's page is built.
        self.stack = QStackedWidget()
        self._page_index = {kind: self.stack.addWidget(page)}
        self.stack.setCurrentIndex(self._page_index[kind])

        self.error_label = QLabel()
        self.error_label.setObjectName("resultBad")
        self.error_label.setWordWrap(True)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(head)
        layout.addWidget(self.stack)
        layout.addWidget(self.error_label)
        layout.addWidget(self.buttons)

        for widget in self.findChildren(QLineEdit):
            widget.textChanged.connect(self._revalidate)
        for widget in self.findChildren(QSpinBox):
            widget.valueChanged.connect(self._revalidate)
        for widget in self.findChildren(QCheckBox):
            widget.toggled.connect(self._revalidate)
        if self.tunnel_group is not None:
            self.tunnel_group.toggled.connect(self._revalidate)
        self._revalidate()

    # --- building ----------------------------------------------------------------

    def _ssh_group(self, title: str, target: SshTarget, field: str, checkable: bool = False) -> SshGroup:
        return SshGroup(title, target, self.secrets, self._conn.id, field,
                        password_ok=not self._why_not, why_not=self._why_not, checkable=checkable)

    def _docker_group(self) -> QGroupBox:
        conn = self._conn
        box = QGroupBox("Docker container")
        form = QFormLayout(box)
        self.container_edit = QLineEdit(conn.container)
        self.list_button = QPushButton("List…")
        self.list_button.setToolTip("List the running containers on the SSH host")
        self.list_button.setEnabled(self.run_job is not None and self.make_builder is not None)
        self.list_button.clicked.connect(self.list_containers)
        self.container_status = QLabel("")
        self.container_status.setObjectName("muted")
        self.container_status.setWordWrap(True)
        self.container_status.hide()
        row = QHBoxLayout()
        row.addWidget(self.container_edit, 1)
        row.addWidget(self.list_button)
        form.addRow("Container name", row)
        form.addRow("", self.container_status)
        for key, label in (("host", "Host env var"), ("port", "Port env var"),
                           ("user", "User env var"), ("password", "Password env var")):
            edit = QLineEdit(conn.env_names.get(key, key.upper()))
            self.env_edits[key] = edit
            form.addRow(label, edit)
        self.odoo_conf_edit = QLineEdit(conn.odoo_conf_path)
        self.odoo_conf_edit.setPlaceholderText("Optional fallback, e.g. /etc/odoo/odoo.conf")
        form.addRow("Odoo config in container", self.odoo_conf_edit)
        self.filestore_edit = QLineEdit(conn.filestore_root)
        self.filestore_edit.setPlaceholderText(CONTAINER_FILESTORE_ROOT)
        form.addRow("Filestore root", self.filestore_edit)
        return box

    def _extras_group(self) -> QGroupBox:
        conn = self._conn
        box = QGroupBox("Filestore")
        form = QFormLayout(box)
        if conn.kind == Kind.SSH_HOST:
            self.os_user_edit = QLineEdit(conn.pg_os_user)
            self.os_user_edit.setPlaceholderText("Optional, e.g. postgres (sudo -n -u)")
            form.addRow("Run Postgres tools as OS user", self.os_user_edit)
        self.filestore_edit = QLineEdit(conn.filestore_root)
        self.filestore_edit.setPlaceholderText(
            "Optional" if conn.kind == Kind.SSH_HOST else "Default: Odoo data dir")
        form.addRow("Filestore root", self.filestore_edit)
        return box

    # --- containers helper --------------------------------------------------------

    def _pick_container(self, names: list) -> Optional[str]:
        current = self.container_edit.text().strip()
        index = names.index(current) if current in names else 0
        name, ok = QInputDialog.getItem(self, "Containers", "Running containers:", names, index, False)
        return name if ok else None

    def _container_note(self, text: str) -> None:
        self.container_status.setText(text)
        self.container_status.setVisible(bool(text))

    def list_containers(self) -> None:
        """Run `docker ps` over SSH in the background and let the user pick a name."""
        if self.run_job is None or self.make_builder is None:
            return
        conn = self.result_connection()
        if conn.ssh is None or not conn.ssh.destination().strip():
            self._container_note("Enter the SSH host or alias first.")
            return
        try:
            cmd = self.make_builder().list_containers(conn)
        except ValueError as exc:
            self._container_note(str(exc))
            return
        capture = self.capture

        def work(ctx) -> list:
            rc, out, err = capture(cmd, timeout=30, redactor=ctx.redactor)
            if rc != 0:
                kind = classify_ssh_error(err, rc)
                reason = reason_text(kind) if kind else (last_line(err) or f"exit code {rc}")
                hint = SSH_HINTS.get(kind or "", "Check the SSH settings (and sudo -n).")
                raise JobError(f"Cannot list containers: {reason}. {hint}")
            return [line.strip() for line in out.splitlines() if line.strip()]

        def done(names: list) -> None:
            if not names:
                self._container_note("No running containers found.")
                return
            self._container_note("")
            name = self.pick_container(names)
            if name:
                self.container_edit.setText(name)

        self._container_note("Listing containers…")
        if not self.run_job(work, done, self._container_note):
            self._container_note("Another operation is running.")

    # --- result ------------------------------------------------------------------

    def result_connection(self) -> Connection:
        """The edited connection; never carries a password."""
        conn = copy.deepcopy(self._conn)
        conn.name = self.name_edit.text().strip()
        conn.production = self.production.isChecked()
        if self.ssh_group is not None:
            conn.ssh = self.ssh_group.target()
        if self.pg_group is not None:
            conn.pg = self.pg_group.endpoint()
        if self.tunnel_group is not None:
            conn.tunnel = self.tunnel_group.target() if self.tunnel_group.isChecked() else None
        if self.container_edit is not None:
            conn.container = self.container_edit.text().strip()
            conn.env_names = {k: e.text().strip() or k.upper() for k, e in self.env_edits.items()}
            conn.odoo_conf_path = self.odoo_conf_edit.text().strip()
        if self.os_user_edit is not None:
            conn.pg_os_user = self.os_user_edit.text().strip()
        if self.filestore_edit is not None:
            conn.filestore_root = self.filestore_edit.text().strip()
        return conn

    def validation_errors(self) -> list:
        return validate_connection(self.result_connection())

    def _revalidate(self, *_args) -> None:
        errors = self.validation_errors()
        self.error_label.setText("\n".join(errors))
        self.error_label.setVisible(bool(errors))
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(not errors)

    def _password_fields(self) -> list:
        fields = []
        if self.ssh_group is not None and self.ssh_group.password_active():
            fields.append(self.ssh_group.password)
        if self.pg_group is not None and not self.pg_group.no_password.isChecked():
            fields.append(self.pg_group.password)
        if (self.tunnel_group is not None and self.tunnel_group.isChecked()
                and self.tunnel_group.password_active()):
            fields.append(self.tunnel_group.password)
        return fields

    def _accept(self) -> None:
        if self.validation_errors():
            return
        try:
            for field in self._password_fields():
                field.apply(self.secrets, self._conn.id)
        except Exception as exc:  # noqa: BLE001 - keychain backends raise anything
            dialogs.warn(self, "Cannot save password",
                         f"The password could not be saved: {type(exc).__name__}: {exc}")
            return
        self.accept()
