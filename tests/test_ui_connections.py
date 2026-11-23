from __future__ import annotations

import pytest
from keyring.backends.fail import Keyring as FailKeyring
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QDialogButtonBox, QLabel, QLineEdit

from odoo_database_tool.jobs import Check, JobContext
from odoo_database_tool.models import Connection, Kind, SshTarget
from odoo_database_tool.runner import CancelToken, Redactor
from odoo_database_tool.store import ConnectionStore, Secrets
from odoo_database_tool.ui import dialogs
from odoo_database_tool.ui.connection_dialog import ConnectionDialog
from odoo_database_tool.ui.connections_tab import ConnectionsTab, format_checks


class FakeKeyring:
    def __init__(self):
        self.data = {}

    def get_password(self, service, key):
        return self.data.get((service, key))

    def set_password(self, service, key, value):
        self.data[(service, key)] = value

    def delete_password(self, service, key):
        self.data.pop((service, key), None)


class BrokenKeyring(FakeKeyring):
    def set_password(self, service, key, value):
        raise RuntimeError("keychain locked")


@pytest.fixture
def keyring():
    return FakeKeyring()


@pytest.fixture
def secrets(keyring):
    return Secrets(keyring)


def ok_ok(dlg):
    return dlg.buttons.button(QDialogButtonBox.Ok)


# --- dialog ------------------------------------------------------------------------------

def test_postgres_dialog_has_tunnel_and_no_container(qapp, secrets):
    dlg = ConnectionDialog(Connection.new("", Kind.POSTGRES), secrets, (9, 0))
    assert dlg.tunnel_group is not None and dlg.container_edit is None
    docker = ConnectionDialog(Connection.new("", Kind.SSH_DOCKER), secrets, (9, 0))
    assert docker.container_edit is not None and docker.tunnel_group is None


def test_ok_disabled_until_name_host_user_filled(qapp, secrets):
    dlg = ConnectionDialog(Connection.new("", Kind.POSTGRES), secrets, (9, 0))
    dlg.pg_group.host.clear()
    assert not ok_ok(dlg).isEnabled()
    dlg.name_edit.setText("db")
    assert not ok_ok(dlg).isEnabled()
    assert "host" in dlg.error_label.text()
    dlg.pg_group.host.setText("db.example.com")
    dlg.pg_group.user.setText("odoo")
    assert ok_ok(dlg).isEnabled()


def test_password_goes_to_secrets_not_model(qapp, secrets, keyring):
    conn = Connection.new("pg", Kind.POSTGRES)
    conn.pg.host, conn.pg.user = "db", "odoo"
    dlg = ConnectionDialog(conn, secrets, (9, 0))
    assert dlg.pg_group.password.edit.echoMode().name == "Password"
    dlg.pg_group.password.edit.setText("s3cr'et$")
    dlg._accept()
    assert dlg.result() == ConnectionDialog.Accepted
    assert secrets.get(conn.id, "pg_password") == "s3cr'et$"
    assert "s3cr'et$" not in str(dlg.result_connection().to_dict())


def test_empty_password_keeps_stored_and_clear_removes(qapp, secrets):
    conn = Connection.new("pg", Kind.POSTGRES)
    conn.pg.host, conn.pg.user = "db", "odoo"
    secrets.set(conn.id, "pg_password", "old")
    dlg = ConnectionDialog(conn, secrets, (9, 0))
    assert dlg.pg_group.password.edit.placeholderText() == "(unchanged)"
    dlg._accept()
    assert secrets.get(conn.id, "pg_password") == "old"
    dlg = ConnectionDialog(conn, secrets, (9, 0))
    dlg.pg_group.password.clear_button.click()
    dlg._accept()
    assert secrets.get(conn.id, "pg_password") is None


