from __future__ import annotations

import io
import json
import re
import tarfile
from collections import namedtuple
from pathlib import Path

import pytest
from keyring.backends.fail import Keyring as FailKeyring

from odoo_database_tool import dump as dump_mod
from odoo_database_tool.archive import sha256_file
from odoo_database_tool.commands import CommandBuilder
from odoo_database_tool.dump import DumpJob, DumpOptions
from odoo_database_tool.jobs import JobContext, JobError
from odoo_database_tool.models import Connection, Kind, SshTarget
from odoo_database_tool.runner import CancelToken, Redactor, RunResult
from odoo_database_tool.store import Secrets
from odoo_database_tool.tools import ToolInfo

TOC = """;
; Archive created at 2026-11-23 10:00:00 UTC
;     dbname: mydb
;     Dumped from database version: 17.12
;     Dumped by pg_dump version: 17.2
;
3; 2615 2200 SCHEMA - public pg_database_owner
4; 3079 16385 EXTENSION - unaccent
5; 0 16400 TABLE DATA public res_users odoo
6; 0 16401 TABLE DATA public ir_module_module odoo
"""
TOC_EMPTY = TOC.split("5;")[0]
DUMP_BYTES = b"PGDMP" + b"\x00" * 600_000

Usage = namedtuple("Usage", "total used free")


class FakeSettings:
    def __init__(self, versions=None, missing=()):
        self.versions = {"pg_dump": "17.2", "pg_restore": "17.2", "psql": "17.2"}
        self.versions.update(versions or {})
        self.missing = set(missing)

    def tool(self, name):
        if name in self.missing:
            return None
        return ToolInfo(name, f"/pg/bin/{name}", self.versions.get(name, "17.2"))


class FakeCapture:
    """Scripted run_capture: first rule whose key is in the joined argv wins."""

    def __init__(self, **overrides):
        self.rules = {
            "hostname": (0, "vm-prod-001\n", ""),
            "docker inspect": (0, "true\n", ""),
            "server_version": (0, "17.12|1048576\n", ""),
            "pg_database_size(datname)": (0, "mydb|1048576\nother db|2048\n", ""),
            "latest_version": (0, "18.0.1.3\n", ""),
            "--version": (0, "pg_dump (PostgreSQL) 17.2\n", ""),
            "find . -type f": (0, "       3\n12\n", ""),
            "pg_restore -l": (0, TOC, ""),
        }
        self.rules.update(overrides)
        self.calls = []

    def __call__(self, cmd, *, timeout=60, redactor=None):
        joined = " ".join(cmd.argv)
        self.calls.append(joined)
        for key, result in self.rules.items():
            if key in joined:
                return result
        raise AssertionError(f"unexpected command: {joined}")

    def called(self, key):
        return [c for c in self.calls if key in c]


def make_tar_bytes(names):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        top = tarfile.TarInfo("mydb")
        top.type = tarfile.DIRTYPE
        tf.addfile(top)
        for name in names:
            ti = tarfile.TarInfo(f"mydb/{name}")
            ti.size = 4
            tf.addfile(ti, io.BytesIO(b"data"))
    return buf.getvalue()


class FakeRun:
    """Scripted run_stream: writes `payload` for the first matching key, returns a RunResult."""

    def __init__(self, **scripts):
        self.scripts = {"pg_dump": (DUMP_BYTES, 0, [], False),
                        "tar ": (make_tar_bytes(["0a/f1", "0a/f2", "ff/f3"]), 0, [], False)}
        self.scripts.update(scripts)
        self.calls = []

    def __call__(self, cmd, out_path, *, on_bytes=None, on_line=None, cancel=None,
                 redactor=None, tail=200):
        joined = " ".join(cmd.argv)
        self.calls.append(joined)
        for key, (payload, rc, stderr, cancelled) in self.scripts.items():
            if key in joined:
                Path(out_path).write_bytes(payload)
                if on_bytes:
                    on_bytes(len(payload))
                for line in stderr:
                    if on_line:
                        on_line(line)
                if cancelled and cancel is not None:
                    cancel.cancel()
                return RunResult(rc, list(stderr), len(payload), cancelled)
        raise AssertionError(f"unexpected stream: {joined}")


