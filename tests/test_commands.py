from __future__ import annotations

import shlex

import pytest
from keyring.backends.fail import Keyring as FailKeyring

from odoo_database_tool import ssh
from odoo_database_tool.commands import CommandBuilder
from odoo_database_tool.models import Connection, Kind
from odoo_database_tool.store import Secrets
from odoo_database_tool.tools import ToolInfo


class FakeSettings:
    def __init__(self, missing=()):
        self.missing = set(missing)

    def tool(self, name):
        return None if name in self.missing else ToolInfo(name, f"/pg/bin/{name}", "17.2")


@pytest.fixture
def secrets():
    return Secrets(backend=FailKeyring())  # in-memory


@pytest.fixture
def builder(secrets):
    return CommandBuilder(FakeSettings(), secrets, "ssh")


@pytest.fixture
def docker_conn():
    c = Connection.new("staging", Kind.SSH_DOCKER)
    c.ssh.alias = "my-server"
    c.container = "odoo-web-1"
    return c


@pytest.fixture
def host_conn():
    c = Connection.new("db host", Kind.SSH_HOST)
    c.ssh.alias = "db-host"
    c.pg.user = "odoo"
    c.filestore_root = "/srv/odoo data/filestore"
    return c


@pytest.fixture
def local_conn():
    c = Connection.new("local", Kind.LOCAL)
    c.pg.user = "odoo18"
    return c


@pytest.fixture
def pg_conn():
    c = Connection.new("rds", Kind.POSTGRES)
    c.pg.host = "db.example.com"
    c.pg.user = "odoo"
    c.pg.sslmode = "require"
    return c


# --- brief tests (verbatim) ---

def test_docker_dump_streams_without_tty(builder, docker_conn):
    c = builder.dump_db(docker_conn, "odoo_dev")
    remote = c.argv[-1]
    assert " -t " not in f" {remote} " and "docker exec -e LC_ALL=C" in remote and "pg_dump" in remote and "-Fc" in remote
    assert c.stdin is None and "PGPASSWORD" not in c.env


def test_docker_uses_configured_env_names(builder, docker_conn):
    docker_conn.env_names = {"host": "DB_HOST", "port": "DB_PORT", "user": "DB_USER", "password": "DB_PASS"}
    assert '"$DB_PASS"' in builder.dump_db(docker_conn, "x").argv[-1]


def test_docker_odoo_conf_fallback(builder, docker_conn):
    docker_conn.odoo_conf_path = "/etc/odoo/odoo.conf"
    r = builder.psql(docker_conn, "postgres", "select 1").argv[-1]
    assert "/etc/odoo/odoo.conf" in r and "db_password" in r


def test_ssh_host_password_goes_to_stdin_not_argv(builder, host_conn, secrets):
    secrets.set(host_conn.id, "pg_password", "p'a$s \"w")
    c = builder.dump_db(host_conn, "db1")
    assert all("p'a$s" not in a for a in c.argv) and c.stdin == b"p'a$s \"w\n" and "read -r PGPASSWORD" in c.argv[-1]


def test_ssh_host_pg_os_user_uses_sudo(builder, host_conn):
    host_conn.pg_os_user = "postgres"
    assert "sudo -n -u postgres pg_dump" in builder.dump_db(host_conn, "db1").argv[-1]


def test_local_dump_password_in_env_only(builder, local_conn, secrets):
    secrets.set(local_conn.id, "pg_password", "s3cr3t")
    c = builder.dump_db(local_conn, "db1")
    assert c.env["PGPASSWORD"] == "s3cr3t" and "s3cr3t" not in " ".join(c.argv)


def test_tunnel_override_replaces_host_port(builder, pg_conn):
    a = builder.dump_db(pg_conn, "db1", pg_override=("127.0.0.1", 50123)).argv
    assert a[a.index("-h")+1] == "127.0.0.1" and a[a.index("-p")+1] == "50123"


def test_rejects_bad_db_name(builder, docker_conn):
    with pytest.raises(ValueError):
        builder.dump_db(docker_conn, "x; rm -rf /")


def test_restore_commands(builder, local_conn, tmp_path):
    a = builder.pg_restore(local_conn, "db1", tmp_path / "a b.dump", "odoo18", 4).argv
    for flag in ("--no-owner", "--no-privileges", "--role=odoo18", "-j"):
        assert flag in a
    assert str(tmp_path / "a b.dump") in a
    c = builder.createdb(local_conn, "db1", "odoo18", "C", "C").argv
    assert "template0" in c and "--lc-collate=C" in c


# --- additional coverage ---

def test_docker_odoo_conf_positional_order(builder, docker_conn):
    docker_conn.odoo_conf_path = "/etc/odoo/odoo.conf"
    r = builder.psql(docker_conn, "postgres", "select 1").argv[-1]
    assert r.endswith(" sh /etc/odoo/odoo.conf postgres 'select 1'")
    d = builder.dump_db(docker_conn, "db1").argv[-1]
    assert d.endswith(" sh /etc/odoo/odoo.conf db1") and '-Fc -d "$1"' in d


