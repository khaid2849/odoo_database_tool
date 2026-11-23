from __future__ import annotations

import io
import os
import re
import tarfile
from pathlib import Path

import pytest
from keyring.backends.fail import Keyring as FailKeyring

from odoo_database_tool.archive import Manifest, append_sha256sums, parse_toc, sha256_file, write_manifest
from odoo_database_tool.commands import CommandBuilder
from odoo_database_tool.jobs import JobContext, JobError
from odoo_database_tool.models import Connection, Kind, SshTarget
from odoo_database_tool.restore import (Activity, DumpInfo, RestoreJob, RestorePlan,
                                        count_restore_errors, filter_column_options,
                                        find_psql_meta_command, inspect_dump)
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
4; 3079 16385 EXTENSION - vector
5; 0 16400 TABLE DATA public res_users odoo
6; 0 16401 TABLE DATA public ir_module_module odoo
"""
TOC_EMPTY = TOC.split("5;")[0]
DUMP_BYTES = b"PGDMP" + b"\x00" * 1000
PG_RESTORE = ToolInfo("pg_restore", "/pg/bin/pg_restore", "17.2")
ACTIVITY_ROW = "4242|Odoo|odoo|10.0.0.9|2026-11-23 09:15\n"
PRE_DATA = (
    "SET statement_timeout = 0;\n"
    "SELECT pg_catalog.set_config('search_path', '', false);\n"
    "CREATE TABLE public.res_users (\n"
    "    id integer NOT NULL,\n"
    "    login character varying\n"
    ");\n"
    "ALTER TABLE ONLY public.res_users ALTER COLUMN login SET (n_distinct=-1);\n"
)
PG_ATTVER_LINE = "ALTER TABLE ONLY public.res_users ALTER COLUMN id SET (pg_attver=27258152);\n"


def file_arg(cmd):
    return Path(cmd.argv[cmd.argv.index("-f") + 1])


def writes_script(text):
    """FakeCapture rule for `pg_restore --section=pre-data -f <file>`: writes the script."""
    def rule(cmd):
        file_arg(cmd).write_text(text, encoding="utf-8")
        return 0, "", ""
    return rule


class FakeSettings:
    def tool(self, name):
        return ToolInfo(name, f"/pg/bin/{name}", "17.2")


class FakeCapture:
    """Scripted run_capture: first rule whose key is in the joined argv wins."""

    def __init__(self, events, **overrides):
        self.rules = {
            "--section=pre-data": writes_script(PRE_DATA),
            "pg_has_role": (0, "t\n", ""),
            "pg_stat_activity": (0, "", ""),
            "datcollate": (0, "en_US.UTF-8|en_US.UTF-8\n", ""),
            "pg_database_size(datname)": (0, "mydb|1048576\npostgres|8000\nother|2048\n", ""),
            "CREATE EXTENSION": (0, "", ""),
            "pg_database_size(current_database())": (0, "2097152\n", ""),
            "pg_tables": (0, "812\n", ""),
            "res_users": (0, "3\n", ""),
            "ir_module_module": (0, "120\n", ""),
            "ir_config_parameter": (0, "True\n", ""),
            "pg_restore -l": (0, TOC, ""),
        }
        self.rules.update(overrides)
        self.events = events
        self.calls = []

    def __call__(self, cmd, *, timeout=60, redactor=None):
        joined = " ".join(cmd.argv)
        self.calls.append(joined)
        self.events.append(joined)
        for key, result in self.rules.items():
            if key in joined:
                return result(cmd) if callable(result) else result
        raise AssertionError(f"unexpected command: {joined}")

    def called(self, key):
        return [c for c in self.calls if key in c]


class FakeRun:
    """Scripted run_stream: (rc, stderr lines, cancelled, error) for the first matching key."""

    def __init__(self, events, **scripts):
        self.scripts = {"dropdb": (0, [], False, ""), "createdb": (0, [], False, ""),
                        "ON_ERROR_STOP=0": (0, [], False, ""),
                        "pg_restore": (0, [], False, ""), "vacuumdb": (0, [], False, "")}
        self.scripts.update(scripts)
        self.events = events
        self.calls = []
        self.cancel_passed = {}
        self.scripts_seen = {}  # path -> content of a `-f` file when it ran

    def __call__(self, cmd, out_path, *, on_bytes=None, on_line=None, cancel=None,
                 redactor=None, tail=200):
        joined = " ".join(cmd.argv)
        self.calls.append(joined)
        self.events.append(joined)
        for key, (rc, stderr, cancelled, error) in self.scripts.items():
            if key in joined:
                self.cancel_passed[key] = cancel is not None
                assert out_path is None
                if "-f" in cmd.argv:
                    self.scripts_seen[file_arg(cmd)] = file_arg(cmd).read_text(encoding="utf-8")
                for line in stderr:
                    line = redactor(line) if redactor else line
                    if on_line:
                        on_line(line)
                if cancelled and cancel is not None:
                    cancel.cancel()
                return RunResult(rc, list(stderr), 0, bool(cancelled), error)
        raise AssertionError(f"unexpected stream: {joined}")

    def called(self, key):
        return [c for c in self.calls if key in c]


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


@pytest.fixture(autouse=True)
def reset_tunnels():
    FakeTunnel.opened.clear()


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


@pytest.fixture
def local_conn():
    c = Connection.new("Local", Kind.LOCAL)
    c.pg.user = "odoo"
    return c


@pytest.fixture
def remote_conn():
    c = Connection.new("Staging PG", Kind.POSTGRES)
    c.pg.host = "10.0.0.5"
    c.pg.user = "odoo"
    return c


@pytest.fixture
def dump_file(tmp_path):
    folder = tmp_path / "Odoo DB Dumps"
    folder.mkdir()
    p = folder / "mydb_my-prod_20261123-1000.dump"
    p.write_bytes(DUMP_BYTES)
    return p


def make_job(secrets, ctx, capture=None, run=None, events=None):
    events = [] if events is None else events
    settings = FakeSettings()
    capture = capture or FakeCapture(events)
    run = run or FakeRun(events)
    job = RestoreJob(CommandBuilder(settings, secrets, "ssh"), settings, ctx,
                     run=run, capture=capture, tunnel_factory=FakeTunnel)
    return job, capture, run, events


def make_plan(dump, target, db="mydb", is_new=False, fs_src=None, fs_root=None, toc=TOC):
    info = DumpInfo(dump, dump.stat().st_size, parse_toc(toc), "missing", None)
    return RestorePlan(info, target, db, is_new, "odoo", "C", "C", fs_src, fs_root, 4)


def tool_order(events):
    """Basenames of the local tools run, with CREATE EXTENSION shown as its own step."""
    out = []
    for e in events:
        if "--section=pre-data" in e:
            out.append("pg_restore pre-data")
        elif "ON_ERROR_STOP=0" in e:
            out.append("psql script")
        elif "CREATE EXTENSION" in e:
            out.append("psql CREATE EXTENSION " + re.search(r'"([^"]+)"', e).group(1))
        else:
            out.append(Path(e.split(" ")[0]).name)
    return out


def make_tar(path, top, names):
    with tarfile.open(path, "w") as tf:
        d = tarfile.TarInfo(top)
        d.type = tarfile.DIRTYPE
        tf.addfile(d)
        for name in names:
            ti = tarfile.TarInfo(f"{top}/{name}")
            ti.size = 4
            tf.addfile(ti, io.BytesIO(b"data"))
    return path


# --- inspect_dump ----------------------------------------------------------------------

def test_inspect_dump_rejects_old_pg_restore(dump_file):
    events = []
    old = ToolInfo("pg_restore", "/pg/bin/pg_restore", "16.4")
    with pytest.raises(JobError, match="16.4") as exc:
        inspect_dump(dump_file, old, capture=FakeCapture(events))
    assert "17.12" in str(exc.value)


def test_inspect_dump_sha_mismatch(dump_file):
    append_sha256sums(dump_file.parent, dump_file.name, "0" * 64)
    with pytest.raises(JobError, match="SHA256"):
        inspect_dump(dump_file, PG_RESTORE, capture=FakeCapture([]))


def test_inspect_dump_ok_with_sum_and_manifest(dump_file):
    append_sha256sums(dump_file.parent, dump_file.name, sha256_file(dump_file))
    write_manifest(dump_file.with_name(dump_file.name + ".json"), Manifest(
        kind="db", connection="My Prod", connection_kind="ssh_docker", database="mydb",
        created_at="2026-11-23T10:00:00+07:00", size=len(DUMP_BYTES), sha256="x"))
    cap = FakeCapture([])
    info = inspect_dump(dump_file, PG_RESTORE, capture=cap)
    assert info.path == dump_file and info.size == len(DUMP_BYTES)
    assert info.sha_status == "ok"
    assert info.toc.table_data == 2 and info.toc.extensions == ["vector"]
    assert info.manifest is not None and info.manifest.connection == "My Prod"
    assert cap.calls == [f"/pg/bin/pg_restore -l {dump_file}"]


def test_inspect_dump_without_sum_entry(dump_file):
    info = inspect_dump(dump_file, PG_RESTORE, capture=FakeCapture([]))
    assert info.sha_status == "missing" and info.manifest is None


@pytest.mark.parametrize("toc_result, message", [
    ((0, TOC_EMPTY, ""), "no table data"),
    ((1, "", "pg_restore: error: unsupported version (1.16) in file header"), "cannot read"),
])
def test_inspect_dump_rejects_bad_toc(dump_file, toc_result, message):
    with pytest.raises(JobError, match=message):
        inspect_dump(dump_file, PG_RESTORE, capture=FakeCapture([], **{"pg_restore -l": toc_result}))


def test_inspect_dump_missing_file(tmp_path):
    with pytest.raises(JobError, match="not found"):
        inspect_dump(tmp_path / "nope.dump", PG_RESTORE, capture=FakeCapture([]))


# --- refusals --------------------------------------------------------------------------

def test_execute_refuses_production_target(secrets, ctx, local_conn, dump_file):
    local_conn.production = True
    job, cap, run, events = make_job(secrets, ctx)
    with pytest.raises(JobError, match="Production"):
        job.execute(make_plan(dump_file, local_conn))
    assert events == []


def test_execute_refuses_ssh_kind(secrets, ctx, dump_file):
    conn = Connection.new("My Prod", Kind.SSH_DOCKER)
    job, _, _, events = make_job(secrets, ctx)
    with pytest.raises(JobError, match="Direct or Local Postgres"):
        job.execute(make_plan(dump_file, conn))
    assert events == []


def test_execute_refuses_when_connections_open(secrets, ctx, local_conn, dump_file):
    events = []
    cap = FakeCapture(events, pg_stat_activity=(0, ACTIVITY_ROW, ""))
    job, _, run, _ = make_job(secrets, ctx, capture=cap, events=events)
    with pytest.raises(JobError) as exc:
        job.execute(make_plan(dump_file, local_conn))
    msg = str(exc.value)
    for part in ("4242", "Odoo", "odoo", "10.0.0.9", "2026-11-23 09:15"):
        assert part in msg
    assert run.calls == []
    assert not list(dump_file.parent.glob("restore_*.log"))


def test_active_connections_query_and_parse(secrets, ctx, local_conn):
    events = []
    cap = FakeCapture(events, pg_stat_activity=(
        0, ACTIVITY_ROW + "77||postgres|local|2026-11-22 08:00\n", ""))
    job, _, _, _ = make_job(secrets, ctx, capture=cap, events=events)
    rows = job.active_connections(local_conn, "mydb")
    assert rows == [Activity(4242, "Odoo", "odoo", "10.0.0.9", "2026-11-23 09:15"),
                    Activity(77, "", "postgres", "local", "2026-11-22 08:00")]
    sql = cap.called("pg_stat_activity")[0]
    assert "datname = 'mydb'" in sql and "pid <> pg_backend_pid()" in sql
    assert "backend_type = 'client backend'" in sql
    assert "to_char(backend_start,'YYYY-MM-DD HH24:MI')" in sql
    assert " -d postgres " in sql


def test_remote_target_requires_typed_name(secrets, ctx, remote_conn, dump_file):
    job, cap, run, events = make_job(secrets, ctx)
    for text in ("", "MYDB", "mydb "):
        with pytest.raises(JobError, match="type the database name"):
            job.execute(make_plan(dump_file, remote_conn), confirmed_text=text)
    assert events == []
    outcome = job.execute(make_plan(dump_file, remote_conn), confirmed_text="mydb")
    assert outcome.status == "ok"


def test_local_target_needs_no_typed_name(secrets, ctx, local_conn, dump_file):
    job, _, _, _ = make_job(secrets, ctx)
    assert job.execute(make_plan(dump_file, local_conn)).status == "ok"


def test_remote_target_rejects_filestore(secrets, ctx, remote_conn, dump_file, tmp_path):
    src = make_tar(tmp_path / "fs.tar", "mydb", ["0a/f1"])
    job, _, _, events = make_job(secrets, ctx)
    plan = make_plan(dump_file, remote_conn, fs_src=src, fs_root=tmp_path / "filestore")
    with pytest.raises(JobError, match="only be restored on this machine"):
        job.execute(plan, confirmed_text="mydb")
    assert events == []


def test_execute_refuses_missing_dump(secrets, ctx, local_conn, dump_file):
    plan = make_plan(dump_file, local_conn)
    dump_file.unlink()
    job, _, run, _ = make_job(secrets, ctx)
    with pytest.raises(JobError, match="not found"):
        job.execute(plan)
    assert run.calls == []


def test_execute_refuses_bad_extension_name(secrets, ctx, local_conn, dump_file):
    toc = TOC.replace("EXTENSION - vector", 'EXTENSION - bad"name')
    job, _, run, _ = make_job(secrets, ctx)
    with pytest.raises(JobError, match="extension"):
        job.execute(make_plan(dump_file, local_conn, toc=toc))
    assert run.calls == []


# --- happy path and ordering -----------------------------------------------------------

def test_execute_happy_path_order(secrets, ctx, local_conn, dump_file):
    job, cap, run, events = make_job(secrets, ctx)
    outcome = job.execute(make_plan(dump_file, local_conn))
    steps = [s for s in tool_order(events) if s != "psql"]  # drop the read-only queries
    assert steps == ["pg_restore pre-data", "dropdb", "createdb", "psql CREATE EXTENSION vector",
                     "psql script", "pg_restore", "vacuumdb"]
    # the schema script is extracted first, so the connections check stays next to dropdb
    assert tool_order(events)[:2] == ["pg_restore pre-data", "psql"]
    assert "pg_stat_activity" in events[1]
    create = run.called("createdb")[0]
    assert "-T template0 -E UTF8 --lc-collate=C --lc-ctype=C -O odoo mydb" in create
    pre = cap.called("--section=pre-data")[0]
    assert pre.startswith("/pg/bin/pg_restore --section=pre-data --no-owner --no-privileges -f ")
    assert pre.endswith(f" {dump_file}") and " -d " not in pre
    schema = run.called("ON_ERROR_STOP=0")[0]
    assert "-X -b -v ON_ERROR_STOP=0 -d mydb -f " in schema
    assert "--single-transaction" not in schema
    restore = run.called("--section=data")[0]
    assert (f"--section=data --section=post-data --no-owner --no-privileges --role=odoo "
            f"-j 4 -d mydb {dump_file}") in restore
    assert outcome.status == "ok" and outcome.errors == 0
    assert outcome.log_path.parent == dump_file.parent
    assert re.fullmatch(r"restore_mydb_\d{8}-\d{4}\.log", outcome.log_path.name)
    assert outcome.quick == {"size": "2.0 MB", "tables": "812", "dump_table_data": "2",
                             "active_users": "3", "installed_modules": "120",
                             "neutralized": "True"}


def test_new_db_skips_drop(secrets, ctx, local_conn, dump_file):
    job, cap, run, events = make_job(secrets, ctx)
    outcome = job.execute(make_plan(dump_file, local_conn, db="fresh_db", is_new=True))
    assert outcome.status == "ok"
    assert not run.called("dropdb")
    assert not cap.called("pg_stat_activity")
    assert run.called("createdb")[0].endswith(" fresh_db")


def test_cancel_only_reaches_pg_restore(secrets, ctx, local_conn, dump_file):
    job, _, run, _ = make_job(secrets, ctx)
    job.execute(make_plan(dump_file, local_conn))
    assert run.cancel_passed == {"dropdb": False, "createdb": False, "ON_ERROR_STOP=0": True,
                                 "pg_restore": True, "vacuumdb": False}


def test_cancel_before_drop_touches_nothing(secrets, ctx, local_conn, dump_file):
    ctx.cancel.cancel()
    job, _, run, _ = make_job(secrets, ctx)
    outcome = job.execute(make_plan(dump_file, local_conn))
    assert outcome.status == "cancelled"
    assert run.calls == []
    assert not list(dump_file.parent.glob("restore_*.log"))


def test_cancel_during_restore_gives_partial(secrets, ctx, local_conn, dump_file, tmp_path):
    root = tmp_path / "filestore"
    (root / "mydb" / "0a").mkdir(parents=True)
    (root / "mydb" / "0a" / "old").write_bytes(b"old")
    src = make_tar(tmp_path / "fs.tar", "mydb", ["0b/new"])
    events = []
    run = FakeRun(events, pg_restore=(-15, ["pg_restore: processing data for table x"], True, ""))
    job, cap, _, _ = make_job(secrets, ctx, run=run, events=events)
    outcome = job.execute(make_plan(dump_file, local_conn, fs_src=src, fs_root=root))
    assert outcome.status == "partial"
    assert not run.called("vacuumdb")
    assert (root / "mydb" / "0a" / "old").exists() and not (root / "mydb" / "0b").exists()
    assert outcome.filestore_files == 0 and outcome.quick == {}
    assert not cap.called("pg_tables")
    assert outcome.log_path is not None and outcome.log_path.exists()
    assert any("partial" in w for w in outcome.warnings)


@pytest.mark.parametrize("script", [
    (127, [], False, "No such file or directory: '/pg/bin/pg_restore'"),
    (-9, ["killed"], False, ""),
])
def test_pg_restore_hard_failure_is_partial(secrets, ctx, local_conn, dump_file, script):
    events = []
    run = FakeRun(events, pg_restore=script)
    job, _, _, _ = make_job(secrets, ctx, run=run, events=events)
    outcome = job.execute(make_plan(dump_file, local_conn))
    assert outcome.status == "partial"
    assert not run.called("vacuumdb")


def test_pg_restore_errors_are_warning_not_abort(secrets, ctx, local_conn, dump_file):
    secrets.set(local_conn.id, "pg_password", "s3cr$t pw")
    stderr = ["perl: warning: Setting locale failed.",
              "pg_restore: error: could not execute query: ERROR:  role \"x\" does not exist",
              "pg_restore: error: password s3cr$t pw leaked?",
              "pg_restore: warning: errors ignored on restore: 2"]
    events = []
    run = FakeRun(events, pg_restore=(1, stderr, False, ""))
    job, _, _, _ = make_job(secrets, ctx, run=run, events=events)
    outcome = job.execute(make_plan(dump_file, local_conn))
    assert outcome.status == "ok" and outcome.errors == 2
    assert run.called("vacuumdb")
    text = outcome.log_path.read_text()
    assert "role \"x\" does not exist" in text
    assert "s3cr$t pw" not in text and "****" in text
    warning = next(w for w in outcome.warnings if "pg_restore" in w)
    assert "2 error" in warning and outcome.log_path.name in warning
    assert "role \"x\" does not exist" in warning


def test_vacuum_failure_is_warning(secrets, ctx, local_conn, dump_file):
    events = []
    run = FakeRun(events, vacuumdb=(1, ["vacuumdb: error: boom"], False, ""))
    job, _, _, _ = make_job(secrets, ctx, run=run, events=events)
    outcome = job.execute(make_plan(dump_file, local_conn))
    assert outcome.status == "ok"
    assert any("vacuumdb" in w for w in outcome.warnings)


def test_extension_failure_is_partial_before_restore(secrets, ctx, local_conn, dump_file):
    events = []
    cap = FakeCapture(events, **{"CREATE EXTENSION": (
        1, "", 'ERROR:  permission denied to create extension "vector"')})
    job, _, run, _ = make_job(secrets, ctx, capture=cap, events=events)
    outcome = job.execute(make_plan(dump_file, local_conn))
    assert outcome.status == "partial" and outcome.log_path is None
    text = "\n".join(outcome.warnings)
    assert ("Could not create extension vector: the connection user may lack the privilege, "
            "or the extension is not installed on the server.") in text
    assert 'permission denied to create extension "vector"' in text
    assert not run.called("pg_restore") and not run.called("vacuumdb")
    assert not cap.called("pg_tables")
    assert not list(dump_file.parent.glob("restore_*.log"))


@pytest.mark.parametrize("tool, message", [("dropdb", "dropdb failed"),
                                           ("createdb", "createdb failed")])
def test_drop_or_create_failure_stops(secrets, ctx, local_conn, dump_file, tool, message):
    events = []
    run = FakeRun(events, **{tool: (1, [f"{tool}: error: nope"], False, "")})
    job, _, _, _ = make_job(secrets, ctx, run=run, events=events)
    with pytest.raises(JobError, match=message) as exc:
        job.execute(make_plan(dump_file, local_conn))
    assert "nope" in str(exc.value)
    assert not run.called("pg_restore")


# --- filestore -------------------------------------------------------------------------

def test_filestore_extracted_into_target_db_folder(secrets, ctx, local_conn, dump_file, tmp_path):
    root = tmp_path / "filestore"
    target = root / "odoo_prod"
    (target / "ab").mkdir(parents=True)
    (target / "ab" / "stale").write_bytes(b"old")
    src = make_tar(tmp_path / "filestore_odoo_src.tar", "odoo_src", ["0a/f1", "0a/f2", "ff/f3"])
    job, _, _, _ = make_job(secrets, ctx)
    outcome = job.execute(make_plan(dump_file, local_conn, db="odoo_prod",
                                    fs_src=src, fs_root=root))
    assert outcome.status == "ok"
    assert (target / "0a" / "f1").read_bytes() == b"data"
    assert not (target / "ab").exists()
    assert not (root / "odoo_src").exists()
    assert outcome.filestore_files == 3


def test_filestore_folder_copied(secrets, ctx, local_conn, dump_file, tmp_path):
    src = tmp_path / "src" / "somedb"
    (src / "ab").mkdir(parents=True)
    (src / "ab" / "f1").write_bytes(b"1")
    root = tmp_path / "filestore"
    job, _, _, _ = make_job(secrets, ctx)
    outcome = job.execute(make_plan(dump_file, local_conn, fs_src=tmp_path / "src", fs_root=root))
    assert (root / "mydb" / "ab" / "f1").read_bytes() == b"1"
    assert outcome.filestore_files == 1


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlinked_filestore_target_refused_before_drop(secrets, ctx, local_conn, dump_file,
                                                       tmp_path):
    root = tmp_path / "filestore"
    root.mkdir()
    elsewhere = tmp_path / "precious"
    elsewhere.mkdir()
    (elsewhere / "keep").write_bytes(b"k")
    (root / "mydb").symlink_to(elsewhere, target_is_directory=True)
    src = make_tar(tmp_path / "fs.tar", "mydb", ["0a/f1"])
    job, _, run, _ = make_job(secrets, ctx)
    with pytest.raises(JobError, match="it is a symlink"):
        job.execute(make_plan(dump_file, local_conn, fs_src=src, fs_root=root))
    assert run.calls == []
    assert (elsewhere / "keep").exists()


def test_bad_filestore_source_refused_before_drop(secrets, ctx, local_conn, dump_file, tmp_path):
    job, _, run, _ = make_job(secrets, ctx)
    plan = make_plan(dump_file, local_conn, fs_src=tmp_path / "missing.tar",
                     fs_root=tmp_path / "filestore")
    with pytest.raises(JobError, match="does not exist"):
        job.execute(plan)
    assert run.calls == []


def test_filestore_source_inside_destination_refused(secrets, ctx, local_conn, dump_file,
                                                    tmp_path):
    root = tmp_path / "filestore"
    (root / "mydb" / "0a").mkdir(parents=True)
    (root / "mydb" / "0a" / "f").write_bytes(b"x")
    job, _, run, _ = make_job(secrets, ctx)
    with pytest.raises(JobError, match="overlaps"):
        job.execute(make_plan(dump_file, local_conn, fs_src=root / "mydb", fs_root=root))
    assert run.calls == [] and (root / "mydb" / "0a" / "f").exists()


def test_no_filestore_source_keeps_old_with_warning(secrets, ctx, local_conn, dump_file, tmp_path):
    root = tmp_path / "filestore"
    (root / "mydb" / "0a").mkdir(parents=True)
    (root / "mydb" / "0a" / "old").write_bytes(b"old")
    job, _, _, _ = make_job(secrets, ctx)
    outcome = job.execute(make_plan(dump_file, local_conn, fs_root=root))
    assert (root / "mydb" / "0a" / "old").exists()
    assert any("kept" in w and "may not match" in w for w in outcome.warnings)


# --- tunnel, secrets, helpers ----------------------------------------------------------

def test_tunnel_open_for_whole_execute(secrets, ctx, dump_file):
    conn = Connection.new("rds", Kind.POSTGRES)
    conn.pg.host = "db.internal"
    conn.pg.user = "odoo"
    conn.tunnel = SshTarget(alias="bastion")
    job, cap, run, events = make_job(secrets, ctx)
    outcome = job.execute(make_plan(dump_file, conn), confirmed_text="mydb")
    assert outcome.status == "ok"
    assert len(FakeTunnel.opened) == 1
    tunnel = FakeTunnel.opened[0]
    assert tunnel.args == ("db.internal", 5432) and tunnel.closed
    server = [e for e in events if "--section=pre-data" not in e]  # pre-data reads the file only
    assert server and all("-h 127.0.0.1 -p 55432" in e for e in server)


def test_secrets_registered_before_running(secrets, ctx, local_conn, dump_file):
    secrets.set(local_conn.id, "pg_password", "pg-secret")
    job, _, _, _ = make_job(secrets, ctx)
    job.execute(make_plan(dump_file, local_conn))
    assert ctx.redactor("x pg-secret y") == "x **** y"


def test_count_restore_errors_ignores_perl_warnings():
    log = "perl: warning: Setting locale failed.\npg_restore: error: could not execute query\npg_restore: warning: x\n"
    assert count_restore_errors(log) == 1


def test_quick_check_failures_give_question_mark(secrets, ctx, local_conn):
    events = []
    cap = FakeCapture(events, res_users=(1, "", 'ERROR:  relation "res_users" does not exist'),
                      ir_config_parameter=(0, "", ""))
    job, _, _, _ = make_job(secrets, ctx, capture=cap, events=events)
    quick = job.quick_check(local_conn, "mydb", parse_toc(TOC))
    assert quick["active_users"] == "?"
    assert quick["neutralized"] == ""
    assert quick["tables"] == "812" and quick["dump_table_data"] == "2"


def test_list_databases_skips_postgres(secrets, ctx, local_conn):
    job, _, _, _ = make_job(secrets, ctx)
    assert job.list_databases(local_conn) == [("mydb", 1048576), ("other", 2048)]


def test_db_locale(secrets, ctx, local_conn):
    job, cap, _, _ = make_job(secrets, ctx)
    assert job.db_locale(local_conn, "mydb") == ("en_US.UTF-8", "en_US.UTF-8")
    assert "datname = 'mydb'" in cap.called("datcollate")[0]


def test_summary_existing_db(secrets, ctx, local_conn, dump_file, tmp_path):
    root = tmp_path / "filestore"
    (root / "mydb").mkdir(parents=True)
    src = make_tar(tmp_path / "fs.tar", "mydb", ["0a/f1"])
    job, _, run, _ = make_job(secrets, ctx)
    lines = job.summary(make_plan(dump_file, local_conn, fs_src=src, fs_root=root))
    text = "\n".join(lines)
    assert "Drop database mydb" in text and "1.0 MB" in text
    assert "Create database mydb" in text and "owner odoo" in text
    assert dump_file.name in text and "4 parallel jobs" in text
    assert "vector" in text
    assert f"Delete filestore {root / 'mydb'}" in text
    assert run.calls == []


def test_summary_new_db_remote_leaves_filestore(secrets, ctx, remote_conn, dump_file):
    job, _, _, _ = make_job(secrets, ctx)
    lines = job.summary(make_plan(dump_file, remote_conn, db="fresh", is_new=True))
    text = "\n".join(lines)
    assert "Drop" not in text and "Create database fresh" in text
    assert "Filestore can only be restored on this machine" in text


def test_drop_incomplete(secrets, ctx, local_conn):
    job, _, run, _ = make_job(secrets, ctx)
    job.drop_incomplete(local_conn, "mydb")
    assert run.called("dropdb")[0].endswith(" mydb")
    local_conn.production = True
    with pytest.raises(JobError, match="Production"):
        job.drop_incomplete(local_conn, "mydb")


# --- fix round 1 ------------------------------------------------------------------------

@pytest.mark.parametrize("result, message", [
    ((0, "", ""), "Role odoo does not exist on the target"),
    ((0, "f\n", ""), "The connection user cannot act as role odoo"),
])
def test_owner_role_checked_before_drop(secrets, ctx, local_conn, dump_file, result, message):
    events = []
    cap = FakeCapture(events, pg_has_role=result)
    job, _, run, _ = make_job(secrets, ctx, capture=cap, events=events)
    with pytest.raises(JobError, match=message):
        job.execute(make_plan(dump_file, local_conn))
    assert run.calls == []
    sql = cap.called("pg_has_role")[0]
    assert "from pg_roles where rolname = 'odoo'" in sql


def test_pg_restore_fatal_exit_without_ignored_line_is_partial(secrets, ctx, local_conn,
                                                               dump_file, tmp_path):
    root = tmp_path / "filestore"
    (root / "mydb" / "0a").mkdir(parents=True)
    (root / "mydb" / "0a" / "old").write_bytes(b"old")
    src = make_tar(tmp_path / "fs.tar", "mydb", ["0b/new"])
    events = []
    run = FakeRun(events, pg_restore=(1, [
        "pg_restore: error: could not execute query: server closed the connection unexpectedly"],
        False, ""))
    job, cap, _, _ = make_job(secrets, ctx, run=run, events=events)
    outcome = job.execute(make_plan(dump_file, local_conn, fs_src=src, fs_root=root))
    assert outcome.status == "partial" and outcome.errors == 1
    assert not run.called("vacuumdb") and not cap.called("pg_tables")
    assert (root / "mydb" / "0a" / "old").exists() and not (root / "mydb" / "0b").exists()
    assert any("exit code 1" in w and "server closed the connection" in w
               for w in outcome.warnings)


def test_pg_restore_exit_0_with_racing_cancel_is_ok(secrets, ctx, local_conn, dump_file):
    events = []
    run = FakeRun(events, pg_restore=(0, [], True, ""))
    job, _, _, _ = make_job(secrets, ctx, run=run, events=events)
    outcome = job.execute(make_plan(dump_file, local_conn))
    assert outcome.status == "ok" and run.called("vacuumdb")


def test_non_filestore_destination_refused(secrets, ctx, local_conn, dump_file, tmp_path):
    root = tmp_path / "filestore"
    (root / "mydb").mkdir(parents=True)
    (root / "mydb" / "notes.txt").write_bytes(b"mine")
    src = make_tar(tmp_path / "fs.tar", "mydb", ["0a/f1"])
    job, _, run, _ = make_job(secrets, ctx)
    with pytest.raises(JobError, match="does not look like an Odoo filestore"):
        job.execute(make_plan(dump_file, local_conn, fs_src=src, fs_root=root))
    assert run.calls == []
    assert (root / "mydb" / "notes.txt").read_bytes() == b"mine"


def test_cancel_during_dropdb_still_creates_and_skips_restore(secrets, ctx, local_conn,
                                                             dump_file):
    events = []

    class CancellingRun(FakeRun):
        def __call__(self, cmd, out_path, **kw):
            result = super().__call__(cmd, out_path, **kw)
            if "dropdb" in cmd.argv[0]:
                ctx.cancel.cancel()  # user pressed Cancel while dropdb ran
            return result

    run = CancellingRun(events)
    job, _, _, _ = make_job(secrets, ctx, run=run, events=events)
    outcome = job.execute(make_plan(dump_file, local_conn))
    assert run.called("createdb") and not run.called("pg_restore")
    assert outcome.status == "partial"
    assert any("cancelled before the data restore" in w for w in outcome.warnings)


def test_refusal_precedence(secrets, ctx, remote_conn, dump_file, tmp_path):
    src = make_tar(tmp_path / "fs.tar", "mydb", ["0a/f1"])
    job, _, _, events = make_job(secrets, ctx)
    remote_conn.production = True
    with pytest.raises(JobError, match="Production"):
        job.execute(make_plan(dump_file, remote_conn, fs_src=src, fs_root=tmp_path / "fs"))
    remote_conn.production = False
    with pytest.raises(JobError, match="only be restored on this machine"):
        job.execute(make_plan(dump_file, remote_conn, fs_src=src, fs_root=tmp_path / "fs"),
                    confirmed_text="")
    assert events == []


def test_filestore_failure_after_delete_is_warning(secrets, ctx, local_conn, dump_file, tmp_path):
    root = tmp_path / "filestore"
    (root / "mydb" / "0a").mkdir(parents=True)
    (root / "mydb" / "0a" / "old").write_bytes(b"old")
    src = tmp_path / "bad.tar"
    with tarfile.open(src, "w") as tf:
        link = tarfile.TarInfo("mydb/0a/evil")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
    job, _, _, _ = make_job(secrets, ctx)
    outcome = job.execute(make_plan(dump_file, local_conn, fs_src=src, fs_root=root))
    assert outcome.status == "ok" and outcome.filestore_files == 0
    assert not (root / "mydb" / "0a" / "old").exists()
    assert any("Filestore restore failed" in w and "may be incomplete" in w
               for w in outcome.warnings)
    assert outcome.quick  # the quick check still runs


def test_late_stderr_line_after_log_close_does_not_raise(secrets, ctx, local_conn, dump_file):
    events = []

    class KeepLineCallback(FakeRun):
        def __call__(self, cmd, out_path, **kw):
            if "pg_restore" in cmd.argv[0]:
                self.on_line = kw.get("on_line")
            return super().__call__(cmd, out_path, **kw)

    run = KeepLineCallback(events)
    job, _, _, _ = make_job(secrets, ctx, run=run, events=events)
    outcome = job.execute(make_plan(dump_file, local_conn))
    run.on_line("pg_restore: late line from the reader thread")  # file already closed
    assert outcome.status == "ok"


# --- final review fixes ------------------------------------------------------------------

def tunnelled_conn(expected="db-box"):
    conn = Connection.new("rds", Kind.POSTGRES)
    conn.pg.host, conn.pg.user = "db.internal", "odoo"
    conn.tunnel = SshTarget(alias="bastion", expected_hostname=expected)
    return conn


def test_expected_hostname_mismatch_refuses_execute_before_anything(secrets, ctx, dump_file):
    events = []
    cap = FakeCapture(events, hostname=(0, "clone-box\n", ""))
    job, _, run, _ = make_job(secrets, ctx, capture=cap, events=events)
    with pytest.raises(JobError, match="clone-box.*db-box"):
        job.execute(make_plan(dump_file, tunnelled_conn()), confirmed_text="mydb")
    assert run.calls == [] and FakeTunnel.opened == []
    assert [e for e in events if "hostname" not in e] == []  # nothing but the hostname check
    assert not list(dump_file.parent.glob("restore_*.log"))


def test_expected_hostname_mismatch_refuses_drop_and_queries(secrets, ctx):
    events = []
    cap = FakeCapture(events, hostname=(0, "clone-box\n", ""))
    job, _, run, _ = make_job(secrets, ctx, capture=cap, events=events)
    conn = tunnelled_conn()
    with pytest.raises(JobError, match="expected 'db-box'"):
        job.drop_incomplete(conn, "mydb")
    for call in (lambda: job.list_databases(conn), lambda: job.db_locale(conn, "mydb"),
                 lambda: job.active_connections(conn, "mydb"),
                 lambda: job.quick_check(conn, "mydb", parse_toc(TOC))):
        with pytest.raises(JobError, match="clone-box"):
            call()
    assert run.calls == [] and FakeTunnel.opened == []
    assert all("hostname" in e for e in events)


def test_expected_hostname_match_runs_hostname_first(secrets, ctx, dump_file):
    events = []
    cap = FakeCapture(events, hostname=(0, "db-box\n", ""))
    job, _, run, _ = make_job(secrets, ctx, capture=cap, events=events)
    outcome = job.execute(make_plan(dump_file, tunnelled_conn()), confirmed_text="mydb")
    assert outcome.status == "ok"
    assert "hostname" in events[0] and "bastion" in events[0]
    assert run.called("dropdb")


def test_no_expected_hostname_skips_check(secrets, ctx, dump_file):
    job, cap, _, _ = make_job(secrets, ctx)
    assert job.execute(make_plan(dump_file, tunnelled_conn("")), confirmed_text="mydb").status == "ok"
    assert not [c for c in cap.called("hostname") if "--section=pre-data" not in c]


def test_unexpected_error_after_recreate_is_partial(secrets, ctx, local_conn, dump_file):
    secrets.set(local_conn.id, "pg_password", "pg-secret")
    job, _, run, _ = make_job(secrets, ctx)

    def broken(*args, **kwargs):
        raise OSError(28, "No space left on device pg-secret")

    job._restore_data = broken
    outcome = job.execute(make_plan(dump_file, local_conn))
    assert outcome.status == "partial"
    assert run.called("createdb") and not run.called("vacuumdb")
    text = "\n".join(outcome.warnings)
    assert "Restore stopped unexpectedly after the database was recreated" in text
    assert "No space left on device" in text and "pg-secret" not in text
    assert "mydb is partial" in text


def test_summary_remote_has_no_typed_confirmation_line(secrets, ctx, remote_conn, dump_file):
    job, _, _, _ = make_job(secrets, ctx)
    lines = job.summary(make_plan(dump_file, remote_conn))
    assert any("remote server" in line for line in lines)
    assert not any("to confirm" in line for line in lines)


# --- unknown column options and sectioned restore -------------------------------------------

KEPT_ALTERS = (
    "ALTER TABLE ONLY public.t ALTER COLUMN c SET (n_distinct=-1);\n"
    "ALTER TABLE ONLY public.t ALTER COLUMN c SET (n_distinct=0.5, n_distinct_inherited=-0.2);\n"
    "ALTER TABLE ONLY public.t ALTER COLUMN c SET NOT NULL;\n"
    "ALTER TABLE ONLY public.t ALTER COLUMN id SET DEFAULT nextval('public.t_id_seq'::regclass);\n"
    "ALTER TABLE ONLY public.t ALTER COLUMN c SET STATISTICS 500;\n"
    "ALTER TABLE ONLY public.t ALTER COLUMN c SET STORAGE EXTERNAL;\n"
    "ALTER TABLE public.t OWNER TO odoo;\n"
    "ALTER TABLE ONLY public.t SET (fillfactor=80);\n"
    "-- ALTER TABLE ONLY public.t ALTER COLUMN c SET (pg_attver=1);\n"
)


def test_filter_keeps_everything_without_unknown_options():
    text = PRE_DATA + KEPT_ALTERS + "CREATE INDEX x ON public.t USING btree (c);"
    assert filter_column_options(text) == (text, {})


@pytest.mark.parametrize("line, option", [
    ("ALTER TABLE ONLY public.access_control_button ALTER COLUMN id SET (pg_attver=27258152);",
     "pg_attver"),
    ('ALTER TABLE ONLY public."Weird Table" ALTER COLUMN "Weird Col" SET (pg_attver=3);',
     "pg_attver"),
    ('ALTER TABLE ONLY "my schema".t ALTER COLUMN "a""b" SET (pg_attver=3);', "pg_attver"),
    ("ALTER TABLE t ALTER COLUMN c SET (pg_attver=3);", "pg_attver"),
    ("ALTER TABLE ONLY public.t ALTER COLUMN c SET (n_distinct=-1, my_opt=on);", "my_opt"),
])
def test_filter_drops_statements_with_unknown_options(line, option):
    text = "CREATE TABLE public.t (c integer);\n" + line + "\n" + KEPT_ALTERS
    out, skipped = filter_column_options(text)
    assert out == "CREATE TABLE public.t (c integer);\n" + KEPT_ALTERS
    assert skipped == {option: 1}


def test_filter_counts_each_unknown_option_name():
    lines = [f"ALTER TABLE ONLY public.t{i} ALTER COLUMN id SET (pg_attver={i});\n"
             for i in range(3)]
    lines.append("ALTER TABLE ONLY public.t ALTER COLUMN c SET (pg_attver=1, other_opt=2);\n")
    out, skipped = filter_column_options("SET x = 1;\n" + "".join(lines) + "SELECT 1;")
    assert out == "SET x = 1;\nSELECT 1;"
    assert skipped == {"pg_attver": 4, "other_opt": 1}


def test_filter_never_drops_lines_it_cannot_parse():
    for line in ("ALTER TABLE ONLY public.t ALTER COLUMN c SET (pg_attver='a b');",
                 "ALTER TABLE ONLY public.t ALTER COLUMN c SET (pg_attver=1); SELECT 1;",
                 "  ALTER TABLE ONLY public.t ALTER COLUMN c SET (pg_attver=1);",
                 "ALTER FOREIGN TABLE public.t ALTER COLUMN c SET (n_distinct=1);"):
        assert filter_column_options(line + "\n") == (line + "\n", {})


def test_schema_script_is_filtered_and_runs_as_owner(secrets, ctx, local_conn, dump_file):
    events = []
    cap = FakeCapture(events, **{"--section=pre-data": writes_script(
        PRE_DATA + PG_ATTVER_LINE + PG_ATTVER_LINE.replace("COLUMN id", "COLUMN login"))})
    job, _, run, _ = make_job(secrets, ctx, capture=cap, events=events)
    outcome = job.execute(make_plan(dump_file, local_conn))
    assert outcome.status == "ok"
    [(path, script)] = run.scripts_seen.items()
    assert script == 'SET ROLE "odoo";\n' + PRE_DATA
    assert ("Skipped 2 column option(s) not supported by the target server: pg_attver (2)."
            in outcome.warnings)
    assert not path.exists() and not path.parent.exists()  # temp script cleaned up
    assert file_arg_from(cap.called("--section=pre-data")[0]) == path


def file_arg_from(joined):
    parts = joined.split(" ")
    return Path(parts[parts.index("-f") + 1])


def test_schema_script_failure_refuses_before_drop(secrets, ctx, local_conn, dump_file):
    events = []
    cap = FakeCapture(events, **{"--section=pre-data": (
        1, "", "pg_restore: error: could not read from input file: end of file")})
    job, _, run, _ = make_job(secrets, ctx, capture=cap, events=events)
    with pytest.raises(JobError, match="end of file") as exc:
        job.execute(make_plan(dump_file, local_conn))
    assert "Nothing was changed" in str(exc.value)
    assert run.calls == []
    assert not list(dump_file.parent.glob("restore_*.log"))
    assert not file_arg_from(cap.called("--section=pre-data")[0]).parent.exists()


def test_cancel_before_drop_cleans_the_schema_script(secrets, ctx, local_conn, dump_file):
    ctx.cancel.cancel()
    job, cap, run, _ = make_job(secrets, ctx)
    assert job.execute(make_plan(dump_file, local_conn)).status == "cancelled"
    assert run.calls == []
    assert not file_arg_from(cap.called("--section=pre-data")[0]).parent.exists()


def test_psql_and_pg_restore_errors_share_the_log(secrets, ctx, local_conn, dump_file):
    events = []
    run = FakeRun(events, **{
        "ON_ERROR_STOP=0": (0, ['psql:/tmp/x/pre_data.sql:12: ERROR:  unrecognized parameter '
                                '"other"', "LINE 1: ALTER TABLE ..."], False, ""),
        "pg_restore": (1, ["pg_restore: error: COPY failed for table \"t\"",
                           "pg_restore: warning: errors ignored on restore: 1"], False, "")})
    job, _, _, _ = make_job(secrets, ctx, run=run, events=events)
    outcome = job.execute(make_plan(dump_file, local_conn))
    assert outcome.status == "ok" and outcome.errors == 2
    text = outcome.log_path.read_text()
    assert text.index("unrecognized parameter") < text.index("COPY failed")
    warning = next(w for w in outcome.warnings if "2 error" in w)
    assert "unrecognized parameter" in warning and "COPY failed" in warning


@pytest.mark.parametrize("script, reason", [
    ((2, ["psql:/tmp/x/pre_data.sql:40: FATAL:  terminating connection"], False, ""),
     "psql stopped with exit code 2"),
    ((1, ["psql: error: pre_data.sql: No such file or directory"], False, ""),
     "psql stopped with exit code 1"),
    ((127, [], False, "No such file or directory: '/pg/bin/psql'"), "psql could not run"),
    ((-15, [], True, ""), "cancelled during the schema restore"),
])
def test_schema_step_failure_is_partial(secrets, ctx, local_conn, dump_file, script, reason):
    events = []
    run = FakeRun(events, **{"ON_ERROR_STOP=0": script})
    job, cap, _, _ = make_job(secrets, ctx, run=run, events=events)
    outcome = job.execute(make_plan(dump_file, local_conn))
    assert outcome.status == "partial"
    assert not run.called("--section=data") and not run.called("vacuumdb")
    assert not cap.called("pg_tables")
    assert any(reason in w for w in outcome.warnings)
    assert outcome.log_path is not None and outcome.log_path.exists()


def test_ignored_line_from_psql_does_not_excuse_a_fatal_pg_restore(secrets, ctx, local_conn,
                                                                   dump_file):
    events = []
    run = FakeRun(events, **{
        "ON_ERROR_STOP=0": (0, ["pg_restore: warning: errors ignored on restore: 1"], False, ""),
        "pg_restore": (1, ["pg_restore: error: connection lost"], False, "")})
    job, _, _, _ = make_job(secrets, ctx, run=run, events=events)
    outcome = job.execute(make_plan(dump_file, local_conn))
    assert outcome.status == "partial"
    assert any("pg_restore stopped with exit code 1" in w for w in outcome.warnings)


def test_count_restore_errors_counts_psql_errors():
    log = ("psql:/tmp/a/pre_data.sql:7: ERROR:  unrecognized parameter \"pg_attver\"\n"
           "psql:/tmp/a/pre_data.sql:9: NOTICE:  extension \"vector\" already exists\n"
           "pg_restore: error: could not execute query: ERROR:  relation \"t\" does not exist\n"
           "DETAIL:  ERROR: inside a detail line\n")
    assert count_restore_errors(log) == 2


def toc_with_tables(n, other=0):
    rows = "".join(f"{10 + i}; 0 {20000 + i} TABLE DATA public t{i} odoo\n" for i in range(n))
    rows += "".join(f"{9000 + i}; 0 {30000 + i} TABLE DATA audit a{i} odoo\n"
                    for i in range(other))
    return TOC_EMPTY + rows


def test_user_scenario_no_tables_is_partial(secrets, ctx, local_conn, dump_file, tmp_path):
    root = tmp_path / "filestore"
    (root / "mydb" / "0a").mkdir(parents=True)
    (root / "mydb" / "0a" / "old").write_bytes(b"old")
    src = make_tar(tmp_path / "fs.tar", "mydb", ["0b/new"])
    events = []
    cap = FakeCapture(events, pg_tables=(0, "0\n", ""))
    run = FakeRun(events, **{
        "ON_ERROR_STOP=0": (0, [], False, ""),
        "pg_restore": (1, ['pg_restore: error: could not execute query: ERROR:  relation '
                           '"public.res_users" does not exist'] * 3
                       + ["pg_restore: warning: errors ignored on restore: 3"], False, "")})
    job, _, _, _ = make_job(secrets, ctx, capture=cap, run=run, events=events)
    plan = make_plan(dump_file, local_conn, fs_src=src, fs_root=root, toc=toc_with_tables(2959))
    outcome = job.execute(plan)
    assert outcome.status == "partial" and outcome.errors == 3
    assert f"Only 0 of 2959 tables were restored; see {outcome.log_path.name}." in outcome.warnings
    assert not run.called("vacuumdb")
    assert (root / "mydb" / "0a" / "old").exists() and not (root / "mydb" / "0b").exists()
    assert outcome.quick == {} and outcome.filestore_files == 0
    assert any("mydb is partial" in w for w in outcome.warnings)


@pytest.mark.parametrize("tables, status", [
    ((0, "2811\n", ""), "partial"),  # 94.99 %
    ((0, "2812\n", ""), "ok"),       # 95.03 %
    ((1, "", "ERROR:  boom"), "ok"),  # unknown count: the quick check shows "?"
])
def test_table_shortfall_threshold(secrets, ctx, local_conn, dump_file, tables, status):
    events = []
    cap = FakeCapture(events, pg_tables=tables)
    job, _, run, _ = make_job(secrets, ctx, capture=cap, events=events)
    outcome = job.execute(make_plan(dump_file, local_conn, toc=toc_with_tables(2959)))
    assert outcome.status == status
    assert bool(run.called("vacuumdb")) == (status == "ok")


def test_shortfall_ignores_tables_outside_public(secrets, ctx, local_conn, dump_file):
    events = []
    cap = FakeCapture(events, pg_tables=(0, "2\n", ""))
    job, _, run, _ = make_job(secrets, ctx, capture=cap, events=events)
    outcome = job.execute(make_plan(dump_file, local_conn, toc=toc_with_tables(2, other=100)))
    assert outcome.status == "ok" and run.called("vacuumdb")


def test_shortfall_counts_only_public_table_data(secrets, ctx, local_conn, dump_file):
    events = []
    cap = FakeCapture(events, pg_tables=(0, "2\n", ""))
    job, _, _, _ = make_job(secrets, ctx, capture=cap, events=events)
    outcome = job.execute(make_plan(dump_file, local_conn, toc=toc_with_tables(10, other=5)))
    assert outcome.status == "partial"
    assert f"Only 2 of 10 tables were restored; see {outcome.log_path.name}." in outcome.warnings


# --- psql meta-commands in the schema script -------------------------------------------------

def test_find_psql_meta_command():
    allowed = "SET x = 1;\n\\restrict AbC123\nSELECT 1;\n\\unrestrict AbC123\n"
    assert find_psql_meta_command(allowed) == 0
    assert find_psql_meta_command("SELECT 'a\\b';\nSELECT 1;\n") == 0
    assert find_psql_meta_command("SET x = 1;\n-- Name: x\n\\! touch /tmp/x\n") == 3
    assert find_psql_meta_command("SELECT 1;\n   \\copy t from program 'x'\n") == 2
    assert find_psql_meta_command("\\restrict\n") == 1  # a key is required
    assert find_psql_meta_command("\\restrict k extra\n") == 1


@pytest.mark.parametrize("bad", ["\\! touch x\n", "  \\! touch x\n", "\\i /etc/passwd\n"])
def test_meta_command_in_schema_refuses_before_anything(secrets, ctx, local_conn, dump_file,
                                                        bad):
    events = []
    cap = FakeCapture(events, **{"--section=pre-data": writes_script(
        "-- Name: evil\n" + bad + PRE_DATA)})
    job, _, run, _ = make_job(secrets, ctx, capture=cap, events=events)
    with pytest.raises(JobError) as exc:
        job.execute(make_plan(dump_file, local_conn))
    # line 1 is the prepended SET ROLE, line 2 the comment
    assert str(exc.value) == ("The schema script contains psql meta-commands (line 3); refusing "
                              "to run it. Nothing was changed.")
    assert run.calls == [] and not cap.called("pg_stat_activity")
    assert not list(dump_file.parent.glob("restore_*.log"))
    assert not file_arg_from(cap.called("--section=pre-data")[0]).parent.exists()


def test_restrict_meta_commands_are_allowed(secrets, ctx, local_conn, dump_file):
    events = []
    body = "\\restrict k3Y\n" + PRE_DATA + "\\unrestrict k3Y\n"
    cap = FakeCapture(events, **{"--section=pre-data": writes_script(body)})
    job, _, run, _ = make_job(secrets, ctx, capture=cap, events=events)
    outcome = job.execute(make_plan(dump_file, local_conn))
    assert outcome.status == "ok"
    assert list(run.scripts_seen.values()) == ['SET ROLE "odoo";\n' + body]


def test_log_notes_the_line_offset(secrets, ctx, local_conn, dump_file):
    job, _, _, _ = make_job(secrets, ctx)
    outcome = job.execute(make_plan(dump_file, local_conn))
    first = outcome.log_path.read_text().splitlines()[0]
    assert first == "Schema script line numbers are +1 (SET ROLE prepended)"
