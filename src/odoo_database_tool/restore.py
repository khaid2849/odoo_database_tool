"""Restore job: dump inspection, safety refusals, drop/create/restore, filestore, quick check. Qt-free."""
from __future__ import annotations

import os
import shutil
import tarfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Tuple

from . import filestore
from .archive import Manifest, TocInfo, lookup_sha256, parse_toc, read_manifest, sha256_file
from .common import timestamp
from .jobs import (LIST_DATABASES_SQL, JobContext, JobError, human_bytes, last_line,
                   open_tunnel, parse_database_sizes, register_secrets)
from .models import Connection, is_local_target, validate_identifier
from .runner import RunResult, run_capture, run_stream
from .ssh import Cmd, Tunnel
from .tools import ToolInfo, pg_major

_TOOLS_HINT = "Install the PostgreSQL client tools or set the path in Settings."
_ERROR_MARK = "pg_restore: error"
_QUICK_SQL = (
    ("size", "select pg_database_size(current_database())"),
    ("tables", "select count(*) from pg_tables where schemaname='public'"),
    ("active_users", "select count(*) from res_users where active"),
    ("installed_modules", "select count(*) from ir_module_module where state='installed'"),
    ("neutralized", "select value from ir_config_parameter where key='database.is_neutralized'"),
)
_FILE_PROGRESS_STEP = 200

PgOverride = Optional[Tuple[str, int]]


@dataclass
class DumpInfo:
    path: Path
    size: int
    toc: TocInfo
    sha_status: str  # "ok" | "missing"
    manifest: Optional[Manifest]


@dataclass
class Activity:
    pid: int
    application: str
    user: str
    client: str
    since: str


@dataclass
class RestorePlan:
    dump: DumpInfo
    target: Connection
    db: str
    is_new: bool
    owner: str
    collate: str
    ctype: str
    filestore_src: Optional[Path]
    filestore_root: Optional[Path]
    jobs: int


@dataclass
class RestoreOutcome:
    status: str  # "ok" | "partial" | "cancelled"
    errors: int
    log_path: Optional[Path]
    filestore_files: int
    quick: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


@dataclass
class _Commands:
    """Built before anything destructive runs, so a missing tool cannot strand a dropped DB."""

    dropdb: Optional[Cmd]
    createdb: Cmd
    extensions: list
    pg_restore: Cmd


def count_restore_errors(log_text: str) -> int:
    """Lines reporting a pg_restore error (perl locale noise and warnings don't count)."""
    return sum(1 for line in log_text.splitlines() if _ERROR_MARK in line)


