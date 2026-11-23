from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal

from odoo_database_tool.archive import Manifest, TocInfo
from odoo_database_tool.common import odoo_data_dir
from odoo_database_tool.jobs import JobContext, JobError
from odoo_database_tool.models import Connection, Kind, SshTarget
from odoo_database_tool.neutralize import NeutralizeResult
from odoo_database_tool.restore import DumpInfo, RestoreOutcome
from odoo_database_tool.runner import CancelToken, Redactor
from odoo_database_tool.store import ConnectionStore
from odoo_database_tool.ui.restore_tab import (
    FILESTORE_REMOTE, NEW_DB_LABEL, NOT_NEUTRALIZED, RestoreTab,
)

QUICK = {"size": "2.0 MB", "tables": "812", "active_users": "3", "installed_modules": "120",
         "neutralized": "", "dump_table_data": "2"}


class FakeSettings:
    def __init__(self, dump_dir):
        self.dump_dir = dump_dir
        self.jobs = 3
        self.addons_sets = {"18.0": ["/odoo/addons"]}

    def tool(self, name):
        return f"tool:{name}"


class FakeWindow(QObject):
    job_running = Signal(bool)
    connections_changed = Signal()

    def __init__(self, dump_dir):
        super().__init__()
        self.settings = FakeSettings(dump_dir)
        self.refuse = False
        self.pending = None

    def builder(self):
        return "builder"

    def run_job(self, fn, on_done, on_failed=None):
        if self.refuse:
            return False
        if self.pending is not None:  # deferred: the test completes the job later
            self.pending.append((fn, on_done))
            return True
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
        self.locale = ("en_US.UTF-8", "en_US.UTF-8")
        self.outcome = RestoreOutcome("ok", 0, Path("/dumps/restore_shop.log"), 0, dict(QUICK), [])
        self.quick_after = dict(QUICK, neutralized="True")
        self.error = None

    def list_databases(self, target):
        self.calls.append(("list", target.name))
        return self.databases

    def db_locale(self, target, db):
        self.calls.append(("locale", db))
        return self.locale

    def summary(self, plan):
        self.calls.append(("summary", plan.db))
        return [f"Drop database {plan.db}.", f"Create database {plan.db}."]

    def execute(self, plan, *, confirmed_text=""):
        self.calls.append(("execute", plan, confirmed_text))
        if self.error:
            raise JobError(self.error)
        return self.outcome

    def drop_incomplete(self, target, db):
        self.calls.append(("drop", target.name, db))

    def quick_check(self, target, db, toc):
        self.calls.append(("quick", db))
        return self.quick_after

    def names(self):
        return [c[0] for c in self.calls]


def make_info(path, manifest=True):
    man = Manifest("db", "Team server", "ssh_docker", "shop", "2026-11-23T10:00", 1000,
                   "ab" * 32, odoo_version="18.0") if manifest else None
    return DumpInfo(Path(path), 1_048_576, TocInfo("17.12", "17.2", 40, 25, []), "ok", man)


def make(kind, name="c", production=False, host="localhost", user="odoo", tunnel=False):
    conn = Connection.new(name, kind)
    conn.production = production
    if conn.pg is not None:
        conn.pg.host, conn.pg.user = host, user
    if tunnel:
        conn.tunnel = SshTarget(host="jump")
    return conn


@pytest.fixture
def env(qapp, tmp_path):
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    (dumps / "shop_srv_20261120-1000.dump").write_bytes(b"old")
    newest = dumps / "shop_srv_20261123-1000.dump"
    newest.write_bytes(b"new")
    store = ConnectionStore(tmp_path / "connections.json")
    window = FakeWindow(dumps)
    job = FakeJob()
    inspected = []
    neutralized = []

    def inspect_fn(path, tool):
        inspected.append((Path(path), tool))
        if "bad" in Path(path).name:
            raise JobError("pg_restore cannot read bad.dump")
        return make_info(path)

    def neutralize_fn(builder, target, db, addons_sets, ctx):
        neutralized.append((builder, target.name, db, addons_sets))
        return env.neutralize_result

    tab = RestoreTab(store, window, job_factory=lambda ctx: job, inspect_fn=inspect_fn,
                     neutralize_fn=neutralize_fn)
    tab.confirm = lambda *a, **k: True
    tab.confirm_typed = lambda *a, **k: pytest.fail("confirm_typed must not be used")

    class Env:
        pass

    env = Env()
    env.store, env.window, env.job, env.tab = store, window, job, tab
    env.inspected, env.neutralized, env.newest = inspected, neutralized, newest
    env.neutralize_result = NeutralizeResult(True, "18.0", [Path("a"), Path("b")])
    return env