class FakeTunnel:
    opened = []

    def __init__(self, ssh_bin, t, conn_id, remote_host, remote_port, secret_field="tunnel_password"):
        self.args = (remote_host, remote_port)
        self.local_port = 0
        self.closed = False

    def __enter__(self):
        self.local_port = 55432
        FakeTunnel.opened.append(self)
        return self

    def __exit__(self, *exc):
        self.closed = True


@pytest.fixture
def secrets():
    return Secrets(backend=FailKeyring())  # in-memory


@pytest.fixture
def ctx():
    events = {"log": [], "progress": []}
    c = JobContext(log=events["log"].append,
                   progress=lambda pct, text: events["progress"].append((pct, text)),
                   cancel=CancelToken(), redactor=Redactor())
    c.events = events
    return c


@pytest.fixture(autouse=True)
def plenty_of_space(monkeypatch):
    monkeypatch.setattr(dump_mod.shutil, "disk_usage", lambda p: Usage(10**12, 0, 10**12))


@pytest.fixture(autouse=True)
def reset_tunnels():
    FakeTunnel.opened.clear()


@pytest.fixture
def docker_conn():
    c = Connection.new("My Prod", Kind.SSH_DOCKER)
    c.ssh.alias = "my-prod"
    c.ssh.expected_hostname = "vm-prod-001"
    c.container = "odoo-1"
    return c


def make_job(secrets, ctx, settings=None, capture=None, run=None):
    settings = settings or FakeSettings()
    builder = CommandBuilder(settings, secrets, "ssh")
    capture = capture or FakeCapture()
    run = run or FakeRun()
    job = DumpJob(builder, settings, ctx, run=run, capture=capture, tunnel_factory=FakeTunnel)
    return job, capture, run


def opts(conn, dest, db=True, fs=False, gzip=False):
    return DumpOptions(conn=conn, db="mydb", with_db=db, with_filestore=fs, gzip=gzip, dest=dest)


def by_name(checks):
    return {c.name: c for c in checks}


# --- preflight -----------------------------------------------------------------------

def test_preflight_fails_on_hostname_mismatch(secrets, ctx, docker_conn, tmp_path):
    job, cap, _ = make_job(secrets, ctx, capture=FakeCapture(hostname=(0, "vm-clone-002\n", "")))
    checks = by_name(job.preflight(opts(docker_conn, tmp_path)))
    assert checks["Hostname"].ok is False
    assert "vm-clone-002" in checks["Hostname"].detail
    assert not cap.called("docker inspect")  # nothing else runs against the wrong machine


def test_preflight_fails_when_pg_dump_older_than_server(secrets, ctx, docker_conn, tmp_path):
    cap = FakeCapture(**{"--version": (0, "pg_dump (PostgreSQL) 16.4\n", "")})
    job, _, _ = make_job(secrets, ctx, capture=cap)
    checks = by_name(job.preflight(opts(docker_conn, tmp_path)))
    assert checks["pg_dump version"].ok is False
    assert "16.4" in checks["pg_dump version"].detail and "17.12" in checks["pg_dump version"].detail
    assert checks["pg_dump version"].hint


def test_preflight_local_uses_local_pg_dump_version(secrets, ctx, tmp_path):
    conn = Connection.new("local", Kind.LOCAL)
    job, cap, _ = make_job(secrets, ctx, settings=FakeSettings({"pg_dump": "16.4"}))
    checks = by_name(job.preflight(opts(conn, tmp_path)))
    assert checks["pg_dump version"].ok is False
    assert not cap.called("--version")


