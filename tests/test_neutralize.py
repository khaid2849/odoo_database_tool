from __future__ import annotations

from pathlib import Path

import pytest

from odoo_database_tool.commands import CommandBuilder
from odoo_database_tool.models import Connection, Kind
from odoo_database_tool.neutralize import NeutralizeResult, neutralize, neutralize_sql_files, odoo_series
from odoo_database_tool.runner import RunResult
from test_restore import FakeSettings, FakeTunnel, ctx, secrets  # noqa: F401  (fixtures)


@pytest.mark.parametrize("version,expected", [
    ("18.0.1.3", "18.0"), ("17.0.1.0", "17.0"), ("saas~17.4.1.0", "saas~17.4"), ("18.0", "18.0"),
    (None, None), ("", None), ("garbage", None), ("18", None), ("x.y.1.0", None),
])
def test_odoo_series(version, expected):
    assert odoo_series(version) == expected


def write_sql(root: Path, rel: str, body: str) -> Path:
    path = root / rel / "data" / "neutralize.sql"
    path.parent.mkdir(parents=True)
    path.write_text(body)
    return path


def test_neutralize_sql_files_base_first_and_skips_missing(tmp_path):
    base = write_sql(tmp_path, "odoo/addons/base", "-- base")
    mail = write_sql(tmp_path, "addons/mail", "-- mail")
    (tmp_path / "addons" / "web").mkdir()  # module without the file
    found = neutralize_sql_files(["mail", "web", "base"], [str(tmp_path)])
    assert found == [base, mail]