def test_docker_odoo_conf_leaves_host_to_socket(builder, docker_conn):
    docker_conn.odoo_conf_path = "/etc/odoo/odoo.conf"
    r = builder.psql(docker_conn, "postgres", "select 1").argv[-1]
    assert "localhost" not in r and "PGPORT=${p:-5432}" in r
    assert 'if [ -n "$h" ]; then PGHOST=$h; export PGHOST; fi' in r


def test_docker_db_and_sql_are_positional_args(builder, docker_conn):
    r = builder.psql(docker_conn, "odoo_dev", "select 'a'").argv[-1]
    assert r.startswith("docker exec -e LC_ALL=C odoo-web-1 sh -c ")
    assert r.endswith(" sh odoo_dev " + shlex.quote("select 'a'"))
    assert "-Atc" in r and "-X" in r and "-w" in r and '"$1"' in r and '"$2"' in r


def test_docker_dump_uses_nice_and_exec(builder, docker_conn):
    r = builder.dump_db(docker_conn, "db1").argv[-1]
    assert "exec nice -n 10 pg_dump" in r and "-w" in r


def test_docker_use_sudo_prefixes_docker(builder, docker_conn):
    docker_conn.ssh.use_sudo = True
    assert builder.dump_db(docker_conn, "db1").argv[-1].startswith("sudo -n docker exec -e LC_ALL=C ")
    assert builder.container_running(docker_conn).argv[-1] == \
        "sudo -n docker inspect -f '{{.State.Running}}' odoo-web-1"


def test_container_running_without_sudo(builder, docker_conn):
    c = builder.container_running(docker_conn)
    assert c.argv[-1] == "docker inspect -f '{{.State.Running}}' odoo-web-1"
    assert c.argv[-2] == "my-server"


@pytest.mark.parametrize("names", [
    {"password": "PASS;rm"}, {"host": "1HOST"}, {"user": "$USER"}, {"port": ""},
])
def test_rejects_bad_env_names(builder, docker_conn, names):
    docker_conn.env_names = {**docker_conn.env_names, **names}
    with pytest.raises(ValueError):
        builder.dump_db(docker_conn, "db1")


def test_rejects_bad_container(builder, docker_conn):
    docker_conn.container = "ctr; reboot"
    with pytest.raises(ValueError):
        builder.container_running(docker_conn)


def test_rejects_bad_odoo_conf_path(builder, docker_conn):
    docker_conn.odoo_conf_path = "etc/odoo.conf"
    with pytest.raises(ValueError):
        builder.psql(docker_conn, "postgres", "select 1")


def test_docker_pg_dump_version(builder, docker_conn):
    assert builder.pg_dump_version(docker_conn).argv[-1] == \
        "docker exec -e LC_ALL=C odoo-web-1 pg_dump --version"


def test_ssh_host_quotes_pg_values(builder, host_conn):
    host_conn.pg.user = "odoo user"
    host_conn.pg.host = "db host"
    r = builder.psql(host_conn, "db1", "select 1").argv[-1]
    assert "-U 'odoo user'" in r and "-h 'db host'" in r and "-Atc 'select 1'" in r
    assert "read -r PGPASSWORD" not in r


def test_ssh_host_without_password_has_no_stdin(builder, host_conn):
    c = builder.dump_db(host_conn, "db1")
    assert c.stdin is None and "exec nice -n 10 pg_dump" in c.argv[-1]


def test_ssh_host_pg_os_user_password_via_sudo_stdin(builder, host_conn, secrets):
    host_conn.pg_os_user = "postgres"
    secrets.set(host_conn.id, "pg_password", "p'a$s \"w")
    c = builder.dump_db(host_conn, "db1")
    assert all("p'a$s" not in a for a in c.argv) and c.stdin == b"p'a$s \"w\n"
    assert "sudo -n -u postgres sh -c " in c.argv[-1]
    assert c.argv[-1].endswith(" sh pg_dump -w -U odoo -Fc -d db1")


def test_ssh_host_pg_os_user_local_host_uses_socket(builder, host_conn):
    host_conn.pg_os_user = "postgres"
    host_conn.pg.host = "localhost"
    r = builder.psql(host_conn, "db1", "select 1").argv[-1]
    assert " -h " not in r and " -p " not in r
    host_conn.pg.port = 5433
    assert " -p 5433 " in builder.psql(host_conn, "db1", "select 1").argv[-1]


def test_ssh_host_pg_os_user_remote_host_keeps_h(builder, host_conn):
    host_conn.pg_os_user = "postgres"
    host_conn.pg.host = "10.0.0.5"
    assert " -h 10.0.0.5 -p 5432 " in builder.psql(host_conn, "db1", "select 1").argv[-1]


def test_kind_label_in_errors(builder, docker_conn):
    with pytest.raises(ValueError, match="SSH \\+ Docker"):
        builder.dropdb(docker_conn, "db1")


def test_ssh_host_rejects_bad_pg_os_user(builder, host_conn):
    host_conn.pg_os_user = "postgres; id"
    with pytest.raises(ValueError):
        builder.dump_db(host_conn, "db1")


