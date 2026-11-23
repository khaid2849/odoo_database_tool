from datetime import datetime

import pytest

from odoo_database_tool.__main__ import main
from odoo_database_tool.common import odoo_data_dir, slugify, timestamp


def test_odoo_data_dir_per_os(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "L"))
    assert odoo_data_dir("darwin") == tmp_path / "Library/Application Support/Odoo"
    assert odoo_data_dir("linux") == tmp_path / ".local/share/Odoo"
    assert odoo_data_dir("win32") == tmp_path / "L" / "OpenERP S.A" / "Odoo"


def test_slugify():
    assert slugify("My Server (SSH + Docker)") == "my-server-ssh-docker"
    assert slugify("  ") == "connection"


def test_timestamp():
    assert timestamp(datetime(2026, 11, 23, 9, 5)) == "20261123-0905"


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    from odoo_database_tool import __version__
    assert __version__ in capsys.readouterr().out


# --- frozen builds: child process environment ----------------------------------------------

def _frozen(monkeypatch, platform, frozen=True):
    import sys
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "platform", platform)


def test_child_env_linux_frozen_restores_original(monkeypatch):
    import os
    from odoo_database_tool.common import child_env

    _frozen(monkeypatch, "linux")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIxyz")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/local/lib")
    env = child_env({"PGPASSWORD": "pw"})
    assert env["LD_LIBRARY_PATH"] == "/usr/local/lib" and env["PGPASSWORD"] == "pw"
    assert os.environ["LD_LIBRARY_PATH"] == "/tmp/_MEIxyz"  # our own process is untouched
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG")
    assert "LD_LIBRARY_PATH" not in child_env()


def test_child_env_unchanged_when_not_frozen_or_not_linux(monkeypatch):
    from odoo_database_tool.common import child_env

    monkeypatch.setenv("LD_LIBRARY_PATH", "/bundle")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/orig")
    _frozen(monkeypatch, "linux", frozen=False)
    assert child_env()["LD_LIBRARY_PATH"] == "/bundle"
    _frozen(monkeypatch, "darwin")
    assert child_env()["LD_LIBRARY_PATH"] == "/bundle"
    assert child_env({"A": "1"})["A"] == "1"


def test_reset_dll_directory_only_frozen_windows(monkeypatch):
    import ctypes
    from odoo_database_tool.common import reset_frozen_dll_directory

    calls = []

    class Kernel32:
        def SetDllDirectoryW(self, value):
            calls.append(value)

    class WinDLL:
        kernel32 = Kernel32()

    monkeypatch.setattr(ctypes, "windll", WinDLL(), raising=False)
    _frozen(monkeypatch, "darwin")
    reset_frozen_dll_directory()
    _frozen(monkeypatch, "win32", frozen=False)
    reset_frozen_dll_directory()
    assert calls == []
    _frozen(monkeypatch, "win32")
    reset_frozen_dll_directory()
    assert calls == [None]


def test_main_resets_dll_directory_first(monkeypatch):
    from odoo_database_tool import __main__ as entry

    calls = []
    monkeypatch.setattr(entry, "reset_frozen_dll_directory", lambda: calls.append(1))
    with pytest.raises(SystemExit):
        main(["--version"])
    assert calls == [1]


def test_spawns_use_child_env(monkeypatch, tmp_path):
    import subprocess
    import sys
    from odoo_database_tool import ssh, tools
    from odoo_database_tool.models import SshTarget
    from odoo_database_tool.runner import run_capture
    from odoo_database_tool.ssh import Cmd

    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIbundle")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/orig/lib")
    _frozen(monkeypatch, "linux")
    rc, out, _ = run_capture(Cmd([sys.executable, "-c",
                                  "import os; print(os.environ.get('LD_LIBRARY_PATH'))"]))
    assert rc == 0 and out.strip() == "/orig/lib"

    seen = []

    def fake_run(argv, **kw):
        seen.append(kw["env"].get("LD_LIBRARY_PATH"))
        return subprocess.CompletedProcess(argv, 0, "OpenSSH_9.6p1", "")

    monkeypatch.setattr(ssh.subprocess, "run", fake_run)
    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    ssh.openssh_version("ssh")
    ssh.resolve_host_port("ssh", SshTarget(alias="box"))
    ssh.host_key_known("h", 22)
    tools._run_version("/x/pg_dump", "--version")
    assert seen == ["/orig/lib"] * 4

    popen_env = []

    class FakePopen:
        def __init__(self, argv, env=None, **kw):
            popen_env.append(env.get("LD_LIBRARY_PATH"))
            raise OSError("stop here")

    monkeypatch.setattr(ssh.subprocess, "Popen", FakePopen)
    with pytest.raises(OSError):
        ssh.Tunnel("ssh", SshTarget(host="h"), "c1", "db", 5432).__enter__()
    assert popen_env == ["/orig/lib"]