# Need = DB size (1048576) + 1.1 x filestore (12 KB -> 13516 bytes)
@pytest.mark.parametrize("free, ok", [(1048576 + 13516, True), (1048576 + 13515, False), (1000, False)])
def test_preflight_free_space(secrets, ctx, docker_conn, tmp_path, monkeypatch, free, ok):
    seen = []
    monkeypatch.setattr(dump_mod.shutil, "disk_usage",
                        lambda p: seen.append(p) or Usage(10**12, 0, free))
    job, _, _ = make_job(secrets, ctx)
    checks = by_name(job.preflight(opts(docker_conn, tmp_path / "not yet created", fs=True)))
    assert checks["Free space"].ok is ok
    assert Path(seen[0]) == tmp_path  # nearest existing folder


def test_preflight_reports_container_not_running(secrets, ctx, docker_conn, tmp_path):
    job, cap, _ = make_job(secrets, ctx, capture=FakeCapture(**{"docker inspect": (0, "false\n", "")}))
    checks = by_name(job.preflight(opts(docker_conn, tmp_path)))
    assert checks["Container"].ok is False
    assert not cap.called("server_version")


def test_preflight_ssh_unreachable_hint(secrets, ctx, docker_conn, tmp_path):
    cap = FakeCapture(hostname=(255, "", "ssh: connect to host x port 22: Operation timed out"))
    job, _, _ = make_job(secrets, ctx, capture=cap)
    check = by_name(job.preflight(opts(docker_conn, tmp_path)))["Hostname"]
    assert check.ok is False and "VPN" in check.hint


def test_preflight_remote_filestore_stats_are_stripped(secrets, ctx, docker_conn, tmp_path):
    job, _, _ = make_job(secrets, ctx)
    checks = by_name(job.preflight(opts(docker_conn, tmp_path, db=False, fs=True)))
    assert checks["Filestore"].ok is True
    assert "3 files" in checks["Filestore"].detail


def test_preflight_missing_pg_restore_is_warning(secrets, ctx, docker_conn, tmp_path):
    job, _, _ = make_job(secrets, ctx, settings=FakeSettings(missing=("pg_restore",)))
    checks = by_name(job.preflight(opts(docker_conn, tmp_path)))
    assert checks["pg_restore"].ok is True and checks["pg_restore"].warning is True


def test_preflight_tunnel_failure_is_a_check(secrets, ctx, tmp_path):
    conn = Connection.new("rds", Kind.POSTGRES)
    conn.pg.host = "db.internal"
    conn.tunnel = SshTarget(alias="bastion")

    class BrokenTunnel(FakeTunnel):
        def __enter__(self):
            raise RuntimeError("SSH tunnel failed: the host is unreachable (unreachable)")

    settings = FakeSettings()
    job = DumpJob(CommandBuilder(settings, secrets, "ssh"), settings, ctx,
                  run=FakeRun(), capture=FakeCapture(), tunnel_factory=BrokenTunnel)
    checks = by_name(job.preflight(opts(conn, tmp_path)))
    assert checks["SSH tunnel"].ok is False


# --- run -----------------------------------------------------------------------------

def test_production_requires_confirmation(secrets, ctx, docker_conn, tmp_path):
    docker_conn.production = True
    job, cap, run = make_job(secrets, ctx)
    with pytest.raises(JobError, match="Production"):
        job.run(opts(docker_conn, tmp_path))
    assert cap.calls == [] and run.calls == []
    job.run(opts(docker_conn, tmp_path), confirmed_production=True)


def test_run_raises_when_checks_fail(secrets, ctx, docker_conn, tmp_path):
    job, _, run = make_job(secrets, ctx, capture=FakeCapture(hostname=(0, "other\n", "")))
    with pytest.raises(JobError, match="Hostname"):
        job.run(opts(docker_conn, tmp_path))
    assert run.calls == []


