from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal

from odoo_database_tool.dump import DumpResult
from odoo_database_tool.jobs import Check, JobContext, JobError
from odoo_database_tool.models import Connection, Kind
from odoo_database_tool.runner import CancelToken, Redactor
from odoo_database_tool.store import ConnectionStore
from odoo_database_tool.ui.dump_tab import DumpTab, format_check_lines, source_label


class FakeSettings:
    def __init__(self, dump_dir):
        self.dump_dir = dump_dir


class FakeWindow(QObject):
    job_running = Signal(bool)
    connections_changed = Signal()

    def __init__(self, dump_dir):
        super().__init__()
        self.settings = FakeSettings(dump_dir)
        self.refuse = False

    def run_job(self, fn, on_done, on_failed=None):
        if self.refuse:
            return False
        ctx = JobContext(log=lambda t: None, progress=lambda p, t: None,
                         cancel=CancelToken(), redactor=Redactor())
        try:
            result = fn(ctx)
        except Exception as exc:  # noqa: BLE001
            if on_failed:
                on_failed(str(exc))
        else:
            on_done(result)
        return True


class FakeJob:
    def __init__(self):
        self.calls = []
        self.databases = [("shop", 1_288_490_189), ("erp", 2048)]
        self.checks = [Check("Disk", True, "10 GB free")]
        self.result = None
        self.error = None

    def list_databases(self, conn):
        self.calls.append(("list", conn.name))
        return self.databases

    def preflight(self, opts):
        self.calls.append(("preflight", opts.db))
        return self.checks

    def run(self, opts, *, confirmed_production=False):
        self.calls.append(("run", opts.db, confirmed_production, opts.dest))
        if self.error:
            raise JobError(self.error)
        return self.result


def make(kind, name="c", production=False):
    conn = Connection.new(name, kind)
    conn.production = production
    return conn


@pytest.fixture
def env(qapp, tmp_path):
    store = ConnectionStore(tmp_path / "connections.json")
    window = FakeWindow(tmp_path / "dumps")
    job = FakeJob()
    tab = DumpTab(store, window, job_factory=lambda ctx: job)
    tab.confirm = lambda *a, **k: True
    return store, window, job, tab


def add(store, tab, conn):
    store.upsert(conn)
    tab.refresh_sources()


def load(tab):
    tab.load_databases()


def test_filestore_disabled_for_postgres_enabled_for_ssh_docker(env):
    store, _, _, tab = env
    pg, docker = make(Kind.POSTGRES, "pg"), make(Kind.SSH_DOCKER, "dk")
    store.upsert(pg)
    store.upsert(docker)
    tab.refresh_sources()
    tab.source_combo.setCurrentIndex(tab.source_combo.findData(pg.id))
    assert not tab.filestore_check.isEnabled() and tab.filestore_check.toolTip()
    tab.source_combo.setCurrentIndex(tab.source_combo.findData(docker.id))
    assert tab.filestore_check.isEnabled()


def test_gzip_only_with_filestore(env):
    store, _, _, tab = env
    add(store, tab, make(Kind.SSH_DOCKER))
    assert not tab.gzip_check.isEnabled()
    tab.filestore_check.setChecked(True)
    assert tab.gzip_check.isEnabled()


def test_dump_disabled_until_db_selected_and_something_ticked(env):
    store, _, _, tab = env
    add(store, tab, make(Kind.SSH_DOCKER))
    assert not tab.dump_button.isEnabled() and not tab.check_button.isEnabled()
    load(tab)
    assert tab.dump_button.isEnabled()
    tab.db_check.setChecked(False)
    assert not tab.dump_button.isEnabled()
    tab.filestore_check.setChecked(True)
    assert tab.dump_button.isEnabled()


def test_database_items_show_size_and_source_change_clears(env):
    store, _, _, tab = env
    a, b = make(Kind.SSH_DOCKER, "a"), make(Kind.LOCAL, "b")
    store.upsert(a)
    store.upsert(b)
    tab.refresh_sources()
    load(tab)
    assert tab.db_combo.itemText(0) == "shop — 1.2 GB"
    assert tab.selected_db() == "shop"
    tab.source_combo.setCurrentIndex(1)
    assert tab.db_combo.count() == 0