def add(env, *conns):
    for conn in conns:
        env.store.upsert(conn)
    env.tab.refresh_targets()


def choose_db(tab, name):
    tab.db_combo.setCurrentIndex(tab.db_combo.findData(name))


def choose_new(tab, name):
    tab.db_combo.setCurrentIndex(tab.db_combo.count() - 1)
    tab.new_db_edit.setText(name)


def ready(env, conn=None, db="shop"):
    """Inspected dump, target selected, databases loaded, db chosen."""
    add(env, conn or make(Kind.LOCAL, "local"))
    env.tab.inspect_dump()
    env.tab.load_databases()
    choose_db(env.tab, db)


def card_text(tab):
    return tab.result.text() + "\n" + tab.neutralize_status.text()


# --- dump file ----------------------------------------------------------------------


def test_dump_defaults_to_newest_and_details_shown(env):
    tab = env.tab
    assert tab.dump_edit.text() == str(env.newest)
    tab.inspect_dump()
    assert env.inspected == [(env.newest, "tool:pg_restore")]
    text = tab.dump_details.text()
    for part in ("shop_srv_20261123-1000.dump", "1.0 MB", "17.12", "25", "SHA256 verified",
                 "Team server", "shop", "Odoo 18.0"):
        assert part in text
    assert tab.dump_details.objectName() != "resultBad"


def test_inspect_error_shown_red_and_restore_stays_disabled(env, tmp_path):
    tab = env.tab
    bad = tmp_path / "dumps" / "bad.dump"
    bad.write_bytes(b"x")
    tab.pick_dump = lambda start: str(bad)
    add(env, make(Kind.LOCAL, "local"))
    tab.load_databases()
    choose_db(tab, "shop")
    tab.dump_browse.click()
    assert tab.dump_details.objectName() == "resultBad"
    assert "cannot read bad.dump" in tab.dump_details.text()
    assert not tab.restore_button.isEnabled()


def test_matching_filestore_prefilled(env, tmp_path):
    archive = tmp_path / "dumps" / "filestore_shop_srv_20261123-1000.tar"
    archive.write_bytes(b"t")
    add(env, make(Kind.LOCAL, "local"))
    env.tab.inspect_dump()
    assert env.tab.fs_src_edit.text() == str(archive)


# --- target -------------------------------------------------------------------------


def test_only_restore_targets_listed_never_production(env):
    prod = make(Kind.POSTGRES, "prod", production=True, host="10.0.0.1")
    docker = make(Kind.SSH_DOCKER, "docker")
    local = make(Kind.LOCAL, "local")
    remote = make(Kind.POSTGRES, "remote", host="10.0.0.5")
    add(env, prod, docker, local, remote)
    ids = [env.tab.target_combo.itemData(i) for i in range(env.tab.target_combo.count())]
    assert set(ids) == {local.id, remote.id}


def test_target_selection_preserved_on_connections_changed(env):
    a, b = make(Kind.LOCAL, "a"), make(Kind.POSTGRES, "b", host="10.0.0.5")
    add(env, a, b)
    env.tab.target_combo.setCurrentIndex(env.tab.target_combo.findData(b.id))
    env.window.connections_changed.emit()
    assert env.tab.selected_target().id == b.id
    env.store.delete(b.id)
    env.window.connections_changed.emit()
    assert env.tab.selected_target().id == a.id


def test_filestore_group_disabled_for_remote_enabled_for_local(env):
    local, remote = make(Kind.LOCAL, "local"), make(Kind.POSTGRES, "remote", host="10.0.0.5")
    add(env, local, remote)
    tab = env.tab
    tab.target_combo.setCurrentIndex(tab.target_combo.findData(remote.id))
    assert not tab.fs_group.isEnabled()
    assert tab.fs_note.text() == FILESTORE_REMOTE and tab.fs_note.isVisibleTo(tab)
    tab.target_combo.setCurrentIndex(tab.target_combo.findData(local.id))
    assert tab.fs_group.isEnabled() and not tab.fs_note.isVisibleTo(tab)
    assert tab.fs_root_edit.text() == str(odoo_data_dir() / "filestore")