def test_no_password_checkbox_maps_to_model(qapp, secrets):
    conn = Connection.new("pg", Kind.LOCAL)
    conn.pg.user = "odoo"
    conn.name = "local"
    dlg = ConnectionDialog(conn, secrets, (9, 0))
    dlg.pg_group.no_password.setChecked(True)
    assert dlg.result_connection().pg.no_password is True
    assert not dlg.pg_group.password.isEnabled()


def test_secrets_set_failure_keeps_dialog_open(qapp, monkeypatch):
    secrets = Secrets(BrokenKeyring())
    warnings = []
    monkeypatch.setattr(dialogs, "warn", lambda parent, title, text: warnings.append(text))
    conn = Connection.new("pg", Kind.POSTGRES)
    conn.pg.host, conn.pg.user = "db", "odoo"
    dlg = ConnectionDialog(conn, secrets, (9, 0))
    dlg.pg_group.password.edit.setText("topsecret")
    dlg._accept()
    assert dlg.result() != ConnectionDialog.Accepted
    assert "RuntimeError" in warnings[0] and "keychain locked" in warnings[0]
    assert "topsecret" not in warnings[0]


@pytest.mark.parametrize("version,available", [((8, 3), True), (None, True), ((9, 0), False)])
def test_ssh_password_disabled_with_tooltip(qapp, version, available):
    secrets = Secrets(FakeKeyring() if available else FailKeyring())
    dlg = ConnectionDialog(Connection.new("x", Kind.SSH_DOCKER), secrets, version)
    assert not dlg.ssh_group.use_password.isEnabled()
    assert dlg.ssh_group.use_password.toolTip()


def test_ssh_password_enabled_and_tunnel_password(qapp, secrets):
    conn = Connection.new("pg", Kind.POSTGRES)
    conn.pg.host, conn.pg.user = "db", "odoo"
    conn.tunnel = SshTarget(alias="bastion")
    dlg = ConnectionDialog(conn, secrets, (9, 0))
    assert dlg.tunnel_group.use_password.isEnabled()
    dlg.tunnel_group.use_password.setChecked(True)
    dlg.tunnel_group.password.edit.setText("tp")
    dlg._accept()
    assert secrets.get(conn.id, "tunnel_password") == "tp"
    assert dlg.result_connection().tunnel.use_password is True


def test_unchecked_tunnel_means_no_tunnel(qapp, secrets):
    conn = Connection.new("pg", Kind.POSTGRES)
    dlg = ConnectionDialog(conn, secrets, (9, 0))
    assert dlg.result_connection().tunnel is None
    dlg.tunnel_group.setChecked(True)
    assert dlg.result_connection().tunnel is not None


# --- dialogs.py --------------------------------------------------------------------------

def test_confirm_typed_ok_only_for_exact_text(qapp):
    seen = []

    def prompt(dlg):
        edit = dlg.findChild(QLineEdit, "typedEdit")
        ok = dlg.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
        seen.append(ok.isEnabled())
        edit.setText("prod_db ")
        seen.append(ok.isEnabled())
        edit.setText("prod_db")
        seen.append(ok.isEnabled())
        return True

    assert dialogs.confirm_typed(None, "Restore", "Overwrite?", "prod_db", prompt=prompt) is True
    assert seen == [False, False, True]


def test_confirm_and_host_key_use_prompt(qapp):
    assert dialogs.confirm(None, "t", "m", danger=True, prompt=lambda d: False) is False
    shown = []
    assert dialogs.confirm_host_key(
        None, "h", 22, ["256 SHA256:abc h (ED25519)"],
        prompt=lambda d: shown.append(d.findChild(QLabel, "fingerprints").text()) or True)
    assert shown == ["256 SHA256:abc h (ED25519)"]


# --- tab ---------------------------------------------------------------------------------