def test_neutralize_sql_files_first_hit_wins_and_dir_itself(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    hit = write_sql(first, "custom", "-- one")
    write_sql(second, "custom", "-- two")
    own = write_sql(second, "other", "-- other")
    assert neutralize_sql_files(["custom", "other"], [str(first), str(second)]) == [hit, own]


def test_neutralize_sql_files_rejects_unsafe_names(tmp_path):
    write_sql(tmp_path / "x", "evil", "-- evil")
    (tmp_path / "addons").mkdir()
    assert neutralize_sql_files(["../x/evil", "..", "a/b", ""], [str(tmp_path / "addons")]) == []


class FakeCapture:
    def __init__(self, version="18.0.1.3", modules="base\nmail\nnot_there\n", rc=0):
        self.version, self.modules, self.rc = version, modules, rc
        self.calls = []

    def __call__(self, cmd, *, timeout=60, redactor=None):
        joined = " ".join(cmd.argv)
        self.calls.append(joined)
        if self.rc:
            return self.rc, "", "psql: connection refused"
        if "latest_version" in joined:
            return 0, self.version + "\n", ""
        if "state in" in joined:
            return 0, self.modules, ""
        raise AssertionError(joined)


class FakeRun:
    def __init__(self, rc=0, stderr=()):
        self.rc, self.stderr, self.calls, self.content = rc, list(stderr), [], None

    def __call__(self, cmd, out_path, *, on_bytes=None, on_line=None, cancel=None,
                 redactor=None, tail=200):
        self.calls.append(cmd.argv)
        self.content = Path(cmd.argv[cmd.argv.index("-f") + 1]).read_text()
        self.path = cmd.argv[cmd.argv.index("-f") + 1]
        return RunResult(self.rc, self.stderr, 0, False)


@pytest.fixture
def local_conn():
    c = Connection.new("Local", Kind.LOCAL)
    c.pg.user = "odoo"
    return c


def tree(tmp_path):
    write_sql(tmp_path, "odoo/addons/base", "update ir_mail_server set active = false;")
    write_sql(tmp_path, "addons/mail", "update ir_cron set active = false;")
    return {"18.0": [str(tmp_path)]}


def go(secrets, ctx, conn, sets, capture=None, run=None, **kw):
    builder = CommandBuilder(FakeSettings(), secrets, "ssh")
    return neutralize(builder, conn, "mydb", sets, ctx, run=run or FakeRun(),
                      capture=capture or FakeCapture(), **kw)


def test_no_matching_set_names_series(secrets, ctx, local_conn):
    run = FakeRun()
    res = go(secrets, ctx, local_conn, {"17.0": ["/x"]}, run=run)
    assert res == NeutralizeResult(False, "18.0", [], res.reason)
    assert "18.0" in res.reason and not run.calls


def test_unreadable_series(secrets, ctx, local_conn):
    res = go(secrets, ctx, local_conn, {}, capture=FakeCapture(version="weird"))
    assert not res.ok and res.series is None and "version" in res.reason


def test_base_file_not_found(secrets, ctx, local_conn, tmp_path):
    run = FakeRun()
    res = go(secrets, ctx, local_conn, {"18.0": [str(tmp_path)]}, run=run)
    assert not res.ok and "base" in res.reason and not run.calls


def test_runs_one_psql_with_all_bodies(secrets, ctx, local_conn, tmp_path):
    run = FakeRun()
    res = go(secrets, ctx, local_conn, tree(tmp_path), run=run)
    assert res.ok and res.series == "18.0" and len(res.files) == 2
    assert len(run.calls) == 1 and "--single-transaction" in run.calls[0]
    base_at = run.content.index("-- base")
    assert base_at < run.content.index("-- mail")
    assert "ir_mail_server" in run.content and "ir_cron" in run.content
    assert not Path(run.path).exists()  # temp file removed
    assert any("mail" in line for line in ctx.events["log"])


def test_psql_failure_reports_redacted_last_line(secrets, ctx, local_conn, tmp_path):
    ctx.redactor.add("s3cret")
    run = FakeRun(rc=3, stderr=["noise", "ERROR: boom s3cret"])
    res = go(secrets, ctx, local_conn, tree(tmp_path), run=run)
    assert not res.ok and "boom" in res.reason and "s3cret" not in res.reason


def test_query_failure_is_not_ok_not_raise(secrets, ctx, local_conn, tmp_path):
    res = go(secrets, ctx, local_conn, tree(tmp_path), capture=FakeCapture(rc=2))
    assert not res.ok and "connection refused" in res.reason


def test_opens_tunnel_when_needed(secrets, ctx, tmp_path):
    from odoo_database_tool.models import SshTarget
    c = Connection.new("Staging PG", Kind.POSTGRES)
    c.pg.host, c.pg.user = "10.0.0.5", "odoo"
    c.tunnel = SshTarget(host="jump", user="u")
    run = FakeRun()
    res = go(secrets, ctx, c, tree(tmp_path), run=run, tunnel_factory=FakeTunnel)
    assert res.ok and len(FakeTunnel.opened) == 1 and FakeTunnel.opened[0].closed
    assert "55432" in " ".join(run.calls[0])


# --- final review fixes ------------------------------------------------------------------

class HostCapture(FakeCapture):
    def __init__(self, host, **kw):
        super().__init__(**kw)
        self.host = host

    def __call__(self, cmd, *, timeout=60, redactor=None):
        joined = " ".join(cmd.argv)
        if joined.endswith(" hostname"):
            self.calls.append(joined)
            return 0, self.host + "\n", ""
        return super().__call__(cmd, timeout=timeout, redactor=redactor)


def tunnelled(expected="db-box"):
    from odoo_database_tool.models import SshTarget
    c = Connection.new("Staging PG", Kind.POSTGRES)
    c.pg.host, c.pg.user = "10.0.0.5", "odoo"
    c.tunnel = SshTarget(host="jump", user="u", expected_hostname=expected)
    return c


def test_expected_hostname_mismatch_never_runs_psql(secrets, ctx, tmp_path):
    FakeTunnel.opened.clear()
    run, cap = FakeRun(), HostCapture("clone-box")
    res = go(secrets, ctx, tunnelled(), tree(tmp_path), capture=cap, run=run,
             tunnel_factory=FakeTunnel)
    assert not res.ok and "clone-box" in res.reason and "db-box" in res.reason
    assert run.calls == [] and FakeTunnel.opened == []
    assert len(cap.calls) == 1 and cap.calls[0].endswith(" hostname")


def test_expected_hostname_match_neutralizes(secrets, ctx, tmp_path):
    run, cap = FakeRun(), HostCapture("db-box")
    res = go(secrets, ctx, tunnelled(), tree(tmp_path), capture=cap, run=run,
             tunnel_factory=FakeTunnel)
    assert res.ok and cap.calls[0].endswith(" hostname") and len(run.calls) == 1


def test_missing_module_folders_reported_separately(secrets, ctx, local_conn, tmp_path):
    sets = tree(tmp_path)
    (tmp_path / "addons" / "web").mkdir()  # present, but without neutralize.sql
    cap = FakeCapture(modules="base\nmail\nweb\nnot_there\ncustom_x\n")
    res = go(secrets, ctx, local_conn, sets, capture=cap)
    assert res.ok and len(res.files) == 2
    assert res.missing == ["custom_x", "not_there"]
    assert any("not_there" in line for line in ctx.events["log"])


def test_no_missing_modules_by_default(secrets, ctx, local_conn, tmp_path):
    res = go(secrets, ctx, local_conn, tree(tmp_path), capture=FakeCapture(modules="base\nmail\n"))
    assert res.ok and res.missing == []