def test_source_labels_and_selection_preserved(env):
    store, window, _, tab = env
    a, b = make(Kind.SSH_DOCKER, "a", production=True), make(Kind.LOCAL, "b")
    store.upsert(a)
    store.upsert(b)
    tab.refresh_sources()
    assert tab.source_combo.itemText(0) == "a (SSH + Docker) PRODUCTION"
    assert source_label(b) == "b (Local Postgres)"
    tab.source_combo.setCurrentIndex(1)
    window.connections_changed.emit()
    assert tab.selected_connection().id == b.id
    store.delete(b.id)
    window.connections_changed.emit()
    assert tab.selected_connection().id == a.id


def test_production_dump_confirms_and_no_does_nothing(env):
    store, _, job, tab = env
    add(store, tab, make(Kind.SSH_DOCKER, "prod1", production=True))
    load(tab)
    asked = []
    tab.confirm = lambda parent, title, msg, danger=False, **k: asked.append((msg, danger)) or False
    tab.dump_button.click()
    assert asked and asked[0][1] is True
    assert "prod1" in asked[0][0] and "shop" in asked[0][0]
    assert not any(c[0] == "run" for c in job.calls)
    tab.confirm = lambda *a, **k: True
    job.result = DumpResult(files=[])
    tab.dump_button.click()
    assert ("run", "shop", True, tmp_dest(tab)) in job.calls


def tmp_dest(tab):
    return Path(tab.dest_edit.text())


def test_non_production_does_not_confirm(env):
    store, _, job, tab = env
    add(store, tab, make(Kind.SSH_DOCKER))
    load(tab)
    tab.confirm = lambda *a, **k: pytest.fail("must not ask")
    job.result = DumpResult(files=[])
    tab.dump_button.click()
    assert job.calls[-1][:3] == ("run", "shop", False)


def test_success_lists_files_warnings_and_opens_folder(env, tmp_path):
    store, _, job, tab = env
    add(store, tab, make(Kind.SSH_DOCKER))
    load(tab)
    f = tmp_path / "shop.dump"
    f.write_bytes(b"x" * 2048)
    job.result = DumpResult(files=[f], warnings=["stale filestore"])
    opened = []
    tab.open_url = opened.append
    tab.dump_button.click()
    text = tab.result.text()
    assert tab.result.objectName() == "resultOk"
    assert "shop.dump" in text and "2.0 KB" in text and "stale filestore" in text
    tab.open_button.click()
    assert opened[0].toLocalFile() == str(tmp_path)


def test_failure_shows_error_in_red_box(env):
    store, _, job, tab = env
    add(store, tab, make(Kind.SSH_DOCKER))
    load(tab)
    job.error = "disk full"
    tab.dump_button.click()
    assert tab.result.objectName() == "resultBad" and "disk full" in tab.result.text()
    assert not tab.open_button.isVisibleTo(tab)


def test_check_formats_marks_and_hints(env):
    store, _, job, tab = env
    add(store, tab, make(Kind.SSH_DOCKER))
    load(tab)
    job.checks = [Check("SSH", True, "ok"), Check("Disk", False, "1 GB free", "Free space"),
                  Check("Version", True, "older", warning=True)]
    tab.check_button.click()
    lines = tab.result.text().splitlines()
    assert lines[0] == "✓ SSH — ok"
    assert lines[1] == "✗ Disk — 1 GB free" and "Free space" in lines[2]
    assert any(line.startswith("⚠ Version") for line in lines)
    assert tab.result.objectName() == "resultBad"
    assert format_check_lines([Check("A", True, "d")]) == "✓ A — d"


def test_buttons_disabled_while_job_runs(env):
    store, window, _, tab = env
    add(store, tab, make(Kind.SSH_DOCKER))
    load(tab)
    window.job_running.emit(True)
    assert not (tab.dump_button.isEnabled() or tab.check_button.isEnabled()
                or tab.load_button.isEnabled())
    window.job_running.emit(False)
    assert tab.dump_button.isEnabled()


def test_list_failure_shown(env):
    store, _, job, tab = env
    add(store, tab, make(Kind.SSH_DOCKER))
    job.list_databases = lambda conn: (_ for _ in ()).throw(JobError("no route"))
    load(tab)
    assert tab.result.objectName() == "resultBad" and "no route" in tab.result.text()


def test_dest_defaults_to_settings_and_browse(env, tmp_path):
    _, _, _, tab = env
    assert tab.dest_edit.text() == str(tmp_path / "dumps")
    tab.pick_folder = lambda start: str(tmp_path / "other")
    tab.browse_button.click()
    assert tab.dest_edit.text() == str(tmp_path / "other")
    tab.pick_folder = lambda start: ""
    tab.browse_button.click()
    assert tab.dest_edit.text() == str(tmp_path / "other")