def inspect_dump(path, pg_restore: Optional[ToolInfo], capture=run_capture) -> DumpInfo:
    """Read and validate a dump before it is offered for restore; JobError when unusable."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise JobError(f"Dump file not found: {path}")
    if pg_restore is None:
        raise JobError(f"pg_restore not found. {_TOOLS_HINT}")
    rc, text, err = capture(Cmd([pg_restore.path, "-l", str(path)]), timeout=600)
    if rc != 0:
        raise JobError(f"pg_restore {pg_restore.version} cannot read {path.name}: "
                       f"{last_line(err) or f'exit {rc}'}. The file may be damaged or need a "
                       "newer pg_restore.")
    toc = parse_toc(text)
    if toc.table_data == 0:
        raise JobError(f"{path.name} has no table data.")
    if toc.dumped_from and pg_restore.major < pg_major(toc.dumped_from):
        raise JobError(f"pg_restore {pg_restore.version} is older than the server the dump "
                       f"came from ({toc.dumped_from}). Install pg_restore "
                       f"{pg_major(toc.dumped_from)} or newer and set its path in Settings.")
    expected = lookup_sha256(path.parent, path.name)
    sha_status = "missing"
    if expected is not None:
        try:
            actual = sha256_file(path)
        except OSError as exc:
            raise JobError(f"Cannot read {path.name}: {exc.strerror or exc}") from None
        if actual.lower() != expected.strip().lower():
            raise JobError(f"SHA256 mismatch for {path.name}: the file is damaged or was "
                           "changed after the dump.")
        sha_status = "ok"
    return DumpInfo(path, path.stat().st_size, toc, sha_status,
                    read_manifest(path.with_name(path.name + ".json")))


def _sql_literal(db: str) -> str:
    return "'" + validate_identifier(db, "database name").replace("'", "''") + "'"


def _unlink(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


class RestoreJob:
    """Restore a dump into a Direct/Local Postgres target (spec §9.5)."""

    def __init__(self, builder, settings, ctx: JobContext, run=run_stream, capture=run_capture,
                 tunnel_factory=Tunnel):
        self.builder = builder
        self.settings = settings
        self.ctx = ctx
        self._run = run
        self._capture = capture
        self._tunnel_factory = tunnel_factory
        self._registered: set = set()

    # --- queries ---------------------------------------------------------------------

    def list_databases(self, target: Connection) -> list:
        """(name, size) of the target's databases, without templates and `postgres`."""
        with self._session(target) as override:
            return self._list(target, override)

    def active_connections(self, target: Connection, db: str) -> list:
        with self._session(target) as override:
            return self._active(target, db, override)

    def db_locale(self, target: Connection, db: str) -> tuple:
        """(collation, ctype) of an existing database, kept for the recreate."""
        with self._session(target) as override:
            sql = f"select datcollate, datctype from pg_database where datname = {_sql_literal(db)}"
            rc, out, err = self._psql(target, "postgres", sql, override)
        collate, sep, ctype = out.strip().partition("|")
        if rc != 0 or not sep:
            raise JobError(f"Cannot read the locale of {db}: "
                           f"{last_line(err) if rc else 'database not found'}.")
        return collate, ctype

    def quick_check(self, target: Connection, db: str, toc: TocInfo) -> dict:
        with self._session(target) as override:
            return self._quick(target, db, toc, override)

    def summary(self, plan: RestorePlan) -> list:
        """What execute will do, as lines for the confirmation dialog."""
        t, db = plan.target, plan.db
        lines = []
        if not plan.is_new:
            lines.append(f"Drop database {db} on {t.name} ({self._size_text(t, db)}). "
                         "This cannot be undone.")
        lines.append(f"Create database {db} on {t.name} (owner {plan.owner}, collation "
                     f"{plan.collate}, ctype {plan.ctype}).")
        if plan.dump.toc.extensions:
            lines.append("Create extensions: " + ", ".join(plan.dump.toc.extensions) + ".")
        lines.append(f"Restore {plan.dump.path.name} ({human_bytes(plan.dump.size)}) with "
                     f"{plan.jobs} parallel jobs, then analyze.")
        lines.append(self._filestore_summary(plan))
        if not is_local_target(t):
            lines.append(f"{t.name} is a remote server: type {db} to confirm.")
        return lines

    # --- restore ---------------------------------------------------------------------

    def execute(self, plan: RestorePlan, *, confirmed_text: str = "") -> RestoreOutcome:
        target, db = plan.target, plan.db
        self._register(target)
        self._refuse_unsafe(plan, confirmed_text)
        warnings: list = []
        with self._session(target) as override:
            if not plan.is_new:
                busy = self._active(target, db, override)
                if busy:
                    raise JobError(self._busy_message(db, busy))
            if not plan.dump.path.is_file():
                raise JobError(f"Dump file not found: {plan.dump.path}")
            fs = self._prepare_filestore(plan)
            cmds = self._build_commands(plan, override)
            if self.ctx.cancel.cancelled:
                self.ctx.log("Cancelled before anything was changed.")
                return RestoreOutcome("cancelled", 0, None, 0, {}, [])
            log_path = self._create_log(plan)
            try:
                self._recreate(plan, cmds)  # cancel is ignored until the DB exists again
            except BaseException:
                _unlink(log_path)
                raise
            if self.ctx.cancel.cancelled:
                _unlink(log_path)
                return self._partial(db, None, 0, warnings, "cancelled before the data restore")
            res, errors, error_lines = self._restore_data(plan, cmds.pg_restore, log_path,
                                                          warnings)
            reason = self._hard_failure(res)
            if reason:
                return self._partial(db, log_path, errors, warnings, reason)
            if res.rc != 0 or errors:
                self._warn(warnings, f"pg_restore finished with exit code {res.rc} and {errors} "
                                     f"error line(s); see {log_path.name}. First ones:\n"
                                     + "\n".join(error_lines[:5]))
            self._vacuum(target, db, override, warnings)
            files = self._restore_filestore(fs, warnings) if fs else 0
            if fs is None:
                self._note_kept_filestore(plan, warnings)
            self.ctx.progress(None, "Checking the restored database…")
            quick = self._quick(target, db, plan.dump.toc, override)
        self.ctx.progress(100, "Done")
        self.ctx.log(f"Restore of {db} finished.")
        return RestoreOutcome("ok", errors, log_path, files, quick, warnings)

    def drop_incomplete(self, target: Connection, db: str) -> None:
        """Drop a database left partial by a failed or cancelled restore."""
        self._refuse_target(target)
        with self._session(target) as override:
            cmd = self._cmd(self.builder.dropdb, target, db, pg_override=override)
            self._step(cmd, "dropdb", f"{db} may still exist.")
        self.ctx.log(f"Dropped {db}.")

    # --- refusals and preparation ----------------------------------------------------

    def _refuse_target(self, target: Connection) -> None:
        if target.can_be_restore_target():
            return
        if target.production:
            raise JobError(f"'{target.name}' is marked Production: it cannot be a restore target.")
        raise JobError(f"Restore needs a Direct or Local Postgres connection; "
                       f"'{target.name}' is not one.")

    def _refuse_unsafe(self, plan: RestorePlan, confirmed_text: str) -> None:
        """Checks that need no server; all run before anything is touched."""
        target, db = plan.target, plan.db
        self._refuse_target(target)
        local = is_local_target(target)
        if plan.filestore_src is not None and not local:
            raise JobError("Filestore can only be restored on this machine; remove the "
                           "filestore source for a remote target.")
        if not local and confirmed_text != db:
            raise JobError(f"{target.name} is a remote server: type the database name ({db}) "
                           "to confirm the restore.")
        try:
            validate_identifier(db, "database name")
            validate_identifier(plan.owner, "owner role")
            for ext in plan.dump.toc.extensions:
                validate_identifier(ext, "extension name")
        except ValueError as exc:
            raise JobError(str(exc)) from None

    @staticmethod
    def _busy_message(db: str, busy: list) -> str:
        rows = "\n".join(f"  pid {a.pid}  {a.application or '?'}  {a.user}  {a.client}  "
                         f"since {a.since}" for a in busy)
        return (f"{db} has {len(busy)} other connection(s). Close them (Odoo server, debugger, "
                f"database clients) and try again:\n{rows}")

    def _prepare_filestore(self, plan: RestorePlan) -> Optional[tuple]:
        """Resolve and validate the filestore source/destination; returns (kind, src, dest)."""
        if plan.filestore_src is None:
            return None
        if plan.filestore_root is None:
            raise JobError("Choose the filestore destination folder.")
        root = Path(plan.filestore_root).expanduser()
        try:
            kind, src = filestore.resolve_filestore_source(Path(plan.filestore_src).expanduser())
            dest = filestore.filestore_target(root, plan.db)
            if dest.is_symlink():
                raise ValueError(f"refusing to replace {dest}: it is a symlink")
            filestore.assert_safe_target(dest, root, plan.db)
        except ValueError as exc:
            raise JobError(f"Filestore: {exc}") from None
        src_real, dest_real = Path(os.path.realpath(src)), Path(os.path.realpath(dest))
        if src_real == dest_real or dest_real in src_real.parents or src_real in dest_real.parents:
            raise JobError(f"Filestore: the source {src} overlaps the destination {dest}.")
        return kind, src, dest

    def _build_commands(self, plan: RestorePlan, override: PgOverride) -> _Commands:
        t, db = plan.target, plan.db
        b = self.builder
        return _Commands(
            dropdb=None if plan.is_new else self._cmd(b.dropdb, t, db, pg_override=override),
            createdb=self._cmd(b.createdb, t, db, plan.owner, plan.collate, plan.ctype,
                               pg_override=override),
            extensions=[(ext, self._cmd(b.psql, t, db, f'CREATE EXTENSION IF NOT EXISTS "{ext}"',
                                        pg_override=override))
                        for ext in plan.dump.toc.extensions],
            pg_restore=self._cmd(b.pg_restore, t, db, plan.dump.path, plan.owner, plan.jobs,
                                 pg_override=override),
        )

    def _create_log(self, plan: RestorePlan) -> Path:
        path = plan.dump.path.parent / f"restore_{plan.db}_{timestamp()}.log"
        try:
            path.open("w", encoding="utf-8").close()
        except OSError as exc:
            raise JobError(f"Cannot write the restore log in {path.parent}: "
                           f"{exc.strerror or exc}") from None
        return path

    # --- steps -----------------------------------------------------------------------

    def _recreate(self, plan: RestorePlan, cmds: _Commands) -> None:
        db = plan.db
        if cmds.dropdb is not None:
            self.ctx.progress(None, f"Dropping {db}…")
            self.ctx.log(f"Dropping {db}")
            self._step(cmds.dropdb, "dropdb", f"{db} may still exist; nothing was restored.")
        self.ctx.progress(None, f"Creating {db}…")
        self.ctx.log(f"Creating {db}")
        self._step(cmds.createdb, "createdb",
                   "Nothing was created." if plan.is_new else f"{db} was dropped and not recreated.")
        for ext, cmd in cmds.extensions:
            self.ctx.log(f"Creating extension {ext}")
            rc, _out, err = self._capture(cmd, timeout=300, redactor=self.ctx.redactor)
            if rc != 0:
                raise JobError(
                    f"Cannot create extension {ext} in {db}: {self._red(last_line(err)) or f'exit {rc}'}. "
                    f"The user {plan.target.pg.user or '(default)'} lacks the privilege to create "
                    "it: connect as a superuser or ask an admin to create it. No data was "
                    f"restored; {db} exists and is empty.")

    def _restore_data(self, plan: RestorePlan, cmd: Cmd, log_path: Path,
                      warnings: list) -> tuple:
        """Run pg_restore with its stderr in the log; returns (result, errors, error lines)."""
        self.ctx.progress(None, "Restoring data…")
        self.ctx.log(f"Restoring {plan.dump.path.name} into {plan.db} (log: {log_path})")
        log_failed = []
        with open(log_path, "w", encoding="utf-8") as fh:
            def on_line(line: str) -> None:
                line = self._red(line)
                if not log_failed:
                    try:
                        fh.write(line + "\n")
                    except OSError as exc:
                        log_failed.append(exc.strerror or str(exc))
                self.ctx.log(line)

            res = self._run(cmd, None, on_line=on_line, cancel=self.ctx.cancel,
                            redactor=self.ctx.redactor)
            if res.error and not log_failed:
                fh.write(self._red(res.error) + "\n")
        if log_failed:
            self._warn(warnings, f"Could not write all of {log_path.name}: {log_failed[0]}.")
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = "\n".join(res.stderr_tail)
        error_lines = [ln for ln in text.splitlines() if _ERROR_MARK in ln]
        return res, len(error_lines), error_lines

    def _hard_failure(self, res: RunResult) -> str:
        """Why pg_restore did not run to completion, or "" when it did."""
        if res.cancelled or self.ctx.cancel.cancelled:
            return "cancelled during the data restore"
        if res.error:
            return f"pg_restore could not run: {self._red(res.error)}"
        if res.rc == 127:
            return "pg_restore could not start (exit 127)"
        if res.rc < 0:
            return f"pg_restore was killed (signal {-res.rc})"
        return ""

    def _partial(self, db: str, log_path: Optional[Path], errors: int, warnings: list,
                 reason: str) -> RestoreOutcome:
        log = f" Log: {log_path}." if log_path else ""
        self._warn(warnings, f"Restore incomplete: {db} is partial ({reason}). Drop it or keep "
                             f"it, but do not use it as a complete copy.{log}")
        self.ctx.progress(None, "Restore incomplete")
        return RestoreOutcome("partial", errors, log_path, 0, {}, warnings)

    def _vacuum(self, target: Connection, db: str, override: PgOverride, warnings: list) -> None:
        self.ctx.progress(None, "Analyzing…")
        try:
            cmd = self.builder.vacuum_analyze(target, db, pg_override=override)
        except ValueError as exc:
            self._warn(warnings, f"vacuumdb not run ({exc}); run it later.")
            return
        res = self._run(cmd, None, on_line=self.ctx.log, redactor=self.ctx.redactor)
        if not res.ok:
            detail = self._red(res.error or last_line(res.stderr_tail)) or f"exit {res.rc}"
            self._warn(warnings, f"vacuumdb --analyze-in-stages failed ({detail}); run it later.")

    def _restore_filestore(self, fs: tuple, warnings: list) -> int:
        kind, src, dest = fs
        self.ctx.progress(None, "Restoring filestore…")
        self.ctx.log(f"Replacing filestore {dest} with {src}")

        def on_file(n: int) -> None:
            if n % _FILE_PROGRESS_STEP == 0:
                self.ctx.progress(None, f"Restoring filestore… {n} files")

        try:
            if dest.is_symlink():
                raise ValueError(f"refusing to replace {dest}: it is a symlink")
            if os.path.lexists(dest):
                shutil.rmtree(dest)
            if kind == "tar":
                count = filestore.extract_tar_strip(src, dest, on_file)
            else:
                count = filestore.copy_tree(src, dest, on_file)
        except (OSError, ValueError, tarfile.TarError) as exc:
            self._warn(warnings, f"Filestore restore failed: {exc}. {dest} may be incomplete; "
                                 "restore the filestore again before using attachments.")
            return 0
        self.ctx.log(f"Filestore: {count} files in {dest}")
        return count

    def _note_kept_filestore(self, plan: RestorePlan, warnings: list) -> None:
        if plan.filestore_root is None or not is_local_target(plan.target):
            return
        dest = Path(plan.filestore_root).expanduser() / plan.db
        if dest.exists():
            self._warn(warnings, f"The old filestore in {dest} was kept; attachments may not "
                                 "match this dump.")

    def _quick(self, target: Connection, db: str, toc: TocInfo, override: PgOverride) -> dict:
        quick = {}
        for key, sql in _QUICK_SQL:
            try:
                rc, out, _err = self._psql(target, db, sql, override)
            except JobError:
                rc, out = 1, ""
            value = out.strip().splitlines()[0].strip() if out.strip() else ""
            if rc != 0:
                value = "?"
            elif key == "size":
                value = human_bytes(int(value)) if value.isdigit() else "?"
            elif key != "neutralized" and not value:
                value = "?"
            quick[key] = value
        quick["dump_table_data"] = str(toc.table_data)
        return quick

    # --- helpers ---------------------------------------------------------------------

    def _list(self, target: Connection, override: PgOverride) -> list:
        rc, out, err = self._psql(target, "postgres", LIST_DATABASES_SQL, override)
        if rc != 0:
            raise JobError(f"Cannot list databases on {target.name}: "
                           f"{last_line(err) or f'exit {rc}'}.")
        return [row for row in parse_database_sizes(out) if row[0] != "postgres"]

    def _active(self, target: Connection, db: str, override: PgOverride) -> list:
        sql = ("select pid, coalesce(application_name,''), usename, "
               "coalesce(client_addr::text,'local'), to_char(backend_start,'HH24:MI') "
               f"from pg_stat_activity where datname = {_sql_literal(db)} "
               "and pid <> pg_backend_pid()")
        rc, out, err = self._psql(target, "postgres", sql, override)
        if rc != 0:
            raise JobError(f"Cannot check connections to {db}: {last_line(err) or f'exit {rc}'}.")
        rows = []
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) < 5 or not parts[0].strip().isdigit():
                continue
            # application_name may itself contain "|": take the fixed columns from both ends
            rows.append(Activity(int(parts[0]), "|".join(parts[1:-3]), parts[-3], parts[-2],
                                 parts[-1].strip()))
        return rows

    def _size_text(self, target: Connection, db: str) -> str:
        try:
            sizes = dict(self.list_databases(target))
        except JobError:
            return "size unknown"
        return human_bytes(sizes[db]) if db in sizes else "size unknown"

    def _filestore_summary(self, plan: RestorePlan) -> str:
        if not is_local_target(plan.target):
            return "Filestore can only be restored on this machine; it is left untouched."
        if plan.filestore_root is None:
            return "Leave the filestore untouched."
        dest = Path(plan.filestore_root).expanduser() / plan.db
        if plan.filestore_src is not None:
            if dest.exists():
                return f"Delete filestore {dest} and replace it with {plan.filestore_src}."
            return f"Copy filestore from {plan.filestore_src} to {dest}."
        if dest.exists():
            return f"Leave filestore {dest} untouched (it may not match this dump)."
        return f"No filestore is restored ({dest} does not exist; attachments will be missing)."

    @contextmanager
    def _session(self, target: Connection) -> Iterator[PgOverride]:
        """Secrets registered and, for a tunnelled target, the tunnel open for the block."""
        self._register(target)
        with ExitStack() as stack:
            try:
                override = open_tunnel(stack, self._tunnel_factory, self.builder.ssh_bin, target)
            except (RuntimeError, OSError) as exc:
                raise JobError(self._red(str(exc))) from None
            yield override

    def _register(self, conn: Connection) -> None:
        if conn.id not in self._registered:
            self._registered.add(conn.id)
            register_secrets(self.ctx.redactor, self.builder.secrets, conn)

    def _psql(self, target: Connection, db: str, sql: str, override: PgOverride) -> tuple:
        cmd = self._cmd(self.builder.psql, target, db, sql, pg_override=override)
        return self._capture(cmd, timeout=60, redactor=self.ctx.redactor)

    @staticmethod
    def _cmd(fn, *args, **kwargs) -> Cmd:
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            raise JobError(str(exc)) from None

    def _step(self, cmd: Cmd, what: str, note: str) -> None:
        res = self._run(cmd, None, on_line=self.ctx.log, redactor=self.ctx.redactor)
        if not res.ok:
            detail = self._red(res.error or last_line(res.stderr_tail)) or f"exit {res.rc}"
            raise JobError(f"{what} failed: {detail}. {note}")

    def _red(self, text: str) -> str:
        return self.ctx.redactor(text)

    def _warn(self, warnings: list, text: str) -> None:
        warnings.append(text)
        self.ctx.log(f"Warning: {text}")