class FakeWindow(QObject):
    job_running = Signal(bool)

    def __init__(self):
        super().__init__()
        self.settings = object()
        self.jobs = []
        self.refuse = False

    def builder(self):
        class B:
            ssh_bin = "ssh"
        return B()

    def run_job(self, fn, on_done, on_failed=None):
        if self.refuse:
            return False
        ctx = JobContext(log=lambda t: None, progress=lambda p, t: None,
                         cancel=CancelToken(), redactor=Redactor())
        self.jobs.append(fn)
        try:
            result = fn(ctx)
        except Exception as exc:  # noqa: BLE001
            if on_failed:
                on_failed(str(exc))
        else:
            on_done(result)
        return True


@pytest.fixture
def tab(qapp, tmp_path, secrets):
    window = FakeWindow()
    t = ConnectionsTab(ConnectionStore(tmp_path / "c.json"), secrets, window)
    t.window = window
    # Never touch real hosts: host key helpers are faked unless a test overrides them.
    t.resolve_host_port = lambda ssh_bin, target: (target.destination(), target.port)
    t.host_key_known = lambda host, port, ssh_bin="ssh": True
    t.fetch_host_keys = lambda *a, **k: pytest.fail("must not fetch")
    t.add_known_host = lambda lines: pytest.fail("must not add")
    t.confirm = lambda *a, **k: True
    return t


def saved(tab, name="c1", kind=Kind.LOCAL, **fields):
    conn = Connection.new(name, kind)
    for k, v in fields.items():
        setattr(conn, k, v)
    tab.store.upsert(conn)
    tab.refresh(conn.id)
    return conn


def test_production_badge_and_list(tab):
    saved(tab, "dev")
    saved(tab, "live", production=True)
    labels = [(tab.tree.topLevelItem(i).text(0), tab.tree.itemWidget(tab.tree.topLevelItem(i), 2))
              for i in range(2)]
    assert labels[0][1] is None
    assert labels[1][1].text() == "PRODUCTION" and "c0392b" in labels[1][1].styleSheet()


def test_add_emits_and_saves(tab):
    emitted = []
    tab.connections_changed.connect(lambda: emitted.append(1))
    tab.pick_kind = lambda: Kind.LOCAL

    def fake_dialog(conn):
        conn.name = "new local"
        return conn

    tab.open_dialog = fake_dialog
    tab.add()
    assert [c.name for c in tab.store.load()] == ["new local"] and emitted == [1]
    tab.pick_kind = lambda: None
    tab.add()
    assert len(tab.store.load()) == 1


def test_duplicate_copies_fields_not_secrets(tab, secrets):
    conn = saved(tab, "src", Kind.SSH_DOCKER, container="odoo-1", production=True)
    secrets.set(conn.id, "pg_password", "pw")
    emitted = []
    tab.connections_changed.connect(lambda: emitted.append(1))
    tab.duplicate()
    copies = [c for c in tab.store.load() if c.id != conn.id]
    assert len(copies) == 1 and emitted == [1]
    assert copies[0].name == "src (copy)" and copies[0].container == "odoo-1"
    assert copies[0].production is True
    assert secrets.get(copies[0].id, "pg_password") is None
    assert secrets.get(conn.id, "pg_password") == "pw"


def test_delete_confirms_and_removes_secrets(tab, secrets):
    conn = saved(tab)
    secrets.set(conn.id, "pg_password", "pw")
    calls = []
    tab.confirm = lambda parent, title, msg, danger=False: calls.append(danger) or False
    tab.delete()
    assert tab.store.load() and secrets.get(conn.id, "pg_password") == "pw"
    tab.confirm = lambda parent, title, msg, danger=False: calls.append(danger) or True
    emitted = []
    tab.connections_changed.connect(lambda: emitted.append(1))
    tab.delete()
    assert calls == [True, True]
    assert tab.store.load() == [] and secrets.get(conn.id, "pg_password") is None and emitted == [1]


