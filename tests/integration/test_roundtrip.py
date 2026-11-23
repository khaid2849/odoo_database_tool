"""Real PostgreSQL round trips; they run only when ODT_TEST_PG_URL is set (CI)."""
from __future__ import annotations

import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
from keyring.backends.fail import Keyring as FailKeyring

from odoo_database_tool.commands import CommandBuilder
from odoo_database_tool.dump import DumpJob, DumpOptions
from odoo_database_tool.filestore import extract_tar_strip, tar_directory
from odoo_database_tool.jobs import JobContext, JobError
from odoo_database_tool.models import Connection, Kind
from odoo_database_tool.neutralize import neutralize
from odoo_database_tool.restore import RestoreJob, RestorePlan, inspect_dump
from odoo_database_tool.runner import CancelToken, Redactor
from odoo_database_tool.settings import AppSettings
from odoo_database_tool.store import Secrets

pytestmark = pytest.mark.integration

FAKE_ADDONS = Path(__file__).parent / "fake_addons"

SEED_SQL = """
CREATE TABLE ir_module_module (id serial PRIMARY KEY, name varchar, state varchar, latest_version varchar);
INSERT INTO ir_module_module (name, state, latest_version) VALUES ('base', 'installed', '18.0.1.0');
CREATE TABLE ir_mail_server (id serial PRIMARY KEY, name varchar, active boolean DEFAULT true);
INSERT INTO ir_mail_server (name) VALUES ('smtp');
CREATE TABLE ir_cron (id serial PRIMARY KEY, name varchar, active boolean DEFAULT true);
INSERT INTO ir_cron (name) VALUES ('job');
CREATE TABLE ir_model_data (id serial PRIMARY KEY, module varchar, name varchar);
INSERT INTO ir_model_data (module, name) VALUES ('base', 'main_company');
CREATE TABLE ir_config_parameter (id serial PRIMARY KEY, key varchar UNIQUE, value varchar);
INSERT INTO ir_config_parameter (key, value) VALUES ('web.base.url', 'http://localhost:8069');
CREATE TABLE res_users (id serial PRIMARY KEY, login varchar, active boolean DEFAULT true);
INSERT INTO res_users (login) VALUES ('admin'), ('demo');
"""


class Pg:
    """Server coordinates parsed from ODT_TEST_PG_URL plus psql helpers for seeding."""

    def __init__(self, url: str):
        u = urlparse(url)
        self.host = u.hostname or "localhost"
        self.port = u.port or 5432
        self.user = unquote(u.username or "postgres")
        self.password = unquote(u.password or "")

    def _env(self) -> dict:
        return {**os.environ, "PGPASSWORD": self.password}

    def _base(self, tool: str) -> list:
        return [tool, "-h", self.host, "-p", str(self.port), "-U", self.user, "-w"]

    def psql(self, db: str, sql: str) -> str:
        res = subprocess.run(self._base("psql") + ["-d", db, "-v", "ON_ERROR_STOP=1", "-tAc", sql],
                             env=self._env(), capture_output=True, text=True, check=True)
        return res.stdout.strip()

    def createdb(self, db: str) -> None:
        subprocess.run(self._base("createdb") + [db], env=self._env(), check=True)

    def dropdb(self, db: str) -> None:
        subprocess.run(self._base("dropdb") + ["--if-exists", "--force", db],
                       env=self._env(), check=False)

    @contextmanager
    def database(self, prefix: str = "odt_it"):
        """A fresh, uniquely named database, dropped on exit."""
        name = f"{prefix}_{uuid.uuid4().hex[:10]}"
        self.createdb(name)
        try:
            yield name
        finally:
            self.dropdb(name)

    def seed(self, db: str) -> None:
        self.psql(db, SEED_SQL)


@pytest.fixture(scope="module")
def pg():
    return Pg(os.environ["ODT_TEST_PG_URL"])