def test_tunnelled_localhost_counts_as_remote(env):
    add(env, make(Kind.POSTGRES, "tun", host="localhost", tunnel=True))
    assert not env.tab.fs_group.isEnabled()


def test_owner_defaults_to_target_user_and_is_editable(env):
    a = make(Kind.LOCAL, "a", user="alice")
    b = make(Kind.POSTGRES, "b", host="10.0.0.5", user="bob")
    add(env, a, b)
    tab = env.tab
    tab.target_combo.setCurrentIndex(tab.target_combo.findData(a.id))
    assert tab.owner_edit.text() == "alice"
    tab.target_combo.setCurrentIndex(tab.target_combo.findData(b.id))
    assert tab.owner_edit.text() == "bob"
    tab.owner_edit.setText("odoo_owner")
    assert tab.owner_edit.isEnabled()


def test_databases_listed_with_size_plus_create_new(env):
    add(env, make(Kind.LOCAL, "local"))
    tab = env.tab
    tab.load_databases()
    texts = [tab.db_combo.itemText(i) for i in range(tab.db_combo.count())]
    assert texts == ["shop — 1.2 GB", "erp — 2.0 KB", NEW_DB_LABEL]
    assert tab.db_combo.currentIndex() == -1  # the user must pick the database explicitly
    assert not tab.new_db_edit.isVisibleTo(tab)


def test_existing_db_fetches_locale_for_plan(env):
    ready(env)
    assert ("locale", "shop") in env.job.calls
    plan = env.tab.build_plan()
    assert (plan.db, plan.is_new, plan.collate, plan.ctype) == (
        "shop", False, "en_US.UTF-8", "en_US.UTF-8")
    assert plan.owner == "odoo" and plan.jobs == 3
    assert env.tab.restore_button.isEnabled()


def test_locale_failure_keeps_restore_disabled(env):
    env.job.db_locale = lambda target, db: (_ for _ in ()).throw(JobError("no such db"))
    ready(env)
    assert not env.tab.restore_button.isEnabled()
    assert "no such db" in env.tab.message.text()


def test_new_db_name_validated_and_uses_c_locale(env):
    add(env, make(Kind.LOCAL, "local"))
    tab = env.tab
    tab.inspect_dump()
    tab.load_databases()
    choose_new(tab, "")
    assert tab.new_db_edit.isVisibleTo(tab)
    assert not tab.restore_button.isEnabled()
    tab.new_db_edit.setText("bad name;")
    assert not tab.restore_button.isEnabled() and tab.new_db_hint.text()
    tab.new_db_edit.setText("shop")
    assert not tab.restore_button.isEnabled() and "exists" in tab.new_db_hint.text()
    tab.new_db_edit.setText("shop_copy")
    assert tab.restore_button.isEnabled() and tab.new_db_hint.text() == ""
    plan = tab.build_plan()
    assert (plan.db, plan.is_new, plan.collate, plan.ctype) == ("shop_copy", True, "C", "C")
    assert "locale" not in env.job.names()


def test_target_switch_during_load_discards_old_databases(env):
    add(env, make(Kind.LOCAL, "a"), make(Kind.POSTGRES, "b", host="10.0.0.5"))
    env.window.pending = []
    env.tab.load_databases()
    env.tab.target_combo.setCurrentIndex(1)
    fn, on_done = env.window.pending[0]
    on_done([("old_server_db", 1)])
    assert env.tab.db_combo.count() == 0


def test_restore_disabled_until_dump_target_and_db(env):
    tab = env.tab
    assert not tab.restore_button.isEnabled()
    add(env, make(Kind.LOCAL, "local"))
    tab.load_databases()
    choose_db(tab, "shop")
    assert not tab.restore_button.isEnabled()  # dump not inspected yet
    tab.inspect_dump()
    assert tab.restore_button.isEnabled()
    tab.owner_edit.setText(" ")
    assert not tab.restore_button.isEnabled()


def test_controls_disabled_while_job_runs(env):
    ready(env)
    tab = env.tab
    env.window.job_running.emit(True)
    for w in (tab.restore_button, tab.load_button, tab.target_combo, tab.db_combo,
              tab.dump_browse, tab.dump_edit, tab.owner_edit, tab.fs_group):
        assert not w.isEnabled()
    env.window.job_running.emit(False)
    assert tab.restore_button.isEnabled() and tab.fs_group.isEnabled()