def test_edit_keeps_id_and_emits(tab):
    conn = saved(tab, "old")
    emitted = []
    tab.connections_changed.connect(lambda: emitted.append(1))

    def fake_dialog(c):
        c.name = "renamed"
        return c

    tab.open_dialog = fake_dialog
    tab.edit()
    loaded = tab.store.load()
    assert [(c.id, c.name) for c in loaded] == [(conn.id, "renamed")] and emitted == [1]


def test_test_runs_core_checks_and_shows_results(tab):
    saved(tab)
    seen = []

    class FakeJob:
        def test_connection(self, conn):
            seen.append(conn.name)
            return [Check("Database", True, "ok"), Check("pg_dump", False, "old", "install")]

    tab.job_factory = lambda ctx: FakeJob()
    tab.test()
    text = tab.results.toPlainText()
    assert seen == ["c1"] and "✓ Database: ok" in text and "✗ pg_dump: old  (install)" in text
    assert "1 check(s) failed" in tab.status.text()


def test_test_unknown_host_key_accept_adds_then_tests(tab):
    saved(tab, "ssh", Kind.SSH_HOST, ssh=SshTarget(alias="box"))
    tab.host_key_known = lambda host, port, ssh_bin="ssh": False
    tab.fetch_host_keys = lambda host, port, ssh_bin="ssh": (["box ssh-ed25519 AAA"], ["256 SHA256:x"])
    order = []
    tab.confirm_host_key = lambda parent, host, port, fps: order.append(("confirm", host, port, fps)) or True
    tab.add_known_host = lambda lines: order.append(("add", lines))

    class FakeJob:
        def test_connection(self, conn):
            order.append(("test",))
            return [Check("Hostname", True, "x")]

    tab.job_factory = lambda ctx: FakeJob()
    tab.test()
    assert order == [("confirm", "box", 22, ["256 SHA256:x"]), ("add", ["box ssh-ed25519 AAA"]), ("test",)]


def test_test_unknown_host_key_cancel_aborts(tab):
    saved(tab, "ssh", Kind.SSH_HOST, ssh=SshTarget(alias="box"))
    tab.host_key_known = lambda *a, **k: False
    tab.fetch_host_keys = lambda *a, **k: (["l"], ["fp"])
    tab.confirm_host_key = lambda *a: False
    tab.job_factory = lambda ctx: pytest.fail("must not test")
    tab.test()
    assert "cancelled" in tab.status.text()


def test_test_checks_tunnel_host_key_too(tab):
    conn = saved(tab, "pg", Kind.POSTGRES, tunnel=SshTarget(host="jump.example", port=2222))
    asked = []
    tab.host_key_known = lambda host, port, ssh_bin="ssh": asked.append((host, port)) or True

    class FakeJob:
        def test_connection(self, c):
            return []

    tab.job_factory = lambda ctx: FakeJob()
    tab.test()
    assert asked == [("jump.example", 2222)] and conn


def test_fetch_failure_reported(tab):
    saved(tab, "ssh", Kind.SSH_HOST, ssh=SshTarget(alias="box"))
    tab.host_key_known = lambda *a, **k: False

    def boom(*a, **k):
        raise RuntimeError("Could not fetch host keys from box:22")

    tab.fetch_host_keys = boom
    tab.test()
    assert "Could not fetch" in tab.results.toPlainText()


def test_test_button_disabled_while_job_runs(tab):
    saved(tab)
    assert tab.test_button.isEnabled()
    tab.window.job_running.emit(True)
    assert not tab.test_button.isEnabled()
    tab.window.job_running.emit(False)
    assert tab.test_button.isEnabled()


def test_format_checks_marks():
    text = format_checks([Check("a", True), Check("b", True, "w", "h", warning=True),
                          Check("c", False, "d", "hint")])
    assert text.splitlines() == ["✓ a: ", "! b: w  (h)", "✗ c: d  (hint)"]


def test_main_window_uses_connections_tab(qapp, tmp_path):
    from test_ui_shell import make_window
    win = make_window(tmp_path)
    assert isinstance(win.connections_tab, ConnectionsTab)