def test_dump_success_writes_file_sums_manifest(secrets, ctx, docker_conn, tmp_path):
    dest = tmp_path / "Odoo DB Dumps"
    job, cap, _ = make_job(secrets, ctx)
    result = job.run(opts(docker_conn, dest))
    assert len(result.files) == 1
    out = result.files[0]
    assert out.exists() and out.parent == dest
    assert out.name.startswith("mydb_my-prod_") and out.name.endswith(".dump")
    assert not list(dest.glob("*.part")) and not list(dest.glob("*.log"))
    assert (dest / "SHA256SUMS").read_text() == f"{sha256_file(out)}  {out.name}\n"
    man = json.loads((dest / (out.name + ".json")).read_text())
    assert man["kind"] == "db"
    assert man["connection"] == "My Prod" and man["connection_kind"] == "ssh_docker"
    assert man["server_version"] == "17.12" and man["pg_dump_version"] == "17.2"
    assert man["odoo_version"] == "18.0.1.3"
    assert man["size"] == len(DUMP_BYTES) and man["sha256"] == sha256_file(out)
    assert cap.called("pg_restore -l")
    assert any(re.fullmatch(r"Dumping database… [\d.]+ [KMG]?B \(\d+m \d\ds\)", text)
               and pct is None for pct, text in ctx.events["progress"])
    assert result.warnings == []


def test_dump_ssh_255_reports_connection_lost(secrets, ctx, docker_conn, tmp_path):
    run = FakeRun(pg_dump=(b"PGDMP partial", 255, ["client_loop: send disconnect: Broken pipe"], False))
    job, _, _ = make_job(secrets, ctx, run=run)
    with pytest.raises(JobError, match="Connection lost") as exc:
        job.run(opts(docker_conn, tmp_path))
    parts = list(tmp_path.glob("*.dump.part"))
    logs = list(tmp_path.glob("*.dump.log"))
    assert len(parts) == 1 and len(logs) == 1
    assert "Broken pipe" in logs[0].read_text()
    assert parts[0].name in str(exc.value)
    assert not list(tmp_path.glob("*.dump"))


@pytest.mark.parametrize("stderr, message", [
    ([], "Connection lost"),
    (["Permission denied (publickey)."], "authentication failed"),
])
def test_dump_ssh_255_classified(secrets, ctx, docker_conn, tmp_path, stderr, message):
    job, _, _ = make_job(secrets, ctx, run=FakeRun(pg_dump=(b"", 255, stderr, False)))
    with pytest.raises(JobError, match=message):
        job.run(opts(docker_conn, tmp_path))


def test_dump_failure_log_is_redacted(secrets, ctx, tmp_path):
    conn = Connection.new("local", Kind.LOCAL)
    secrets.set(conn.id, "pg_password", "pa$$ word")
    run = FakeRun(pg_dump=(b"", 1, ['FATAL: password "pa$$ word" rejected'], False))
    job, _, _ = make_job(secrets, ctx, run=run)
    with pytest.raises(JobError) as exc:
        job.run(opts(conn, tmp_path))
    assert "pa$$ word" not in str(exc.value)
    log = next(tmp_path.glob("*.dump.log")).read_text()
    assert "pa$$ word" not in log and "****" in log


def test_secrets_registered_in_redactor(secrets, ctx, tmp_path):
    conn = Connection.new("rds", Kind.POSTGRES)
    conn.pg.host = "db.internal"
    conn.tunnel = SshTarget(alias="bastion", use_password=True)
    secrets.set(conn.id, "pg_password", "pg-secret")
    secrets.set(conn.id, "tunnel_password", "tunnel-secret")
    job, _, _ = make_job(secrets, ctx)
    job.preflight(opts(conn, tmp_path))
    assert ctx.redactor("a pg-secret b tunnel-secret") == "a **** b ****"


def test_postgres_tunnel_used_for_queries_and_dump(secrets, ctx, tmp_path):
    conn = Connection.new("rds", Kind.POSTGRES)
    conn.pg.host = "db.internal"
    conn.tunnel = SshTarget(alias="bastion")
    job, cap, run = make_job(secrets, ctx)
    job.run(opts(conn, tmp_path))
    assert len(FakeTunnel.opened) == 1
    tunnel = FakeTunnel.opened[0]
    assert tunnel.args == ("db.internal", 5432) and tunnel.closed
    assert "-h 127.0.0.1 -p 55432" in cap.called("server_version")[0]
    assert "-h 127.0.0.1 -p 55432" in run.calls[0]


