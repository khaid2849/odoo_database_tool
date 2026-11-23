from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

from odoo_database_tool import tools
from odoo_database_tool.settings import AppSettings
from odoo_database_tool.tools import (
    ToolInfo, candidate_dirs, detect_ssh, detect_tool, parse_openssh_version,
    parse_pg_version, pg_major,
)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell scripts")


@pytest.mark.parametrize("text,expected", [
    ("pg_dump (PostgreSQL) 18.6 (Homebrew)", "18.6"),
    ("psql (PostgreSQL) 9.6.3", "9.6.3"),
    ("pg_dump (PostgreSQL) 17.4\n", "17.4"),
    ("garbage", None),
    ("", None),
])
def test_parse_pg_version(text, expected):
    assert parse_pg_version(text) == expected


@pytest.mark.parametrize("version,major", [
    ("17.12 (Ubuntu 17.12-1)", 17), ("9.6.3", 9), ("18.6", 18),
])
def test_pg_major(version, major):
    assert pg_major(version) == major


@pytest.mark.parametrize("text,expected", [
    ("OpenSSH_9.8p1, LibreSSL 3.3.6", (9, 8)),
    ("OpenSSH_for_Windows_8.1p1, LibreSSL 3.0.2", (8, 1)),
    ("nope", None),
])
def test_parse_openssh_version(text, expected):
    assert parse_openssh_version(text) == expected


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def test_candidate_dirs_highest_version_first(tmp_path, monkeypatch):
    for v in ("16", "18", "9"):
        (tmp_path / f"postgresql@{v}" / "bin").mkdir(parents=True)
    monkeypatch.setitem(tools.GLOB_ROOTS, "darwin", [str(tmp_path / "postgresql@*" / "bin")])
    monkeypatch.setenv("PATH", "/first/bin" + os.pathsep + "/second/bin")
    dirs = candidate_dirs("darwin")
    assert dirs[:2] == [Path("/first/bin"), Path("/second/bin")]
    versioned = [d.parent.name for d in dirs[2:]]
    assert versioned == ["postgresql@18", "postgresql@16", "postgresql@9"]


@posix_only
def test_detect_tool_with_fake_script(tmp_path, monkeypatch):
    fake = _script(tmp_path / "pg_dump", 'echo "pg_dump (PostgreSQL) 17.4"')
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(tools, "GLOB_ROOTS", {})
    info = detect_tool("pg_dump")
    assert info == ToolInfo("pg_dump", str(fake), "17.4")
    assert info.major == 17


@posix_only
def test_detect_tool_highest_version_wins_ties_earlier(tmp_path, monkeypatch):
    dirs = []
    for name, ver in (("a", "14.2"), ("b", "18.1"), ("c", "18.1")):
        d = tmp_path / name
        d.mkdir()
        _script(d / "pg_dump", f'echo "pg_dump (PostgreSQL) {ver}"')
        dirs.append(d)
    monkeypatch.setenv("PATH", os.pathsep.join(str(d) for d in dirs))
    monkeypatch.setattr(tools, "GLOB_ROOTS", {})
    info = detect_tool("pg_dump")
    assert info.major == 18 and info.path == str(dirs[1] / "pg_dump")


@posix_only
def test_detect_tool_override_wins_over_higher(tmp_path, monkeypatch):
    hi = tmp_path / "hi"
    hi.mkdir()
    _script(hi / "psql", 'echo "psql (PostgreSQL) 18.1"')
    low = _script(tmp_path / "low", 'echo "psql (PostgreSQL) 12.1"')
    monkeypatch.setenv("PATH", str(hi))
    assert detect_tool("psql", override=str(low)).major == 12


@posix_only
def test_detect_tool_override_and_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(tools, "GLOB_ROOTS", {})
    assert detect_tool("psql", override=str(tmp_path / "nope")) is None
    fake = _script(tmp_path / "custom", 'echo "psql (PostgreSQL) 16.2"')
    info = detect_tool("psql", override=str(fake))
    assert info is not None and info.major == 16


@posix_only
def test_detect_tool_ignores_unparsable(tmp_path, monkeypatch):
    _script(tmp_path / "psql", 'echo "hello"')
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(tools, "GLOB_ROOTS", {})
    assert detect_tool("psql") is None


@posix_only
def test_detect_ssh(tmp_path, monkeypatch):
    fake = _script(tmp_path / "ssh", 'echo "OpenSSH_9.8p1" >&2')
    monkeypatch.setenv("PATH", str(tmp_path))
    assert detect_ssh() == str(fake)
    assert detect_ssh(override=str(tmp_path / "missing")) is None


@pytest.fixture
def settings(tmp_path):
    return AppSettings(QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat))


def test_settings_defaults(settings):
    assert settings.dump_dir == Path.home() / "Odoo DB Dumps"
    assert settings.jobs == 4
    assert settings.theme == "system"
    assert settings.addons_sets == {}
    assert settings.tool_override("pg_dump") == ""
    assert settings.ssh_override == ""


def test_settings_roundtrip(tmp_path):
    ini = tmp_path / "s.ini"
    s = AppSettings(QSettings(str(ini), QSettings.IniFormat))
    s.addons_sets = {"18.0": ["/a b/odoo", "/x"]}
    s.set_tool_override("psql", "/opt/psql")
    s.dump_dir = tmp_path / "d"
    s.jobs = 2
    s.theme = "dark"
    s.ssh_override = "/usr/bin/ssh"
    s._q.sync()
    s2 = AppSettings(QSettings(str(ini), QSettings.IniFormat))
    assert s2.addons_sets == {"18.0": ["/a b/odoo", "/x"]}
    assert s2.tool_override("psql") == "/opt/psql"
    assert s2.dump_dir == tmp_path / "d"
    assert (s2.jobs, s2.theme, s2.ssh_override) == (2, "dark", "/usr/bin/ssh")


def test_settings_default_uses_env_ini():
    s = AppSettings()
    assert Path(s._q.fileName()) == Path(os.environ["ODT_SETTINGS"])


@posix_only
def test_tool_cache_invalidated_by_override(tmp_path, monkeypatch, settings):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(tools, "GLOB_ROOTS", {})
    calls = []
    real = tools.detect_tool
    monkeypatch.setattr("odoo_database_tool.settings.detect_tool",
                        lambda n, o="": calls.append((n, o)) or real(n, o))
    a = _script(tmp_path / "a", 'echo "psql (PostgreSQL) 15.1"')
    b = _script(tmp_path / "b", 'echo "psql (PostgreSQL) 16.1"')
    settings.set_tool_override("psql", str(a))
    assert settings.tool("psql").major == 15
    assert settings.tool("psql").major == 15
    assert len(calls) == 1
    settings.set_tool_override("psql", str(b))
    assert settings.tool("psql").major == 16
    assert len(calls) == 2
