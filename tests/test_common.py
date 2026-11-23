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
    assert "0.1.0" in capsys.readouterr().out
