"""Connection model and input validation (Qt-free)."""
from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field, fields
from enum import Enum

from .common import odoo_data_dir

CONTAINER_FILESTORE_ROOT = "/var/lib/odoo/filestore"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_LOCAL_HOSTS = ("", "localhost", "127.0.0.1", "::1")


def _default_env_names() -> dict:
    return {"host": "HOST", "port": "PORT", "user": "USER", "password": "PASSWORD"}


class Kind(str, Enum):
    SSH_DOCKER = "ssh_docker"
    SSH_HOST = "ssh_host"
    POSTGRES = "postgres"
    LOCAL = "local"


@dataclass
class SshTarget:
    alias: str = ""
    host: str = ""
    port: int = 22
    user: str = ""
    identity_file: str = ""
    proxy_jump: str = ""
    use_password: bool = False
    expected_hostname: str = ""
    use_sudo: bool = False

    def destination(self) -> str:
        """What is passed to ssh: the ~/.ssh/config alias if set, else the host."""
        return self.alias or self.host


@dataclass
class PgEndpoint:
    host: str = "localhost"
    port: int = 5432
    user: str = ""
    no_password: bool = False
    sslmode: str = "prefer"


@dataclass
class Connection:
    id: str
    name: str
    kind: Kind
    production: bool = False
    ssh: SshTarget | None = None
    pg: PgEndpoint | None = None
    tunnel: SshTarget | None = None
    container: str = ""
    env_names: dict = field(default_factory=_default_env_names)
    odoo_conf_path: str = ""
    pg_os_user: str = ""
    filestore_root: str = ""

    @classmethod
    def new(cls, name: str, kind: Kind) -> Connection:
        kind = Kind(kind)
        conn = cls(id=uuid.uuid4().hex, name=name, kind=kind)
        if kind in (Kind.SSH_DOCKER, Kind.SSH_HOST):
            conn.ssh = SshTarget()
        if kind != Kind.SSH_DOCKER:
            conn.pg = PgEndpoint()
        return conn

    def to_dict(self) -> dict:
        data = asdict(self)
        data["kind"] = self.kind.value
        return data

    @classmethod
    def from_dict(cls, d: dict) -> Connection:
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in d.items() if k in known}
        kwargs["kind"] = Kind(kwargs["kind"])
        for key, sub in (("ssh", SshTarget), ("tunnel", SshTarget), ("pg", PgEndpoint)):
            if kwargs.get(key) is not None:
                sub_known = {f.name for f in fields(sub)}
                kwargs[key] = sub(**{k: v for k, v in kwargs[key].items() if k in sub_known})
        if "env_names" in kwargs:
            kwargs["env_names"] = {**_default_env_names(), **kwargs["env_names"]}
        return cls(**kwargs)

    def supports_filestore(self) -> bool:
        if self.kind == Kind.POSTGRES:
            return False
        if self.kind == Kind.SSH_HOST:
            return bool(self.filestore_root)
        return True

    def can_be_restore_target(self) -> bool:
        """Restore is allowed only into local or direct-Postgres, non-production targets."""
        return self.kind in (Kind.LOCAL, Kind.POSTGRES) and not self.production

    def effective_filestore_root(self) -> str:
        if self.filestore_root:
            return self.filestore_root
        if self.kind == Kind.SSH_DOCKER:
            return CONTAINER_FILESTORE_ROOT
        if self.kind == Kind.LOCAL:
            return str(odoo_data_dir() / "filestore")
        return ""


def is_local_host(host: str) -> bool:
    """Loopback name, empty (libpq default socket) or a socket directory."""
    host = (host or "").strip()
    return host in _LOCAL_HOSTS or host.startswith("/")


def is_local_target(conn: Connection) -> bool:
    """True when the Postgres server is on this machine (loopback host or socket, no tunnel)."""
    if conn.kind not in (Kind.LOCAL, Kind.POSTGRES) or conn.tunnel is not None:
        return False
    return is_local_host(conn.pg.host if conn.pg else "")


def validate_identifier(value: str, what: str) -> str:
    """Return value if it is a safe DB/container name, else raise ValueError."""
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"Invalid {what}: {value!r} (use letters, digits, '_', '.', '-')")
    return value


def validate_remote_path(value: str) -> str:
    """Return value if it is an absolute POSIX path without NUL/newline, else raise ValueError."""
    if not value.startswith("/") or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"Invalid remote path: {value!r} (must be absolute, single line)")
    return value


def _check_ssh(label: str, ssh: SshTarget | None, errors: list) -> None:
    if ssh is None or not ssh.destination().strip():
        errors.append(f"{label}: enter an SSH host or alias.")
    elif not 1 <= int(ssh.port) <= 65535:
        errors.append(f"{label}: SSH port must be between 1 and 65535.")
    if ssh is None:
        return
    # ssh would read a leading '-' as an option (e.g. -oProxyCommand=…)
    for what, value in (("host", ssh.host), ("alias", ssh.alias), ("ProxyJump", ssh.proxy_jump)):
        if value.strip().startswith("-"):
            errors.append(f"{label}: {what} must not start with '-'.")


def validate_connection(conn: Connection) -> list:
    """Human-readable problems with a connection; empty list means valid."""
    errors: list = []
    if not conn.name.strip():
        errors.append("Enter a connection name.")
    if conn.kind in (Kind.SSH_DOCKER, Kind.SSH_HOST):
        _check_ssh("SSH", conn.ssh, errors)
    if conn.tunnel is not None:
        _check_ssh("SSH tunnel", conn.tunnel, errors)
    if conn.kind == Kind.SSH_DOCKER:
        if not conn.container.strip():
            errors.append("Enter the Docker container name.")
        else:
            try:
                validate_identifier(conn.container, "container name")
            except ValueError as exc:
                errors.append(str(exc))
    else:
        if conn.pg is None:
            errors.append("Postgres settings are missing.")
        elif not 1 <= int(conn.pg.port) <= 65535:
            errors.append("Postgres port must be between 1 and 65535.")
        elif conn.kind == Kind.POSTGRES and not conn.pg.host.strip():
            errors.append("Enter the Postgres host.")
        elif conn.kind == Kind.LOCAL and not is_local_host(conn.pg.host):
            errors.append("Local Postgres needs localhost, 127.0.0.1, ::1 or a socket folder; "
                          "use Direct Postgres for remote hosts.")
    for label, path in (("Odoo config path", conn.odoo_conf_path), ("Filestore root", conn.filestore_root)):
        if path and conn.kind != Kind.LOCAL:  # local paths may be Windows-style
            try:
                validate_remote_path(path)
            except ValueError as exc:
                errors.append(f"{label}: {exc}")
    return errors
