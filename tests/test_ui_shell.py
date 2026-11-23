from __future__ import annotations

import threading
from pathlib import Path

import pytest

from conftest import wait_until

from odoo_database_tool.jobs import JobError
from odoo_database_tool.settings import AppSettings
from odoo_database_tool.store import ConnectionStore, Secrets


class FakeKeyring:
    def __init__(self):
        self.data = {}

    def get_password(self, service, key):
        return self.data.get((service, key))

    def set_password(self, service, key, value):
        self.data[(service, key)] = value

    def delete_password(self, service, key):
        self.data.pop((service, key), None)


def make_window(tmp_path):
    from PySide6.QtCore import QSettings
    from odoo_database_tool.ui.main_window import MainWindow

    settings = AppSettings(QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat))
    settings.dump_dir = tmp_path / "dumps"  # never read the real ~/Odoo DB Dumps
    return MainWindow(settings=settings, store=ConnectionStore(tmp_path / "c.json"),
                      secrets=Secrets(FakeKeyring()))


def run_worker(fn):
    from odoo_database_tool.ui.worker import Worker

    w = Worker(fn)
    got = {"ok": [], "failed": [], "log": [], "progress": []}
    w.finished_ok.connect(got["ok"].append)
    w.failed.connect(got["failed"].append)
    w.log.connect(got["log"].append)
    w.progress.connect(lambda p, t: got["progress"].append((p, t)))
    w.start()
    assert wait_until(lambda: got["ok"] or got["failed"])
    w.wait()
    return w, got


def test_main_window_tabs(qapp, tmp_path):
    win = make_window(tmp_path)
    titles = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    assert titles == ["Dump", "Restore", "Connections", "Settings"]
    assert win.windowTitle() == "Odoo Database Tool"


def test_main_window_builder_is_fresh(qapp, tmp_path):
    from odoo_database_tool.commands import CommandBuilder

    win = make_window(tmp_path)
    a, b = win.builder(), win.builder()
    assert isinstance(a, CommandBuilder) and a is not b


def test_log_panel_caps_lines(qapp):
    from odoo_database_tool.ui.log_panel import LogPanel

    panel = LogPanel()
    for i in range(6000):
        panel.append(f"line {i}")
    panel.flush()
    assert panel.log.blockCount() <= 5000
    assert "line 5999" in panel.log.toPlainText()


def test_log_panel_flushes_on_timer(qapp):
    from odoo_database_tool.ui.log_panel import LogPanel

    panel = LogPanel()
    panel.append("hello")
    assert wait_until(lambda: "hello" in panel.log.toPlainText())


def test_log_panel_running_state(qapp):
    from odoo_database_tool.ui.log_panel import LogPanel

    panel = LogPanel()
    panel.show()
    assert not panel.cancel_button.isVisible()
    panel.set_running(True)
    assert panel.cancel_button.isVisible() and panel.bar.isVisible()
    panel.set_progress(None, "Working")
    assert panel.bar.maximum() == 0
    panel.set_progress(40, "Dumping")
    assert panel.bar.maximum() == 100 and panel.bar.value() == 40
    panel.set_running(False)
    assert not panel.cancel_button.isVisible()
    panel.close()


def test_worker_reports_result(qapp):
    _, got = run_worker(lambda ctx: 42)
    assert got["ok"] == [42] and not got["failed"]


def test_worker_forwards_log_and_progress(qapp):
    def fn(ctx):
        ctx.log("hi")
        ctx.progress(50, "half")
        return 1

    _, got = run_worker(fn)
    assert got["log"] == ["hi"] and got["progress"] == [(50, "half")]


def test_worker_job_error(qapp):
    def fn(ctx):
        raise JobError("disk full")

    _, got = run_worker(fn)
    assert got["failed"] == ["disk full"]


def test_worker_unexpected_error_logs_redacted_traceback(qapp):
    def fn(ctx):
        ctx.redactor.add("s3cret")
        raise ValueError("bad s3cret")

    _, got = run_worker(fn)
    assert got["failed"] == ["Unexpected error: ValueError: bad ****"]
    text = "\n".join(got["log"])
    assert "Traceback" in text and "s3cret" not in text


def test_worker_cancel_sets_token(qapp):
    started = threading.Event()

    def fn(ctx):
        started.set()
        ctx.cancel.wait(5)
        return ctx.cancel.cancelled

    from odoo_database_tool.ui.worker import Worker

    w = Worker(fn)
    results = []
    w.finished_ok.connect(results.append)
    w.start()
    assert started.wait(5)
    w.cancel()
    assert wait_until(lambda: results)
    w.wait()
    assert results == [True]


def test_run_job_one_at_a_time(qapp, tmp_path):
    win = make_window(tmp_path)
    states, done = [], []
    win.job_running.connect(states.append)
    release = threading.Event()

    assert win.run_job(lambda ctx: release.wait(5) and "x", done.append) is True
    assert win.log_panel.cancel_button.isHidden() is False
    assert win.run_job(lambda ctx: 1, done.append) is False
    release.set()
    assert wait_until(lambda: done)
    assert done == ["x"] and states == [True, False]
    assert win.log_panel.cancel_button.isHidden()
    assert win.run_job(lambda ctx: 2, done.append) is True
    assert wait_until(lambda: len(done) == 2)