# --- restore confirmation -----------------------------------------------------------


def test_local_restore_confirms_with_summary_danger(env):
    ready(env)
    asked = []
    env.tab.confirm = lambda parent, title, msg, danger=False, **k: asked.append((msg, danger)) or False
    env.tab.restore_button.click()
    assert asked[0][1] is True
    assert "Drop database shop." in asked[0][0] and "Create database shop." in asked[0][0]
    assert "execute" not in env.job.names()


def test_remote_restore_requires_typed_name(env):
    ready(env, make(Kind.POSTGRES, "remote", host="10.0.0.5"))
    typed = []
    env.tab.confirm_typed = lambda parent, title, msg, expected, **k: typed.append(
        (msg, expected)) or False
    env.tab.restore_button.click()
    assert typed and typed[0][1] == "shop" and "Drop database shop." in typed[0][0]
    assert "execute" not in env.job.names()
    env.tab.confirm_typed = lambda *a, **k: True
    env.tab.restore_button.click()
    _, plan, confirmed = [c for c in env.job.calls if c[0] == "execute"][0]
    assert confirmed == "shop" and plan.db == "shop"


def test_remote_plan_ignores_filestore_values(env, tmp_path):
    ready(env, make(Kind.POSTGRES, "remote", host="10.0.0.5"))
    env.tab.fs_src_edit.setText(str(tmp_path))
    plan = env.tab.build_plan()
    assert plan.filestore_src is None and plan.filestore_root is None


def test_local_plan_takes_filestore_values(env, tmp_path):
    ready(env)
    env.tab.fs_src_edit.setText(str(tmp_path / "fs.tar"))
    env.tab.fs_root_edit.setText(str(tmp_path / "root"))
    plan = env.tab.build_plan()
    assert plan.filestore_src == tmp_path / "fs.tar"
    assert plan.filestore_root == tmp_path / "root"
    env.tab.fs_src_edit.setText("")
    assert env.tab.build_plan().filestore_src is None


def test_local_restore_passes_no_confirmed_text(env):
    ready(env)
    env.tab.restore_button.click()
    _, plan, confirmed = [c for c in env.job.calls if c[0] == "execute"][0]
    assert confirmed == "" and plan.target.name == "local"


def test_execute_failure_shown_red_without_drop_offer(env):
    ready(env)
    env.job.error = "shop has 1 other connection(s)"
    env.tab.restore_button.click()
    assert env.tab.result.objectName() == "resultBad"
    assert "1 other connection" in env.tab.result.text()
    assert not env.tab.drop_button.isVisibleTo(env.tab)
    assert env.neutralized == []
    assert env.tab.db_combo.count() == 0 and not env.tab.restore_button.isEnabled()


# --- outcomes -----------------------------------------------------------------------


def test_partial_outcome_offers_drop_and_keep_never_neutralize(env):
    ready(env)
    env.job.outcome = RestoreOutcome("partial", 4, Path("/dumps/r.log"), 0, {},
                                     ["Restore incomplete: shop is partial (exit 1)."])
    questions = []
    env.tab.confirm = lambda parent, title, msg, danger=False, **k: questions.append(msg) or True
    env.tab.restore_button.click()
    tab = env.tab
    assert len(questions) == 1  # the restore confirmation only, no neutralize question
    assert tab.result.objectName() == "resultBad"
    assert "shop is partial" in tab.result.text()
    assert tab.drop_button.isVisibleTo(tab) and tab.keep_button.isVisibleTo(tab)
    assert tab.drop_button.text() == "Drop incomplete database"
    assert tab.keep_button.text() == "Keep it"
    assert env.neutralized == []


def test_drop_incomplete_confirms_then_drops(env):
    ready(env)
    env.job.outcome = RestoreOutcome("partial", 0, None, 0, {}, ["partial"])
    env.tab.restore_button.click()
    asked = []
    env.tab.confirm = lambda parent, title, msg, danger=False, **k: asked.append(danger) or False
    env.tab.drop_button.click()
    assert asked == [True] and "drop" not in env.job.names()
    env.tab.confirm = lambda *a, **k: True
    env.tab.drop_button.click()
    assert ("drop", "local", "shop") in env.job.calls
    assert not env.tab.drop_button.isVisibleTo(env.tab)
    assert "Dropped" in env.tab.result.text()


