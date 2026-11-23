"""Shared plumbing for long-running jobs: checks, context and errors. Qt-free."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

from .models import Connection, Kind
from .runner import CancelToken, Redactor
from .ssh import classify_ssh_error, reason_text
from .store import SECRET_FIELDS

LIST_DATABASES_SQL = ("select datname, pg_database_size(datname) from pg_database "
                      "where not datistemplate order by 1")
SSH_HINTS = {
    "host_key_changed": "The SSH host key has changed. Check with the server admin before connecting.",
    "host_key_unknown": "The SSH host key is not trusted yet. Use Test on the Connections tab to accept it.",
    "auth_failed": "SSH authentication failed: check the key, agent or password.",
    "unreachable": "Cannot SSH: VPN/whitelist/key?",
    "connection_lost": "The SSH connection was lost. Check the network and retry.",
}


@dataclass
class Check:
    """One preflight result; a warning has ok=True and warning=True (it never blocks)."""

    name: str
    ok: bool
    detail: str = ""
    hint: str = ""
    warning: bool = False


@dataclass
class JobContext:
    """Callbacks a job reports through; progress takes a percent (or None) and step text."""

    log: Callable[[str], None]
    progress: Callable[[Optional[int], str], None]
    cancel: CancelToken = field(default_factory=CancelToken)
    redactor: Redactor = field(default_factory=Redactor)


class JobError(Exception):
    """A failure whose message is safe to show in the UI (no secrets)."""


def human_bytes(n: float) -> str:
    """'1.5 GB' style size."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def last_line(text_or_lines) -> str:
    """Last non-blank line (stripped, max 300 chars) of a text or list of lines."""
    lines = text_or_lines.splitlines() if isinstance(text_or_lines, str) else text_or_lines
    for line in reversed(lines):
        if line.strip():
            return line.strip()[:300]
    return ""


def parse_database_sizes(out: str) -> list[tuple[str, int]]:
    """Rows of `LIST_DATABASES_SQL` output as (name, size in bytes)."""
    rows = []
    for line in out.splitlines():
        name, sep, size = line.strip().rpartition("|")
        if sep and name and size.isdigit():
            rows.append((name, int(size)))
    return rows


def register_secrets(redactor: Redactor, secrets, conn: Connection) -> None:
    """Mask every stored secret of conn in logs and messages."""
    for name in SECRET_FIELDS:
        redactor.add(secrets.get(conn.id, name))


def open_tunnel(stack: ExitStack, tunnel_factory, ssh_bin: str,
                conn: Connection) -> Optional[Tuple[str, int]]:
    """Open the SSH tunnel of a direct-Postgres connection on stack; returns the pg_override."""
    if conn.kind != Kind.POSTGRES or conn.tunnel is None:
        return None
    tunnel = stack.enter_context(tunnel_factory(
        ssh_bin, conn.tunnel, conn.id, conn.pg.host, conn.pg.port))
    return ("127.0.0.1", tunnel.local_port)


def check_hostname(builder, conn: Connection, capture, redactor: Redactor,
                   expected: str = "") -> Check:
    """Run `hostname` over the connection's SSH login (or tunnel); fails on a mismatch."""
    name = "Hostname"
    try:
        cmd = builder.hostname(conn)
    except ValueError as exc:
        return Check(name, False, str(exc))
    rc, out, err = capture(cmd, timeout=30, redactor=redactor)
    if rc != 0:
        kind = classify_ssh_error(err, rc)
        reason = reason_text(kind) if kind else (redactor(last_line(err)) or f"exit code {rc}")
        return Check(name, False, f"Cannot run hostname: {reason}",
                     SSH_HINTS.get(kind or "", "Check the SSH settings of this connection."))
    actual = out.strip()
    if expected and actual != expected:
        return Check(name, False, f"The machine is '{actual}', expected '{expected}'.",
                     "Wrong machine? Check the SSH host/alias and the expected hostname.")
    return Check(name, True, actual)


def require_expected_hostname(builder, conn: Connection, capture, redactor: Redactor) -> None:
    """Raise JobError unless the SSH machine is the expected one (no-op when none is set)."""
    guard = conn.ssh or conn.tunnel
    if guard is None or not guard.expected_hostname:
        return
    check = check_hostname(builder, conn, capture, redactor, guard.expected_hostname)
    if not check.ok:
        raise JobError(f"{check.detail} {check.hint}".strip())
