from __future__ import annotations

import json
import logging
import os
import stat
import sys

import pytest
from keyring.backends.fail import Keyring as FailKeyring

from odoo_database_tool import store
from odoo_database_tool.models import Connection, Kind
from odoo_database_tool.store import ASKPASS_ENV, ConnectionStore, Secrets, askpass_main


class FakeBackend:
    def __init__(self):
        self.data = {}

    def get_password(self, service, key):
        return self.data.get((service, key))

    def set_password(self, service, key, value):
        self.data[(service, key)] = value

    def delete_password(self, service, key):
        if (service, key) not in self.data:
            raise KeyError(key)
        del self.data[(service, key)]


def _conn(name, kind=Kind.POSTGRES):
    return Connection.new(name, kind)


def test_store_roundtrip(tmp_path):
    path = tmp_path / "connections.json"
    a, b = _conn("a"), _conn("b", Kind.SSH_DOCKER)
    s = ConnectionStore(path)
    s.upsert(a)
    s.upsert(b)
    assert ConnectionStore(path).load() == [a, b]
    a.name = "renamed"
    s.upsert(a)
    assert [c.name for c in ConnectionStore(path).load()] == ["renamed", "b"]
    assert s.get(b.id) == b and s.get("nope") is None
    s.delete(a.id)
    assert ConnectionStore(path).load() == [b]
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_store_missing_file_is_empty(tmp_path):
    assert ConnectionStore(tmp_path / "none.json").load() == []


def test_store_skips_corrupt_entries(tmp_path, caplog):
    path = tmp_path / "connections.json"
    good = _conn("good")
    bad_env = good.to_dict() | {"id": "x", "env_names": None}
    path.write_text(json.dumps({"connections": [{"id": "y", "name": "nokind"}, bad_env, good.to_dict()]}))
    with caplog.at_level(logging.WARNING):
        assert ConnectionStore(path).load() == [good]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_store_unreadable_json_is_empty(tmp_path):
    path = tmp_path / "connections.json"
    path.write_text("{not json")
    assert ConnectionStore(path).load() == []


def test_store_file_has_no_secrets(tmp_path):
    secret = "p@ss'$x"
    conn = _conn("a")
    Secrets(FakeBackend()).set(conn.id, "pg_password", secret)
    path = tmp_path / "connections.json"
    ConnectionStore(path).save([conn])
    assert secret not in path.read_text()


def test_default_path_uses_config_dir():
    assert ConnectionStore().path == store.config_dir() / "connections.json"


def test_secrets_roundtrip_and_delete_all():
    backend = FakeBackend()
    secrets = Secrets(backend)
    assert secrets.available
    secrets.set("c1", "pg_password", "a")
    secrets.set("c1", "ssh_password", "b")
    assert secrets.get("c1", "pg_password") == "a"
    assert backend.data[("odoo_database_tool", "c1:pg_password")] == "a"
    secrets.delete_all("c1")
    assert secrets.get("c1", "pg_password") is None and secrets.get("c1", "ssh_password") is None


def test_secrets_fallback_memory():
    secrets = Secrets(backend=FailKeyring())
    assert secrets.available is False
    secrets.set("c1", "pg_password", "pw")
    assert secrets.get("c1", "pg_password") == "pw"
    secrets.delete_all("c1")
    assert secrets.get("c1", "pg_password") is None


def test_secrets_fallback_on_probe_exception():
    class Broken(FakeBackend):
        def get_password(self, service, key):
            raise RuntimeError("locked")

    secrets = Secrets(Broken())
    assert secrets.available is False
    secrets.set("c1", "pg_password", "pw")
    assert secrets.get("c1", "pg_password") == "pw"


def test_askpass_main_prints_secret(capsys):
    backend = FakeBackend()
    Secrets(backend).set("c1", "ssh_password", "s3cr3t")
    assert askpass_main({ASKPASS_ENV: "c1:ssh_password"}, backend=backend) == 0
    assert capsys.readouterr().out == "s3cr3t\n"


def test_askpass_main_without_stdout_writes_fd1(monkeypatch):
    backend = FakeBackend()
    Secrets(backend).set("c1", "ssh_password", "s3cr3t")
    written = []
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(store.os, "write", lambda fd, data: written.append((fd, data)) or len(data))
    assert askpass_main({ASKPASS_ENV: "c1:ssh_password"}, backend=backend) == 0
    assert written == [(1, b"s3cr3t\n")]


def test_askpass_main_missing(capsys):
    assert askpass_main({ASKPASS_ENV: "c1:ssh_password"}, backend=FakeBackend()) == 1
    assert askpass_main({}, backend=FakeBackend()) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.skipif(os.name != "posix", reason="shell wrapper")
def test_askpass_program_writes_wrapper_once(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    path = store.askpass_program()
    with open(path) as fh:
        text = fh.read()
    assert text == f'#!/bin/sh\nexec "{sys.executable}" -m odoo_database_tool "$@"\n'
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700
    mtime = os.stat(path).st_mtime_ns
    assert store.askpass_program() == path
    assert os.stat(path).st_mtime_ns == mtime


def test_askpass_program_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert store.askpass_program() == sys.executable


def test_secrets_delete_single_field():
    from odoo_database_tool.store import Secrets

    class KR:
        def __init__(self):
            self.d = {}
        def get_password(self, s, k):
            return self.d.get((s, k))
        def set_password(self, s, k, v):
            self.d[(s, k)] = v
        def delete_password(self, s, k):
            self.d.pop((s, k), None)

    sec = Secrets(KR())
    sec.set("c", "pg_password", "a")
    sec.set("c", "ssh_password", "b")
    sec.delete("c", "pg_password")
    sec.delete("c", "pg_password")  # missing entry is fine
    assert sec.get("c", "pg_password") is None and sec.get("c", "ssh_password") == "b"