def test_run_job_failure_callback_and_log(qapp, tmp_path):
    win = make_window(tmp_path)
    failed = []

    def boom(ctx):
        raise JobError("nope")

    win.run_job(boom, lambda r: None, failed.append)
    assert wait_until(lambda: failed)
    win.log_panel.flush()
    assert failed == ["nope"] and "nope" in win.log_panel.log.toPlainText()
    assert win.run_job(lambda ctx: 1, lambda r: None) is True
    wait_until(lambda: not win.job_active)


def test_settings_tab_persists_dump_dir(qapp, tmp_path):
    from PySide6.QtCore import QSettings
    from odoo_database_tool.ui.settings_tab import SettingsTab

    settings = AppSettings(QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat))
    tab = SettingsTab(settings, Secrets(FakeKeyring()))
    target = str(tmp_path / "my dumps")
    tab.dump_dir_edit.setText(target)
    tab.dump_dir_edit.editingFinished.emit()
    assert settings.dump_dir == Path(target)
    tab.jobs_spin.setValue(7)
    assert settings.jobs == 7
    tab.theme_combo.setCurrentText("dark")
    assert settings.theme == "dark"
    tab.theme_combo.setCurrentText("system")


def test_settings_tab_addons_cleaning(qapp, tmp_path):
    from PySide6.QtCore import QSettings
    from odoo_database_tool.ui.settings_tab import SettingsTab

    settings = AppSettings(QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat))
    tab = SettingsTab(settings, Secrets(FakeKeyring()))
    assert tab.add_series(" 18.0 ")
    assert tab.add_addons_dir("18.0", f"  {tmp_path}  ")
    assert not tab.add_addons_dir("18.0", "relative/dir")
    assert "absolute" in tab.addons_error.text()
    assert not tab.add_addons_dir("18.0", "   ")
    assert settings.addons_sets == {"18.0": [str(tmp_path)]}
    tab.remove_addons_dir("18.0", str(tmp_path))
    assert settings.addons_sets == {"18.0": []}


def test_settings_tab_redetect_clears_cache(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from odoo_database_tool.ui import settings_tab as st

    settings = AppSettings(QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat))
    tab = st.SettingsTab(settings, Secrets(FakeKeyring()))
    settings._tool_cache[("x", "")] = None
    tab.redetect_button.click()
    assert ("x", "") not in settings._tool_cache


def test_settings_tab_ssh_status_lines(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from odoo_database_tool.ui import settings_tab as st

    monkeypatch.setattr(st, "detect_ssh", lambda override="": "/usr/bin/ssh")
    monkeypatch.setattr(st, "openssh_version", lambda p: (8, 0))
    settings = AppSettings(QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat))
    tab = st.SettingsTab(settings, Secrets(FakeKeyring()))
    assert "8.0" in tab.ssh_status.text()
    assert "8.4" in tab.password_ssh_status.text()

    monkeypatch.setattr(st, "openssh_version", lambda p: (9, 6))
    tab.refresh()
    assert tab.password_ssh_status.text().startswith("Password SSH: available") or \
        "keychain" in tab.password_ssh_status.text()


def test_make_window_uses_temp_dump_dir(qapp, tmp_path):
    win = make_window(tmp_path)
    assert win.settings.dump_dir == tmp_path / "dumps"
    assert win.dump_tab.dest_edit.text() == str(tmp_path / "dumps")


def _long_job(started):
    def fn(ctx):
        started.set()
        ctx.cancel.wait(10)
        return "finished"
    return fn


def test_close_while_job_runs_declined_keeps_window_and_job(qapp, tmp_path):
    win = make_window(tmp_path)
    win.show()
    started, done, asked = threading.Event(), [], []
    win.confirm_quit = lambda: asked.append(1) or False
    assert win.run_job(_long_job(started), done.append)
    assert started.wait(5)
    win.close()
    assert asked == [1] and win.isVisible() and win.job_active
    assert not win._worker._token.cancelled
    win._cancel_job()
    assert wait_until(lambda: not win.job_active)
    assert done == ["finished"]
    win.close()
    assert not win.isVisible()


def test_close_while_job_runs_accepted_cancels_then_closes(qapp, tmp_path):
    win = make_window(tmp_path)
    win.show()
    started, done, asked = threading.Event(), [], []
    win.confirm_quit = lambda: asked.append(1) or True
    assert win.run_job(_long_job(started), done.append)
    assert started.wait(5)
    worker = win._worker
    win.close()
    assert asked == [1]
    assert worker._token.cancelled  # cancel requested; the window waits for the worker
    assert wait_until(lambda: not win.isVisible())
    assert not win.job_active and worker.isFinished()
    assert done == []  # follow-up callbacks (dialogs, next jobs) are skipped while quitting


def test_close_without_job_does_not_ask(qapp, tmp_path):
    win = make_window(tmp_path)
    win.show()
    win.confirm_quit = lambda: pytest.fail("must not ask")
    win.close()
    assert not win.isVisible()