@pytest.fixture
def env(pg):
    conn = Connection.new("CI Postgres", Kind.LOCAL)
    conn.pg.host, conn.pg.port, conn.pg.user = pg.host, pg.port, pg.user
    secrets = Secrets(backend=FailKeyring())  # in-memory
    if pg.password:
        secrets.set(conn.id, "pg_password", pg.password)
    settings = AppSettings()
    builder = CommandBuilder(settings, secrets, "ssh")
    logs: list = []
    ctx = JobContext(log=logs.append, progress=lambda pct, text: None,
                     cancel=CancelToken(), redactor=Redactor())

    class Env:
        pass

    e = Env()
    e.conn, e.settings, e.builder, e.ctx, e.logs = conn, settings, builder, ctx, logs
    return e


def make_dump(env, db: str, dest: Path) -> Path:
    opts = DumpOptions(env.conn, db, with_db=True, with_filestore=False, gzip=False, dest=dest)
    result = DumpJob(env.builder, env.settings, env.ctx).run(opts)
    assert len(result.files) == 1 and result.files[0].suffix == ".dump"
    return result.files[0]


def plan_for(env, dump: Path, db: str, is_new: bool) -> RestorePlan:
    info = inspect_dump(dump, env.settings.tool("pg_restore"))
    return RestorePlan(info, env.conn, db, is_new, env.conn.pg.user, "C", "C", None, None, 2)


def test_dump_restore_roundtrip(pg, env, tmp_path):
    with pg.database() as source:
        pg.seed(source)
        dump = make_dump(env, source, tmp_path / "dumps")
        info = inspect_dump(dump, env.settings.tool("pg_restore"))
        assert info.toc.table_data >= 6 and info.sha_status == "ok"

        target = f"odt_it_{uuid.uuid4().hex[:10]}"
        try:
            job = RestoreJob(env.builder, env.settings, env.ctx)
            outcome = job.execute(plan_for(env, dump, target, is_new=True))
            assert outcome.status == "ok" and outcome.errors == 0
            quick = job.quick_check(env.conn, target, info.toc)
            assert quick["active_users"] == "2"
            assert quick["installed_modules"] == "1"
            assert quick["dump_table_data"] == str(info.toc.table_data)
            assert pg.psql(target, "select count(*) from ir_mail_server") == "1"
            assert target in [name for name, _size in job.list_databases(env.conn)]
        finally:
            pg.dropdb(target)


def test_restore_refuses_with_open_connection(pg, env, tmp_path):
    with pg.database() as source:
        pg.seed(source)
        dump = make_dump(env, source, tmp_path / "dumps")
        holder = subprocess.Popen(pg._base("psql") + ["-d", source, "-c", "select pg_sleep(30)"],
                                  env=pg._env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            job = RestoreJob(env.builder, env.settings, env.ctx)
            deadline = time.monotonic() + 15
            while not job.active_connections(env.conn, source) and time.monotonic() < deadline:
                time.sleep(0.3)
            with pytest.raises(JobError) as exc:
                job.execute(plan_for(env, dump, source, is_new=False))
            assert "other connection" in str(exc.value) and "psql" in str(exc.value)
            assert pg.psql(source, "select count(*) from res_users") == "2"  # untouched
        finally:
            holder.kill()
            holder.wait()


def test_neutralize_with_fake_addons(pg, env):
    with pg.database() as db:
        pg.seed(db)
        result = neutralize(env.builder, env.conn, db, {"18.0": [str(FAKE_ADDONS)]}, env.ctx)
        assert result.ok, result
        assert result.series == "18.0"
        assert pg.psql(db, "select active from ir_mail_server") == "f"
        assert pg.psql(db, "select active from ir_cron") == "f"
        assert pg.psql(db, "select value from ir_config_parameter "
                           "where key = 'database.is_neutralized'") in ("t", "true")


def test_filestore_tar_roundtrip_local(tmp_path):
    root = tmp_path / "filestore"
    (root / "mydb" / "0a").mkdir(parents=True)
    (root / "mydb" / "ff").mkdir()
    (root / "mydb" / "0a" / "f1").write_bytes(b"abc")
    (root / "mydb" / "ff" / "f2").write_bytes(b"zz")
    archive = tmp_path / "filestore_mydb.tar.gz"
    assert tar_directory(root, "mydb", archive, gzip=True) == 2
    dest = tmp_path / "restored" / "other_db"
    assert extract_tar_strip(archive, dest) == 2
    assert (dest / "0a" / "f1").read_bytes() == b"abc"
    assert (dest / "ff" / "f2").read_bytes() == b"zz"