@pytest.mark.parametrize("toc_result, message", [
    ((0, TOC_EMPTY, ""), "no table data"),
    ((1, "", "pg_restore: error: input file does not appear to be a valid archive"),
     "valid archive"),
])
def test_verify_rejects_dump_without_table_data(secrets, ctx, docker_conn, tmp_path,
                                                toc_result, message):
    cap = FakeCapture(**{"pg_restore -l": toc_result})
    job, _, _ = make_job(secrets, ctx, capture=cap)
    with pytest.raises(JobError, match=message) as exc:
        job.run(opts(docker_conn, tmp_path))
    assert not list(tmp_path.glob("*.dump"))  # never under a final-looking name
    parts = list(tmp_path.glob("*.dump.part"))
    assert len(parts) == 1 and parts[0].name in str(exc.value)
    assert "Verification failed" in next(tmp_path.glob("*.dump.log")).read_text()
    assert cap.called("pg_restore -l")[0].endswith(".dump.part")
    assert not (tmp_path / "SHA256SUMS").exists()
    assert not list(tmp_path.glob("*.json"))


def test_missing_pg_restore_skips_verify_with_warning(secrets, ctx, docker_conn, tmp_path):
    job, cap, _ = make_job(secrets, ctx, settings=FakeSettings(missing=("pg_restore",)))
    result = job.run(opts(docker_conn, tmp_path))
    assert any("not verified" in w for w in result.warnings)
    assert not cap.called("pg_restore -l")


def test_filestore_tar_rc1_is_warning(secrets, ctx, docker_conn, tmp_path):
    names = ["0a/f1", "0a/f2", "ff/f3"]
    run = FakeRun(**{"tar ": (make_tar_bytes(names), 1, ["tar: ./0a/f2: file changed as we read it"], False)})
    job, _, _ = make_job(secrets, ctx, run=run)
    result = job.run(opts(docker_conn, tmp_path, db=False, fs=True))
    out = result.files[0]
    assert out.name.startswith("filestore_mydb_my-prod_") and out.name.endswith(".tar")
    assert out.exists() and not list(tmp_path.glob("*.part"))
    assert any("changed" in w for w in result.warnings)
    man = json.loads((tmp_path / (out.name + ".json")).read_text())
    assert man["kind"] == "filestore" and man["file_count"] == 3
    assert (tmp_path / (out.name + ".log")).exists()  # stderr kept for review
    assert any(pct is not None for pct, _ in ctx.events["progress"])


def test_filestore_tar_rc2_fails_and_keeps_part(secrets, ctx, docker_conn, tmp_path):
    run = FakeRun(**{"tar ": (b"partial", 2, ["tar: fatal"], False)})
    job, _, _ = make_job(secrets, ctx, run=run)
    with pytest.raises(JobError, match="tar"):
        job.run(opts(docker_conn, tmp_path, db=False, fs=True))
    assert len(list(tmp_path.glob("*.tar.part"))) == 1
    assert len(list(tmp_path.glob("*.tar.log"))) == 1


def test_unreadable_filestore_archive_kept_as_part(secrets, ctx, docker_conn, tmp_path):
    run = FakeRun(**{"tar ": (b"this is not a tar archive" * 40, 0, [], False)})
    job, _, _ = make_job(secrets, ctx, run=run)
    with pytest.raises(JobError, match="unreadable") as exc:
        job.run(opts(docker_conn, tmp_path, db=False, fs=True))
    assert not list(tmp_path.glob("*.tar"))
    parts = list(tmp_path.glob("*.tar.part"))
    assert len(parts) == 1 and parts[0].name in str(exc.value)
    assert "Verification failed" in next(tmp_path.glob("*.tar.log")).read_text()
    assert not (tmp_path / "SHA256SUMS").exists()