def test_ssh_host_pg_dump_version_uses_os_user(builder, host_conn):
    host_conn.pg_os_user = "postgres"
    assert builder.pg_dump_version(host_conn).argv[-1] == "sudo -n -u postgres pg_dump --version"


def test_filestore_stats_docker(builder, docker_conn):
    r = builder.filestore_stats(docker_conn, "db1").argv[-1]
    assert r.startswith("docker exec -e LC_ALL=C odoo-web-1 sh -c ")
    assert r.endswith(" sh /var/lib/odoo/filestore db1") and "du -sk" in r and "wc -l" in r


def test_filestore_stats_ssh_host_sudo_quotes_root(builder, host_conn):
    host_conn.ssh.use_sudo = True
    r = builder.filestore_stats(host_conn, "db1").argv[-1]
    assert r.startswith("sudo -n sh -c ") and r.endswith(" sh '/srv/odoo data/filestore' db1")


def test_filestore_tar_docker_and_gzip(builder, docker_conn):
    assert builder.filestore_tar(docker_conn, "db1", False).argv[-1] == \
        "docker exec -e LC_ALL=C odoo-web-1 nice -n 10 tar cf - -C /var/lib/odoo/filestore db1"
    assert " tar czf - " in builder.filestore_tar(docker_conn, "db1", True).argv[-1]


def test_filestore_tar_ssh_host(builder, host_conn):
    assert builder.filestore_tar(host_conn, "db1", False).argv[-1] == \
        "nice -n 10 tar cf - -C '/srv/odoo data/filestore' db1"


def test_filestore_tar_local(builder, local_conn, tmp_path):
    local_conn.filestore_root = str(tmp_path / "file store")
    a = builder.filestore_tar(local_conn, "db1", False).argv
    assert a[1:] == ["cf", "-", "-C", str(tmp_path / "file store"), "db1"]


def test_filestore_unsupported_for_postgres(builder, pg_conn):
    with pytest.raises(ValueError):
        builder.filestore_tar(pg_conn, "db1", False)


def test_ssh_host_filestore_needs_root(builder, host_conn):
    host_conn.filestore_root = ""
    with pytest.raises(ValueError):
        builder.filestore_stats(host_conn, "db1")


def test_local_env_sslmode_and_no_password(builder, pg_conn, secrets):
    secrets.set(pg_conn.id, "pg_password", "pw")
    pg_conn.pg.no_password = True
    c = builder.psql(pg_conn, "postgres", "select 1")
    assert c.env == {"PGSSLMODE": "require"}
    assert c.argv[0] == "/pg/bin/psql" and c.argv[-2:] == ["-Atc", "select 1"]
    assert "-X" in c.argv and "-w" in c.argv and c.argv[c.argv.index("-h") + 1] == "db.example.com"


def test_missing_tool_is_reported(secrets, local_conn):
    b = CommandBuilder(FakeSettings(missing={"pg_dump"}), secrets, "ssh")
    with pytest.raises(ValueError, match="pg_dump not found"):
        b.dump_db(local_conn, "db1")


def test_restore_side_rejects_ssh_kinds(builder, docker_conn, host_conn):
    for conn in (docker_conn, host_conn):
        with pytest.raises(ValueError):
            builder.dropdb(conn, "db1")


def test_restore_side_argv(builder, local_conn, tmp_path):
    a = builder.dropdb(local_conn, "db1").argv
    assert a[0] == "/pg/bin/dropdb" and a[-1] == "db1" and "-w" in a
    c = builder.createdb(local_conn, "db1", "odoo18", "C", "C").argv
    assert c[c.index("-O") + 1] == "odoo18" and c[c.index("-E") + 1] == "UTF8" and c[-1] == "db1"
    assert "--lc-ctype=C" in c and c[c.index("-T") + 1] == "template0"
    r = builder.pg_restore(local_conn, "db1", tmp_path / "x.dump", "odoo18", 4).argv
    assert r[r.index("-j") + 1] == "4" and r[r.index("-d") + 1] == "db1"
    v = builder.vacuum_analyze(local_conn, "db1").argv
    assert v[0] == "/pg/bin/vacuumdb" and "--analyze-in-stages" in v
    f = builder.psql_file(local_conn, "db1", tmp_path / "n.sql").argv
    assert "ON_ERROR_STOP=1" in f and "--single-transaction" in f
    assert f[-2:] == ["-f", str(tmp_path / "n.sql")]


def test_restore_rejects_bad_db_name(builder, local_conn):
    with pytest.raises(ValueError):
        builder.createdb(local_conn, "a b", "odoo18", "C", "C")


def test_hostname_uses_ssh_or_tunnel(builder, docker_conn, pg_conn, monkeypatch):
    monkeypatch.setattr(ssh, "askpass_program", lambda: "/fake/askpass")
    assert builder.hostname(docker_conn).argv[-2:] == ["my-server", "hostname"]
    pg_conn.tunnel = docker_conn.ssh
    pg_conn.tunnel.use_password = True
    c = builder.hostname(pg_conn)
    assert c.argv[-1] == "hostname" and c.env["ODT_ASKPASS_KEY"] == f"{pg_conn.id}:tunnel_password"
