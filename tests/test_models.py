from __future__ import annotations

import pytest

from odoo_database_tool.models import (
    Connection,
    Kind,
    PgEndpoint,
    SshTarget,
    is_local_target,
    validate_connection,
    validate_identifier,
    validate_remote_path,
)


def test_new_defaults():
    c = Connection.new("srv", Kind.SSH_DOCKER)
    assert c.ssh is not None and c.pg is None and c.effective_filestore_root() == "/var/lib/odoo/filestore"
    assert Connection.new("l", Kind.LOCAL).pg.host == "localhost"
    assert len(c.id) == 32


def test_env_names_not_shared():
    a, b = Connection.new("a", Kind.SSH_DOCKER), Connection.new("b", Kind.SSH_DOCKER)
    a.env_names["host"] = "X"
    assert b.env_names["host"] == "HOST"


@pytest.mark.parametrize("kind", list(Kind))
def test_roundtrip_dict(kind):
    c = Connection.new("n", kind)
    c.production = True
    c.tunnel = SshTarget(host="jump", use_sudo=True)
    c.container = "odoo"
    assert Connection.from_dict(c.to_dict()) == c
    assert isinstance(c.to_dict()["kind"], str)


def test_supports_filestore():
    assert not Connection.new("p", Kind.POSTGRES).supports_filestore()
    assert Connection.new("h", Kind.SSH_HOST).supports_filestore() is False
    c = Connection.new("h", Kind.SSH_HOST)
    c.filestore_root = "/srv/fs"
    assert c.supports_filestore()
    assert Connection.new("d", Kind.SSH_DOCKER).supports_filestore()
    assert Connection.new("l", Kind.LOCAL).supports_filestore()


def test_restore_target_rules():
    assert Connection.new("l", Kind.LOCAL).can_be_restore_target()
    p = Connection.new("p", Kind.POSTGRES)
    assert p.can_be_restore_target()
    p.production = True
    assert not p.can_be_restore_target()
    assert not Connection.new("d", Kind.SSH_DOCKER).can_be_restore_target()


def test_destination():
    assert SshTarget(alias="a", host="h").destination() == "a"
    assert SshTarget(host="h").destination() == "h"


@pytest.mark.parametrize("kind", [Kind.POSTGRES, Kind.LOCAL])
@pytest.mark.parametrize("host,tunnel,expected", [
    ("localhost", False, True), ("127.0.0.1", False, True), ("::1", False, True),
    ("", False, True), ("/tmp", False, True), ("localhost", True, False),
    ("10.0.0.5", False, False), ("db.example.com", False, False),
])
def test_is_local_target(kind, host, tunnel, expected):
    c = Connection.new("p", kind)
    c.pg = PgEndpoint(host=host)
    if tunnel:
        c.tunnel = SshTarget(host="jump")
    assert is_local_target(c) is expected


def test_is_local_target_ssh_kinds_remote():
    assert not is_local_target(Connection.new("d", Kind.SSH_DOCKER))
    assert is_local_target(Connection.new("l", Kind.LOCAL))


def test_validate_identifier():
    assert validate_identifier("odoo_dev", "database") == "odoo_dev"
    for bad in ["", "a b", "x;rm -rf", "-flag", "a'b", "db\n"]:
        with pytest.raises(ValueError, match="Invalid database"):
            validate_identifier(bad, "database")


def test_validate_remote_path():
    assert validate_remote_path("/etc/odoo/odoo.conf") == "/etc/odoo/odoo.conf"
    for bad in ["", "rel/path", "/a\nb", "/a\x00b"]:
        with pytest.raises(ValueError):
            validate_remote_path(bad)


def test_validate_connection_reports_missing_fields():
    errs = validate_connection(Connection.new("x", Kind.SSH_DOCKER))
    assert any("container" in e.lower() for e in errs) and any("ssh" in e.lower() for e in errs)


def test_validate_connection_ok_and_bad_values():
    c = Connection.new("x", Kind.SSH_DOCKER)
    c.ssh.host = "h"
    c.container = "odoo-1"
    assert validate_connection(c) == []
    c.container = "bad name;"
    assert any("container" in e.lower() for e in validate_connection(c))
    assert any("name" in e.lower() for e in validate_connection(Connection.new("", Kind.LOCAL)))


@pytest.mark.parametrize("host,ok", [("localhost", True), ("", True), ("127.0.0.1", True),
                                     ("::1", True), ("/var/run/postgresql", True),
                                     ("10.0.0.5", False), ("db.example.com", False)])
def test_validate_local_connection_host(host, ok):
    c = Connection.new("l", Kind.LOCAL)
    c.pg.host = host
    errors = validate_connection(c)
    assert (errors == []) is ok
    if not ok:
        assert any("Direct Postgres" in e for e in errors)


@pytest.mark.parametrize("field", ["host", "alias", "proxy_jump"])
def test_validate_rejects_ssh_values_starting_with_dash(field):
    c = Connection.new("x", Kind.SSH_DOCKER)
    c.container = "odoo"
    c.ssh.host = "h"
    setattr(c.ssh, field, "-oProxyCommand=evil")
    assert any("must not start with '-'" in e for e in validate_connection(c))
    p = Connection.new("p", Kind.POSTGRES)
    p.pg.host = "db"
    p.tunnel = SshTarget(host="jump")
    setattr(p.tunnel, field, " -x")
    assert any("SSH tunnel" in e and "'-'" in e for e in validate_connection(p))