def test_keep_partial_hides_buttons_and_keeps_warning(env):
    ready(env)
    env.job.outcome = RestoreOutcome("partial", 0, None, 0, {}, ["partial"])
    env.tab.restore_button.click()
    env.tab.keep_button.click()
    assert not env.tab.drop_button.isVisibleTo(env.tab)
    assert env.tab.result.objectName() == "resultBad" and "partial" in env.tab.result.text()
    assert "drop" not in env.job.names()


def test_cancelled_outcome_is_neutral(env):
    ready(env)
    env.job.outcome = RestoreOutcome("cancelled", 0, None, 0, {}, [])
    env.tab.restore_button.click()
    assert env.tab.result.objectName() not in ("resultBad", "resultOk")
    assert "ancel" in env.tab.result.text()
    assert not env.tab.drop_button.isVisibleTo(env.tab)
    assert env.tab.neutralize_status.text() == "" and env.neutralized == []


def test_ok_neutralize_declined_shows_not_neutralized(env):
    ready(env)
    questions = []

    def confirm(parent, title, msg, danger=False, **k):
        questions.append((msg, danger))
        return len(questions) == 1  # yes to the restore, no to neutralize

    env.tab.confirm = confirm
    env.tab.restore_button.click()
    assert questions[1][0].startswith("Neutralize shop now?") and questions[1][1] is False
    assert env.neutralized == []
    assert NOT_NEUTRALIZED in card_text(env.tab)
    assert env.tab.neutralize_status.objectName() == "resultBad"


def test_ok_neutralize_accepted_shows_status_and_quick_check(env):
    ready(env)
    env.job.outcome = RestoreOutcome("ok", 2, Path("/dumps/restore_shop.log"), 57, dict(QUICK),
                                     ["old filestore kept"])
    env.tab.restore_button.click()
    assert env.neutralized == [("builder", "local", "shop", {"18.0": ["/odoo/addons"]})]
    text = card_text(env.tab)
    assert "Neutralized (Odoo 18.0, 2 files)" in text
    assert NOT_NEUTRALIZED not in text
    for part in ("2.0 MB", "812", "120", str(Path("/dumps/restore_shop.log")), "57", "old filestore kept",
                 "True"):
        assert part in text
    assert "2 error" in text
    assert env.tab.result.objectName() == "resultOk"
    assert env.tab.neutralize_status.objectName() == "resultOk"


def test_neutralize_failure_shows_warning_and_reason(env):
    ready(env)
    env.neutralize_result = NeutralizeResult(False, "18.0", [], "No addons set for Odoo 18.0")
    env.tab.restore_button.click()
    text = env.tab.neutralize_status.text()
    assert NOT_NEUTRALIZED in text and "No addons set for Odoo 18.0" in text
    assert env.tab.neutralize_status.objectName() == "resultBad"


def test_main_window_uses_restore_tab(qapp, tmp_path):
    from test_ui_shell import make_window

    win = make_window(tmp_path)
    assert isinstance(win.tabs.widget(1), RestoreTab)


# --- final review fixes ------------------------------------------------------------------


def test_neutralize_question_uses_yes_no_buttons(env):
    ready(env)
    asked = []

    def confirm(parent, title, msg, danger=False, **k):
        asked.append((title, k))
        return True

    env.tab.confirm = confirm
    env.tab.restore_button.click()
    assert asked[1] == ("Neutralize", {"ok_text": "Yes", "cancel_text": "No"})


def test_neutralize_with_missing_modules_is_not_shown_complete(env):
    ready(env)
    env.neutralize_result = NeutralizeResult(True, "18.0", [Path("a")], missing=["custom_x", "y"])
    env.tab.restore_button.click()
    text = env.tab.neutralize_status.text()
    assert "Not neutralized for modules not found in the addons set: custom_x, y" in text
    assert not text.startswith("Neutralized")
    assert env.tab.neutralize_status.objectName() == "resultBad"
    assert "warning" in env.tab.status.text().lower()


def test_unknown_outcome_status_is_red(env):
    ready(env)
    env.job.outcome = RestoreOutcome("weird", 0, None, 0, {}, [])
    env.tab.restore_button.click()
    assert env.tab.result.objectName() == "resultBad"
    assert "weird" in env.tab.result.text()
    assert env.neutralized == []