def test_filestore_failure_mentions_saved_db_dump(secrets, ctx, docker_conn, tmp_path):
    run = FakeRun(**{"tar ": (b"partial", 2, ["tar: fatal"], False)})
    job, _, _ = make_job(secrets, ctx, run=run)
    with pytest.raises(JobError, match="database dump was saved") as exc:
        job.run(opts(docker_conn, tmp_path, fs=True))
    db_file = next(tmp_path.glob("*.dump"))
    assert str(db_file) in str(exc.value)


def test_filestore_count_mismatch_warning(secrets, ctx, docker_conn, tmp_path):
    cap = FakeCapture(**{"find . -type f": (0, "100\n12\n", "")})
    job, _, _ = make_job(secrets, ctx, capture=cap)
    result = job.run(opts(docker_conn, tmp_path, db=False, fs=True))
    assert any("file count" in w for w in result.warnings)


def test_local_filestore_uses_python_tar(secrets, ctx, tmp_path):
    root = tmp_path / "filestore"
    (root / "mydb" / "0a").mkdir(parents=True)
    (root / "mydb" / "0a" / "f1").write_bytes(b"x" * 5000)
    (root / "mydb" / "0a" / "f2").write_bytes(b"y" * 10)
    conn = Connection.new("local", Kind.LOCAL)
    conn.filestore_root = str(root)
    dest = tmp_path / "out"
    job, cap, run = make_job(secrets, ctx)
    result = job.run(opts(conn, dest, db=False, fs=True, gzip=True))
    out = result.files[0]
    assert out.name.endswith(".tar.gz")
    with tarfile.open(out) as tf:
        assert sorted(m.name for m in tf.getmembers() if m.isfile()) == ["mydb/0a/f1", "mydb/0a/f2"]
    assert run.calls == [] and not cap.called("find") and not cap.called("tar")
    man = json.loads((dest / (out.name + ".json")).read_text())
    assert man["file_count"] == 2


