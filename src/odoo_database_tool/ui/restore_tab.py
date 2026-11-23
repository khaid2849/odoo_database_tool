"""Restore tab: inspect a dump, pick a target database, confirm, restore, then neutralize."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from ..archive import matching_filestore, newest_dump
from ..common import odoo_data_dir
from ..jobs import JobError, human_bytes
from ..models import Connection, is_local_target, validate_identifier
from ..neutralize import NeutralizeResult, neutralize
from ..restore import DumpInfo, RestoreJob, RestoreOutcome, RestorePlan, inspect_dump
from ..store import ConnectionStore
from . import dialogs
from .dump_tab import source_label

NEW_DB_LABEL = "Create new database…"
FILESTORE_REMOTE = "Filestore can only be restored on this machine"
NOT_NEUTRALIZED = ("NOT neutralized — starting Odoo on this database may send real emails and "
                   "run real scheduled actions.")
_NEW_DB = "\x00new"  # combo data of the "create new" item; never a valid database name
_RESERVED = ("postgres", "template0", "template1")  # exist on every server, never listed
_ARCHIVES = "Filestore archives (*.tar *.tar.gz *.tgz);;All files (*)"


def format_dump_details(info: DumpInfo) -> str:
    toc, man = info.toc, info.manifest
    lines = [f"{info.path.name} — {human_bytes(info.size)}",
             f"Dumped from PostgreSQL {toc.dumped_from or 'unknown version'} · "
             f"{toc.table_data} tables with data",
             "SHA256 verified" if info.sha_status == "ok"
             else "SHA256 not checked (no entry in SHA256SUMS)"]
    if man is not None:
        odoo = f" · Odoo {man.odoo_version}" if man.odoo_version else ""
        lines.append(f"From {man.connection} · database {man.database}{odoo}")
    return "\n".join(lines)


def format_duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def _matching_filestore(dump: Path) -> Optional[Path]:
    try:
        return matching_filestore(dump)
    except OSError:
        return None


class RestoreTab(QWidget):
    def __init__(self, store: ConnectionStore, window, job_factory=None, inspect_fn=None,
                 neutralize_fn=None, parent=None):
        """`window` provides settings, builder(), run_job() and the job_running signal."""
        super().__init__(parent)
        self.store, self.window = store, window
        # Injectable seams (tests replace these).
        self.job_factory = job_factory or (
            lambda ctx: RestoreJob(self.window.builder(), self.window.settings, ctx))
        self.inspect_fn = inspect_fn or inspect_dump
        self.neutralize_fn = neutralize_fn or neutralize
        self.confirm = dialogs.confirm
        self.confirm_typed = dialogs.confirm_typed
        self.pick_dump = lambda start: QFileDialog.getOpenFileName(
            self, "Dump file", start, "Dumps (*.dump);;All files (*)")[0]
        self.pick_fs_file = lambda start: QFileDialog.getOpenFileName(
            self, "Filestore archive", start, _ARCHIVES)[0]
        self.pick_folder = lambda title, start: QFileDialog.getExistingDirectory(self, title, start)

        self._job_active = False
        self._shown_once = False
        self._info: Optional[DumpInfo] = None
        self._info_text = ""  # dump path text the current _info (or inspection) belongs to
        self._auto_fs_src = ""
        self._databases: list = []
        self._loaded_for: Optional[dict] = None  # target snapshot the database list came from
        self._locale: Optional[tuple] = None  # (target id, db, collate, ctype)
        self._partial: Optional[tuple] = None  # (target, db) of a partial restore

        self._build_ui()
        try:
            newest = newest_dump(window.settings.dump_dir)
        except OSError:
            newest = None
        self.dump_edit.setText(str(newest) if newest else "")
        self.dump_details.setText("Not checked yet." if newest else "Choose a dump file.")

        self.dump_browse.clicked.connect(self.browse_dump)
        self.dump_edit.editingFinished.connect(self._dump_edited)
        self.dump_edit.returnPressed.connect(self.inspect_dump)  # Enter re-checks the same file
        self.dump_edit.textChanged.connect(self._dump_text_changed)
        self.load_button.clicked.connect(self.load_databases)
        self.target_combo.currentIndexChanged.connect(self._target_changed)
        self.db_combo.currentIndexChanged.connect(self._db_changed)
        self.new_db_edit.textChanged.connect(self._update_buttons)
        self.owner_edit.textChanged.connect(self._update_buttons)
        self.fs_file_button.clicked.connect(self.browse_fs_file)
        self.fs_folder_button.clicked.connect(self.browse_fs_folder)
        self.fs_root_button.clicked.connect(self.browse_fs_root)
        self.restore_button.clicked.connect(self.restore)
        self.drop_button.clicked.connect(self.drop_incomplete)
        self.keep_button.clicked.connect(self.keep_partial)
        job_running = getattr(window, "job_running", None)
        if job_running is not None:
            job_running.connect(self._on_job_running)
        connections_changed = getattr(window, "connections_changed", None)
        if connections_changed is not None:
            connections_changed.connect(self.refresh_targets)
        self.refresh_targets()

    def _build_ui(self) -> None:
        self.dump_edit = QLineEdit()
        self.dump_browse = QPushButton("Browse…")
        self.dump_details = QLabel("")
        self.dump_details.setObjectName("muted")
        self.dump_details.setWordWrap(True)
        self.target_combo = QComboBox()
        self.load_button = QPushButton("Load databases")
        self.db_combo = QComboBox()
        self.db_combo.setPlaceholderText("Choose a database…")
        self.new_db_edit = QLineEdit()
        self.new_db_edit.setPlaceholderText("New database name")
        self.new_db_edit.hide()
        self.new_db_hint = QLabel("")
        self.new_db_hint.setObjectName("muted")
        self.new_db_hint.hide()
        self.owner_edit = QLineEdit()

        self.fs_group = QGroupBox("Filestore")
        self.fs_src_edit = QLineEdit()
        self.fs_src_edit.setPlaceholderText("Optional: archive or folder; empty leaves the filestore")
        self.fs_file_button = QPushButton("File…")
        self.fs_folder_button = QPushButton("Folder…")
        self.fs_root_edit = QLineEdit(str(odoo_data_dir() / "filestore"))
        self.fs_root_button = QPushButton("Browse…")
        self.fs_note = QLabel(FILESTORE_REMOTE)
        self.fs_note.hide()

        self.restore_button = QPushButton("Restore")
        self.restore_button.setObjectName("dangerBtn")
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        self.message = self._card_label()
        self.result = self._card_label()
        self.neutralize_status = self._card_label()
        self.drop_button = QPushButton("Drop incomplete database")
        self.drop_button.setObjectName("dangerBtn")
        self.keep_button = QPushButton("Keep it")
        self.drop_button.hide()
        self.keep_button.hide()

        dump_row = QHBoxLayout()
        dump_row.addWidget(self.dump_edit, 1)
        dump_row.addWidget(self.dump_browse)
        target_row = QHBoxLayout()
        target_row.addWidget(self.target_combo, 1)
        target_row.addWidget(self.load_button)
        db_row = QHBoxLayout()
        db_row.addWidget(self.db_combo, 1)
        db_row.addWidget(self.new_db_edit, 1)
        form = QFormLayout()
        form.addRow("Dump file", dump_row)
        form.addRow("", self.dump_details)
        form.addRow("Target", target_row)
        form.addRow("Database", db_row)
        form.addRow("", self.new_db_hint)
        form.addRow("Owner role", self.owner_edit)

        src_row = QHBoxLayout()
        src_row.addWidget(self.fs_src_edit, 1)
        src_row.addWidget(self.fs_file_button)
        src_row.addWidget(self.fs_folder_button)
        root_row = QHBoxLayout()
        root_row.addWidget(self.fs_root_edit, 1)
        root_row.addWidget(self.fs_root_button)
        fs_form = QFormLayout(self.fs_group)
        fs_form.addRow("Source", src_row)
        fs_form.addRow("Destination root", root_row)

        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(self.restore_button)
        partial_row = QHBoxLayout()
        partial_row.addWidget(self.drop_button)
        partial_row.addWidget(self.keep_button)
        partial_row.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.fs_group)
        layout.addWidget(self.fs_note)
        layout.addLayout(actions)
        layout.addWidget(self.status)
        layout.addWidget(self.message)
        layout.addWidget(self.result)
        layout.addWidget(self.neutralize_status)
        layout.addLayout(partial_row)
        layout.addStretch(1)

    @staticmethod
    def _card_label() -> QLabel:
        label = QLabel("")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.hide()
        return label

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._shown_once:
            self._shown_once = True
            if self.dump_edit.text().strip() and not self._info_text:
                self.inspect_dump()

    # --- dump file ---------------------------------------------------------------

    def browse_dump(self) -> None:
        start = self.dump_edit.text().strip() or str(self.window.settings.dump_dir)
        path = self.pick_dump(start)
        if path:
            self.dump_edit.setText(path)
            self.inspect_dump()

    def _dump_edited(self) -> None:
        if self.dump_edit.text().strip() != self._info_text:
            self.inspect_dump()

    def _dump_text_changed(self) -> None:
        if self.dump_edit.text().strip() != self._info_text:
            self._info = None
            self._set(self.dump_details, "muted", "Not checked yet: press Enter to check it.")
        self._update_buttons()

    def inspect_dump(self) -> None:
        text = self.dump_edit.text().strip()
        if not text or self._job_active:
            return
        path = Path(text).expanduser()
        self._info, self._info_text = None, text
        self._set(self.dump_details, "muted", f"Checking {path.name}…")
        self._update_buttons()

        def work(ctx):
            info = self.inspect_fn(path, self.window.settings.tool("pg_restore"))
            return info, _matching_filestore(path)

        def failed(message: str) -> None:
            if text == self.dump_edit.text().strip():
                self._set(self.dump_details, "resultBad", message)

        if not self._start(work, lambda res: self._dump_inspected(text, *res), failed):
            self._info_text = ""
            self._set(self.dump_details, "muted", "Not checked: another operation is running.")

    def _dump_inspected(self, text: str, info: DumpInfo, filestore: Optional[Path]) -> None:
        if text != self.dump_edit.text().strip():
            return  # the user picked another file meanwhile
        self._info = info
        self._set(self.dump_details, "muted", format_dump_details(info))
        current = self.fs_src_edit.text().strip()
        if current in ("", self._auto_fs_src):  # never overwrite a source the user chose
            self._auto_fs_src = str(filestore) if filestore else ""
            self.fs_src_edit.setText(self._auto_fs_src)
        self._update_buttons()

    # --- target / database -------------------------------------------------------

    def selected_target(self) -> Optional[Connection]:
        conn_id = self.target_combo.currentData()
        return self.store.get(conn_id) if conn_id else None

    def refresh_targets(self) -> None:
        keep = self.target_combo.currentData()
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        for conn in self.store.load():
            if conn.can_be_restore_target():
                self.target_combo.addItem(source_label(conn), conn.id)
        index = self.target_combo.findData(keep) if keep else -1
        self.target_combo.setCurrentIndex(index if index >= 0 else (0 if self.target_combo.count() else -1))
        self.target_combo.blockSignals(False)
        if self.target_combo.currentData() != keep:
            self._target_changed()
            return
        conn = self.selected_target()
        if self._loaded_for is not None and (conn is None or conn.to_dict() != self._loaded_for):
            self._clear_databases()  # the connection was edited: its list may be another server's
        self._update_filestore()
        self._update_buttons()

    def _target_changed(self) -> None:
        conn = self.selected_target()
        self.message.hide()
        self._clear_databases()
        self.owner_edit.setText(conn.pg.user if conn is not None and conn.pg else "")
        self._update_filestore()
        self._update_buttons()

    def _update_filestore(self) -> None:
        conn = self.selected_target()
        self.fs_note.setVisible(conn is not None and not is_local_target(conn))

    def load_databases(self) -> None:
        conn = self.selected_target()
        if conn is None or self._job_active:
            return
        self._clear_databases()
        self.message.hide()
        snapshot = conn.to_dict()
        self.status.setText(f"Loading databases from {conn.name}…")
        self._start(lambda ctx: self.job_factory(ctx).list_databases(conn),
                    lambda dbs: self._databases_loaded(snapshot, dbs),
                    lambda msg: self._fail(f"Cannot list databases: {msg}"))

    def _databases_loaded(self, snapshot: dict, databases: list) -> None:
        current = self.selected_target()
        if current is None or current.to_dict() != snapshot:
            return  # the target changed while loading: never mix another server's databases
        self.db_combo.blockSignals(True)
        self.db_combo.clear()
        for name, size in databases:
            self.db_combo.addItem(f"{name} — {human_bytes(size)}", name)
        self.db_combo.addItem(NEW_DB_LABEL, _NEW_DB)
        self.db_combo.setCurrentIndex(-1)  # the database to drop must be picked explicitly
        self.db_combo.blockSignals(False)
        self._databases = [name for name, _size in databases]
        self._loaded_for = snapshot
        self.status.setText(f"{len(databases)} database(s). Choose one to replace, or create "
                            "a new database.")
        self._db_changed()

    def _clear_databases(self) -> None:
        self.db_combo.blockSignals(True)
        self.db_combo.clear()
        self.db_combo.blockSignals(False)
        self._databases, self._loaded_for = [], None
        self._db_changed()

    def _db_changed(self) -> None:
        data = self.db_combo.currentData() if self.db_combo.currentIndex() >= 0 else None
        is_new = data == _NEW_DB
        self.new_db_edit.setVisible(is_new)
        self.new_db_hint.setVisible(is_new)
        self._locale = None
        if data and not is_new:
            self._load_locale(data)
        self._update_buttons()

    def _load_locale(self, db: str) -> None:
        conn = self.selected_target()
        if conn is None:
            return
        self.message.hide()
        self._start(lambda ctx: self.job_factory(ctx).db_locale(conn, db),
                    lambda loc: self._locale_loaded(conn.id, db, loc),
                    lambda msg: self._fail(f"Cannot use {db}: {msg}"))

    def _locale_loaded(self, conn_id: str, db: str, locale: tuple) -> None:
        conn = self.selected_target()
        if conn is None or conn.id != conn_id or self.db_combo.currentData() != db:
            return
        self._locale = (conn_id, db, locale[0], locale[1])
        self._update_buttons()

    def _new_name_problem(self, name: str) -> str:
        if not name:
            return "Enter a name for the new database."
        try:
            validate_identifier(name, "database name")
        except ValueError as exc:
            return str(exc)
        if name in self._databases or name in _RESERVED:
            return f"A database named {name} already exists on the target."
        return ""

    def _choice(self) -> Optional[tuple]:
        """(db, is_new) when a usable database is chosen, else None."""
        if self.db_combo.currentIndex() < 0:
            return None
        data = self.db_combo.currentData()
        if data == _NEW_DB:
            name = self.new_db_edit.text().strip()
            return None if self._new_name_problem(name) else (name, True)
        return data, False

    # --- filestore ---------------------------------------------------------------

    def browse_fs_file(self) -> None:
        path = self.pick_fs_file(self.fs_src_edit.text().strip() or str(self.window.settings.dump_dir))
        if path:
            self.fs_src_edit.setText(path)

    def browse_fs_folder(self) -> None:
        path = self.pick_folder("Filestore folder", self.fs_src_edit.text().strip())
        if path:
            self.fs_src_edit.setText(path)

    def browse_fs_root(self) -> None:
        path = self.pick_folder("Filestore destination root", self.fs_root_edit.text().strip())
        if path:
            self.fs_root_edit.setText(path)

    # --- plan and buttons --------------------------------------------------------

    def build_plan(self) -> Optional[RestorePlan]:
        """The plan for the current inputs, or None while anything required is missing."""
        info, target, choice = self._info, self.selected_target(), self._choice()
        owner = self.owner_edit.text().strip()
        if info is None or self._info_text != self.dump_edit.text().strip():
            return None
        if target is None or choice is None or not owner:
            return None
        db, is_new = choice
        if is_new:
            collate = ctype = "C"
        elif self._locale is not None and self._locale[:2] == (target.id, db):
            collate, ctype = self._locale[2], self._locale[3]
        else:
            return None
        local = is_local_target(target)
        src, root = self.fs_src_edit.text().strip(), self.fs_root_edit.text().strip()
        return RestorePlan(info, target, db, is_new, owner, collate, ctype,
                           Path(src) if local and src else None,
                           Path(root) if local and root else None,
                           self.window.settings.jobs)

    def _on_job_running(self, running: bool) -> None:
        self._job_active = running
        self._update_buttons()

    def _update_buttons(self) -> None:
        idle = not self._job_active
        target = self.selected_target()
        if self.new_db_edit.isVisibleTo(self):
            name = self.new_db_edit.text().strip()
            self.new_db_hint.setText(self._new_name_problem(name) if name else "")
        for w in (self.dump_edit, self.dump_browse, self.target_combo, self.new_db_edit,
                  self.owner_edit, self.drop_button, self.keep_button):
            w.setEnabled(idle)
        self.load_button.setEnabled(idle and target is not None)
        self.db_combo.setEnabled(idle and self.db_combo.count() > 0)
        self.fs_group.setEnabled(idle and target is not None and is_local_target(target))
        self.restore_button.setEnabled(idle and self.build_plan() is not None)

    # --- restore -----------------------------------------------------------------

    def restore(self) -> None:
        plan = self.build_plan()
        if plan is None or self._job_active:
            return
        self._clear_card()
        self.message.hide()
        self.status.setText("Preparing the summary…")
        self._start(lambda ctx: self.job_factory(ctx).summary(plan),
                    lambda lines: self._confirm_restore(plan, lines),
                    lambda msg: self._fail(f"Cannot prepare the restore: {msg}"))

    def _confirm_restore(self, plan: RestorePlan, lines: list) -> None:
        text = "\n".join(f"• {line}" for line in lines)
        remote = not is_local_target(plan.target)
        if remote:
            ok = self.confirm_typed(self, "Restore on a remote server", text, plan.db)
        else:
            ok = self.confirm(self, "Restore database", text, danger=True)
        if not ok:
            self.status.setText("Restore not started.")
            return
        confirmed = plan.db if remote else ""
        started = time.monotonic()
        self.status.setText(f"Restoring {plan.dump.path.name} into {plan.db} on {plan.target.name}…")
        self._start(lambda ctx: self.job_factory(ctx).execute(plan, confirmed_text=confirmed),
                    lambda out: self._restored(plan, out, time.monotonic() - started),
                    self._restore_failed)

    def _restore_failed(self, message: str) -> None:
        self.status.setText("")
        self._clear_databases()  # a failure after the drop leaves the old list wrong
        self._card("resultBad", f"Restore failed: {message}")

    def _restored(self, plan: RestorePlan, outcome: RestoreOutcome, duration: float) -> None:
        if outcome.status == "cancelled":
            self.status.setText("")
            self._card("resultInfo", "Restore cancelled. Nothing was changed.")
            return
        self._clear_databases()  # the server's list changed; reload before the next restore
        if outcome.status == "partial":
            self._show_partial(plan, outcome)
        elif outcome.status == "ok":
            self.status.setText("Restore finished.")
            self._show_ok(plan, outcome, duration, None, outcome.quick)
            if not self.confirm(self, "Neutralize",
                                f"Neutralize {plan.db} now?\n\nThis turns off outgoing mail "
                                "servers, scheduled actions and other integrations in this copy, "
                                "using the installed modules' neutralize.sql files.",
                                ok_text="Yes", cancel_text="No"):
                self._show_ok(plan, outcome, duration, NeutralizeResult(False, None),
                              outcome.quick)
                return
            self._neutralize(plan, outcome, duration)
        else:
            self.status.setText("")
            self._card("resultBad", f"Restore ended with an unknown status '{outcome.status}'. "
                                    f"Check {plan.db} and the log before using it.")

    def _neutralize(self, plan: RestorePlan, outcome: RestoreOutcome, duration: float) -> None:
        def work(ctx):
            res = self.neutralize_fn(self.window.builder(), plan.target, plan.db,
                                     self.window.settings.addons_sets, ctx)
            quick = None
            if res.ok:  # refresh database.is_neutralized and friends
                try:
                    quick = self.job_factory(ctx).quick_check(plan.target, plan.db, plan.dump.toc)
                except JobError:
                    pass
            return res, quick

        def done(value) -> None:
            res, quick = value
            if not res.ok:
                self.status.setText("Neutralize failed.")
            else:
                self.status.setText("Neutralized with warnings." if res.missing else "Neutralized.")
            self._show_ok(plan, outcome, duration, res, quick or outcome.quick)

        def failed(message: str) -> None:
            self.status.setText("Neutralize failed.")
            self._show_ok(plan, outcome, duration, NeutralizeResult(False, None, [], message),
                          outcome.quick)

        self.status.setText(f"Neutralizing {plan.db}…")
        if not self._start(work, done, failed):
            failed("another operation was running, so neutralize did not start.")

    def _show_ok(self, plan: RestorePlan, outcome: RestoreOutcome, duration: float,
                 nres: Optional[NeutralizeResult], quick: dict) -> None:
        q = quick
        log = f" — log: {outcome.log_path}" if outcome.log_path else ""
        files = outcome.filestore_files
        lines = [f"Restored {plan.db} on {plan.target.name} in {format_duration(duration)}.",
                 f"Database size: {q.get('size', '?')}",
                 f"Public tables: {q.get('tables', '?')} (dump: "
                 f"{q.get('dump_table_data', '?')} tables with data)",
                 f"Active users: {q.get('active_users', '?')}",
                 f"Installed modules: {q.get('installed_modules', '?')}",
                 f"database.is_neutralized: {q.get('neutralized') or 'not set'}",
                 f"Restore errors: {outcome.errors} error line(s){log}",
                 "Filestore: not restored" if plan.filestore_src is None and not files
                 else f"Filestore: {files} file(s) restored"]
        lines += [f"⚠ {w}" for w in outcome.warnings]
        self._card("resultOk", "\n".join(lines))
        if nres is None:
            self._set(self.neutralize_status, "resultBad", "Not neutralized yet.")
        elif nres.ok and nres.missing:
            self._set(self.neutralize_status, "resultBad",
                      f"Partly neutralized (Odoo {nres.series}, {len(nres.files)} files).\n"
                      "⚠ Not neutralized for modules not found in the addons set: "
                      + ", ".join(nres.missing))
        elif nres.ok:
            self._set(self.neutralize_status, "resultOk",
                      f"Neutralized (Odoo {nres.series}, {len(nres.files)} files)")
        else:
            reason = f"\n{nres.reason}" if nres.reason else ""
            self._set(self.neutralize_status, "resultBad", NOT_NEUTRALIZED + reason)

    def _show_partial(self, plan: RestorePlan, outcome: RestoreOutcome) -> None:
        self.status.setText("Restore incomplete.")
        lines = [f"Restore incomplete: {plan.db} on {plan.target.name} is partial. It was not "
                 "neutralized and no filestore was restored."]
        if outcome.errors:
            lines.append(f"{outcome.errors} pg_restore error line(s).")
        lines += [f"⚠ {w}" for w in outcome.warnings]
        self._card("resultBad", "\n".join(lines))
        self._partial = (plan.target, plan.db)
        self.drop_button.show()
        self.keep_button.show()

    def drop_incomplete(self) -> None:
        if self._partial is None or self._job_active:
            return
        target, db = self._partial
        if not self.confirm(self, "Drop incomplete database",
                            f"Drop the partial database {db} on {target.name}? "
                            "This cannot be undone.", danger=True):
            return
        self.status.setText(f"Dropping {db}…")
        self._start(lambda ctx: self.job_factory(ctx).drop_incomplete(target, db),
                    lambda _res: self._dropped(db),
                    lambda msg: self._fail(f"Drop failed: {msg}"))

    def _dropped(self, db: str) -> None:
        self._end_partial()
        self.status.setText("")
        self._card("resultOk", f"Dropped the incomplete database {db}.")

    def keep_partial(self) -> None:
        if self._partial is None:
            return
        db = self._partial[1]
        self._end_partial()
        self.result.setText(self.result.text() + f"\nKept {db}: it is partial, do not use it as "
                                                 "a complete copy.")

    def _end_partial(self) -> None:
        self._partial = None
        self.drop_button.hide()
        self.keep_button.hide()

    # --- helpers -----------------------------------------------------------------

    def _start(self, fn, on_done, on_failed) -> bool:
        if self.window.run_job(fn, on_done, on_failed):
            return True
        self.status.setText("Another operation is running.")
        return False

    def _fail(self, message: str) -> None:
        self.status.setText("")
        self._set(self.message, "resultBad", message)

    def _card(self, name: str, text: str) -> None:
        self._end_partial()
        self.neutralize_status.hide()
        self._set(self.result, name, text)

    def _clear_card(self) -> None:
        self._end_partial()
        self.result.hide()
        self.neutralize_status.hide()

    @staticmethod
    def _set(label: QLabel, name: str, text: str) -> None:
        label.setObjectName(name)
        label.setText(text)
        label.style().unpolish(label)
        label.style().polish(label)
        label.show()
