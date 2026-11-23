"""Connection persistence, keychain secrets and the SSH_ASKPASS helper (Qt-free)."""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from .models import Connection

log = logging.getLogger(__name__)

SERVICE = "odoo_database_tool"
SECRET_FIELDS = ("pg_password", "ssh_password", "tunnel_password")
ASKPASS_ENV = "ODT_ASKPASS_KEY"


def config_dir() -> Path:
    override = os.environ.get("ODT_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OdooDatabaseTool"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "OdooDatabaseTool"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "odoo_database_tool"


class ConnectionStore:
    """JSON file of connections; holds no secrets."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else config_dir() / "connections.json"

    def load(self) -> list[Connection]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            log.warning("Cannot read connections file %s: %s", self.path, type(exc).__name__)
            return []
        entries = raw.get("connections", []) if isinstance(raw, dict) else raw
        conns: list[Connection] = []
        for entry in entries if isinstance(entries, list) else []:
            try:
                conns.append(Connection.from_dict(entry))
            except (KeyError, ValueError, TypeError, AttributeError) as exc:
                log.warning("Skipping invalid connection entry: %s", type(exc).__name__)
        return conns

    def save(self, conns: list[Connection]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"connections": [c.to_dict() for c in conns]}, indent=2, ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            if os.name == "posix":
                os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def upsert(self, conn: Connection) -> None:
        conns = self.load()
        for i, existing in enumerate(conns):
            if existing.id == conn.id:
                conns[i] = conn
                break
        else:
            conns.append(conn)
        self.save(conns)

    def delete(self, conn_id: str) -> None:
        self.save([c for c in self.load() if c.id != conn_id])

    def get(self, conn_id: str) -> Connection | None:
        return next((c for c in self.load() if c.id == conn_id), None)


def _default_backend():
    try:
        import keyring
        return keyring.get_keyring()
    except Exception:  # noqa: BLE001 - any keyring failure means "no keychain"
        return None


class Secrets:
    """Passwords in the OS keychain, or in memory for this session when none is usable."""

    def __init__(self, backend=None):
        self._backend = backend if backend is not None else _default_backend()
        self._memory: dict[str, str] = {}
        self.available = self._probe()

    def _probe(self) -> bool:
        if self._backend is None:
            return False
        try:
            from keyring.backends.fail import Keyring as FailKeyring
            if isinstance(self._backend, FailKeyring):
                return False
            self._backend.get_password(SERVICE, "probe")
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _key(conn_id: str, field: str) -> str:
        return f"{conn_id}:{field}"

    def get(self, conn_id: str, field: str) -> str | None:
        key = self._key(conn_id, field)
        if not self.available:
            return self._memory.get(key)
        try:
            return self._backend.get_password(SERVICE, key)
        except Exception:  # noqa: BLE001
            return None

    def set(self, conn_id: str, field: str, value: str) -> None:
        key = self._key(conn_id, field)
        if not self.available:
            self._memory[key] = value
            return
        self._backend.set_password(SERVICE, key, value)

    def delete(self, conn_id: str, field: str) -> None:
        """Remove one stored secret; a missing entry is fine."""
        key = self._key(conn_id, field)
        self._memory.pop(key, None)
        if self.available:
            try:
                self._backend.delete_password(SERVICE, key)
            except Exception:  # noqa: BLE001
                pass

    def delete_all(self, conn_id: str) -> None:
        for field in SECRET_FIELDS:
            key = self._key(conn_id, field)
            self._memory.pop(key, None)
            if self.available:
                try:
                    self._backend.delete_password(SERVICE, key)
                except Exception:  # noqa: BLE001 - missing entry is fine
                    pass


def askpass_main(env, backend=None) -> int:
    """SSH_ASKPASS helper: print the secret named by env[ASKPASS_ENV]; 1 if absent."""
    key = env.get(ASKPASS_ENV, "")
    conn_id, _, field = key.rpartition(":")
    if not conn_id or field not in SECRET_FIELDS:
        return 1
    value = Secrets(backend).get(conn_id, field)
    if not value:
        return 1
    sys.stdout.write(value + "\n")
    sys.stdout.flush()
    return 0


def askpass_program() -> str:
    """Executable ssh can use as SSH_ASKPASS; writes a small wrapper when not frozen."""
    if getattr(sys, "frozen", False):
        return sys.executable
    if os.name == "nt":
        path = config_dir() / "askpass.cmd"
        content = f'@echo off\r\n"{sys.executable}" -m odoo_database_tool %*\r\n'
    else:
        path = config_dir() / "askpass.sh"
        content = f'#!/bin/sh\nexec "{sys.executable}" -m odoo_database_tool "$@"\n'
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            current = fh.read()
    except OSError:
        current = None
    if current != content:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
    if os.name == "posix" and (path.stat().st_mode & 0o777) != 0o700:
        os.chmod(path, 0o700)
    return str(path)