def test_cancel_deletes_part(secrets, ctx, docker_conn, tmp_path):
    run = FakeRun(pg_dump=(b"PGDMP partial", -15, [], True))
    job, _, _ = make_job(secrets, ctx, run=run)
    with pytest.raises(JobError, match="[Cc]ancel"):
        job.run(opts(docker_conn, tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_db_and_filestore_share_timestamp(secrets, ctx, docker_conn, tmp_path):
    job, _, _ = make_job(secrets, ctx)
    result = job.run(opts(docker_conn, tmp_path, fs=True))
    db_file, fs_file = result.files
    assert fs_file.name == "filestore_" + db_file.name[:-len(".dump")] + ".tar"
    assert len((tmp_path / "SHA256SUMS").read_text().splitlines()) == 2


# --- list databases ------------------------------------------------------------------

def test_list_databases_parses_rows(secrets, ctx, docker_conn):
    job, cap, _ = make_job(secrets, ctx)
    assert job.list_databases(docker_conn) == [("mydb", 1048576), ("other db", 2048)]
    assert "not datistemplate" in cap.called("pg_database_size(datname)")[0]


def test_list_databases_failure_raises_job_error(secrets, ctx, docker_conn):
    cap = FakeCapture(**{"pg_database_size(datname)": (255, "", "Permission denied (publickey).")})
    job, _, _ = make_job(secrets, ctx, capture=cap)
    with pytest.raises(JobError, match="authentication"):
        job.list_databases(docker_conn)


# --- test_connection (Connections tab Test button) -------------------------------------

def test_test_connection_docker_all_checks(secrets, ctx, docker_conn):
    job, cap, _ = make_job(secrets, ctx)
    checks = by_name(job.test_connection(docker_conn))
    assert all(c.ok for c in checks.values())
    assert {"Hostname", "Container", "Database", "pg_dump version", "Database list",
            "pg_restore"} <= set(checks)
    assert "2 databases" in checks["Database list"].detail


def test_test_connection_hostname_mismatch_stops(secrets, ctx, docker_conn):
    job, cap, _ = make_job(secrets, ctx, capture=FakeCapture(hostname=(0, "clone\n", "")))
    checks = by_name(job.test_connection(docker_conn))
    assert checks["Hostname"].ok is False
    assert not cap.called("docker inspect")


def test_test_connection_container_not_running(secrets, ctx, docker_conn):
    job, cap, _ = make_job(secrets, ctx, capture=FakeCapture(**{"docker inspect": (0, "false\n", "")}))
    checks = by_name(job.test_connection(docker_conn))
    assert checks["Container"].ok is False
    assert "Database" not in checks


def test_test_connection_ssh_unreachable_hint(secrets, ctx, docker_conn):
    cap = FakeCapture(hostname=(255, "", "ssh: connect to host x port 22: Operation timed out"))
    job, _, _ = make_job(secrets, ctx, capture=cap)
    checks = by_name(job.test_connection(docker_conn))
    assert checks["Hostname"].ok is False and "VPN" in checks["Hostname"].hint


def test_test_connection_ssh_without_expected_hostname_still_checks_reachable(secrets, ctx):
    conn = Connection.new("host", Kind.SSH_HOST)
    conn.ssh.alias = "box"
    job, cap, _ = make_job(secrets, ctx)
    checks = by_name(job.test_connection(conn))
    assert checks["Hostname"].ok is True and cap.called("hostname")


def test_test_connection_local_uses_local_tools_only(secrets, ctx):
    conn = Connection.new("local", Kind.LOCAL)
    job, cap, _ = make_job(secrets, ctx, settings=FakeSettings(missing=["pg_restore"]))
    checks = by_name(job.test_connection(conn))
    assert "Hostname" not in checks
    assert checks["pg_restore"].warning is True
    assert checks["Database list"].ok is True


def test_test_connection_list_failure(secrets, ctx):
    conn = Connection.new("local", Kind.LOCAL)
    cap = FakeCapture(**{"pg_database_size(datname)": (2, "", "FATAL: password authentication failed")})
    job, _, _ = make_job(secrets, ctx, capture=cap)
    checks = by_name(job.test_connection(conn))
    assert checks["Database list"].ok is False


def test_test_connection_postgres_tunnel(secrets, ctx):
    conn = Connection.new("pg", Kind.POSTGRES)
    conn.pg.host = "db.internal"
    conn.tunnel = SshTarget(alias="bastion")
    job, _, _ = make_job(secrets, ctx)
    checks = by_name(job.test_connection(conn))
    assert checks["SSH tunnel"].ok is True and FakeTunnel.opened[0].closed


# --- final review fixes ------------------------------------------------------------------

def test_same_minute_dump_gets_suffix_instead_of_replacing(secrets, ctx, docker_conn, tmp_path,
                                                          monkeypatch):
    from datetime import datetime

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 23, 10, 15)

    monkeypatch.setattr(dump_mod, "datetime", Fixed)
    names = []
    for _ in range(3):
        job, _, _ = make_job(secrets, ctx)
        names += [p.name for p in job.run(opts(docker_conn, tmp_path, fs=True),
                                          confirmed_production=True).files]
    assert names == [
        "mydb_my-prod_20261123-1015.dump", "filestore_mydb_my-prod_20261123-1015.tar",
        "mydb_my-prod_20261123-1015-2.dump", "filestore_mydb_my-prod_20261123-1015-2.tar",
        "mydb_my-prod_20261123-1015-3.dump", "filestore_mydb_my-prod_20261123-1015-3.tar",
    ]
    for name in names:
        assert (tmp_path / name).is_file() and (tmp_path / (name + ".json")).is_file()
    sums = (tmp_path / "SHA256SUMS").read_text().splitlines()
    assert [line.split("  ")[1] for line in sums] == names
