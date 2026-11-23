"""Restore job: dump inspection, safety refusals, drop/create/restore, filestore, quick check. Qt-free."""
from __future__ import annotations

import io
import os
import re
import shutil
import tarfile
import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Tuple

from . import filestore
from .archive import Manifest, TocInfo, lookup_sha256, parse_toc, read_manifest, sha256_file
from .common import timestamp
from .jobs import (LIST_DATABASES_SQL, JobContext, JobError, human_bytes, last_line,
                   open_tunnel, parse_database_sizes, register_secrets, require_expected_hostname)
from .models import Connection, is_local_target, validate_identifier
from .runner import RunResult, run_capture, run_stream
from .ssh import Cmd, Tunnel
from .tools import ToolInfo, pg_major

_TOOLS_HINT = "Install the PostgreSQL client tools or set the path in Settings."
_ERROR_MARK = "pg_restore: error"
# pg_restore prints this when it ran to the end despite errors; a non-zero exit without it is fatal.
_IGNORED_MARK = "pg_restore: warning: errors ignored on restore:"
_TABLES_SQL = "select count(*) from pg_tables where schemaname='public'"
# Below this share of the dump's tables the restore is reported partial, whatever the exit codes.
_MIN_TABLES_PERCENT = 95
_QUICK_SQL = (
    ("size", "select pg_database_size(current_database())"),
    ("tables", _TABLES_SQL),
    ("active_users", "select count(*) from res_users where active"),
    ("installed_modules", "select count(*) from ir_module_module where state='installed'"),
    ("neutralized", "select value from ir_config_parameter where key='database.is_neutralized'"),
)
_FILE_PROGRESS_STEP = 200

# Column options PostgreSQL core accepts; others come from server-specific modules (pg_attver).
_CORE_COLUMN_OPTIONS = frozenset({"n_distinct", "n_distinct_inherited"})
_IDENT = r'(?:[A-Za-z_][A-Za-z0-9_$]*|"(?:[^"]|"")+")'
_COLUMN_OPTIONS_RE = re.compile(
    rf"ALTER TABLE (?:ONLY )?{_IDENT}(?:\.{_IDENT})? ALTER COLUMN {_IDENT} SET \((.+)\);")
_OPTION_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=[^\s,()=';]+")
# pg_restore 17.6+/18 brackets its scripts with these; any other meta-command could run code.
_ALLOWED_META_RE = re.compile(r"\s*\\(restrict|unrestrict) ([A-Za-z0-9]+)\s*")
_RESTRICT_RE = re.compile(r"\\restrict ([A-Za-z0-9]+)\s*")
# First minor of each major whose psql/pg_restore have \restrict; every release from 18 has it.
_RESTRICT_MINOR = {17: 6, 16: 10, 15: 14, 14: 19, 13: 22}
_RESTRICT_VERSIONS = "17.6+, 16.10+, 15.14+, 14.19+, 13.22+ or 18+"
_OWNER_PREFIX_NOTE = "Schema script line numbers are +1 (SET ROLE prepended)"

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
    schema: Optional[Cmd]  # psql runs the rewritten schema script; None for a plain restore
    pg_restore: Cmd  # every section, or only data and post-data after `schema`


def _is_error_line(line: str) -> bool:
    if _ERROR_MARK in line:
        return True
    return line.startswith("psql:") and (": ERROR:" in line or line.startswith("psql: error:"))


def count_restore_errors(log_text: str) -> int:
    """Lines reporting a pg_restore or psql error (perl locale noise and notices don't count)."""
    return sum(1 for line in log_text.splitlines() if _is_error_line(line))


def _unknown_column_options(line: str) -> list:
    """Unsupported option names of an `ALTER COLUMN … SET (…)` statement line, else []."""
    m = _COLUMN_OPTIONS_RE.fullmatch(line)
    if not m:
        return []
    names = []
    for item in m.group(1).split(", "):
        opt = _OPTION_RE.fullmatch(item)
        if not opt:
            return []  # not a plain option list: leave the statement alone
        names.append(opt.group(1))
    return [n for n in names if n.lower() not in _CORE_COLUMN_OPTIONS]


def filter_column_options(script: str) -> tuple:
    """Drop column option statements the target cannot know; returns (script, {option: count}).

    Every other line is kept byte for byte.
    """
    kept, skipped = [], {}
    for line in io.StringIO(script, newline="\n"):
        unknown = _unknown_column_options(line.rstrip("\r\n"))
        if not unknown:
            kept.append(line)
            continue
        for name in unknown:
            skipped[name] = skipped.get(name, 0) + 1
    return "".join(kept), skipped


def supports_restrict(version: str) -> bool:
    """Whether pg_restore/psql of this release have the `\\restrict` safeguard (CVE-2025-8714).

    Pre-releases (18beta1, 19devel) are refused: their fixes cannot be told from the number.
    """
    m = re.match(r"\s*(\d+)(?:\.(\d+))?(?![\w.])", version or "")
    if not m:
        return False
    major, minor = int(m.group(1)), int(m.group(2) or 0)
    if major >= 18:
        return True
    return major in _RESTRICT_MINOR and minor >= _RESTRICT_MINOR[major]


def find_psql_meta_command(script: str, key: str) -> int:
    """1-based number of the first line starting with a backslash, or 0.

    Allowed: one `\\restrict <key>` and any `\\unrestrict <key>`, with the script's own key.
    """
    restricted = False
    for number, line in enumerate(script.split("\n"), 1):
        if not line.lstrip().startswith("\\"):
            continue
        m = _ALLOWED_META_RE.fullmatch(line.rstrip("\r"))
        if not key or not m or m.group(2) != key or (m.group(1) == "restrict" and restricted):
            return number
        restricted = restricted or m.group(1) == "restrict"
    return 0


def _restrict_key(script: str) -> tuple:
    """(key, 0) when `\\restrict <key>` opens the script, else ("", number of the first line).

    Only blank lines and `--` comments may come before it.
    """
    for number, line in enumerate(script.split("\n"), 1):
        line = line.rstrip("\r")
        if not line.strip() or line.startswith("--"):
            continue
        m = _RESTRICT_RE.fullmatch(line)
        return (m.group(1), 0) if m else ("", number)
    return "", 0


@contextmanager
def _temp_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="odt_schema_"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


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


def _sql_literal(value: str, what: str = "database name") -> str:
    return "'" + validate_identifier(value, what).replace("'", "''") + "'"


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
            lines.append(f"{t.name} is a remote server, not this machine.")
        return lines

    # --- restore ---------------------------------------------------------------------

    def execute(self, plan: RestorePlan, *, confirmed_text: str = "") -> RestoreOutcome:
        target, db = plan.target, plan.db
        self._register(target)
        self._refuse_unsafe(plan, confirmed_text)
        warnings: list = []
        with self._session(target) as override, ExitStack() as cleanup:
            if not plan.dump.path.is_file():
                raise JobError(f"Dump file not found: {plan.dump.path}")
            fs = self._prepare_filestore(plan)
            script = cleanup.enter_context(_temp_dir()) / "pre_data.sql"
            pre_data = self._cmd(self.builder.pre_data_script, plan.dump.path, script)
            # The slow local extraction goes first so the checks below stay close to dropdb.
            rewrite = self._write_schema_script(plan, pre_data, script, warnings)
            cmds = self._build_commands(plan, override, script if rewrite else None)
            if not plan.is_new:
                busy = self._active(target, db, override)
                if busy:
                    raise JobError(self._busy_message(db, busy))
            self._check_owner(target, plan.owner, override)
            if self.ctx.cancel.cancelled:
                self.ctx.log("Cancelled before anything was changed.")
                return RestoreOutcome("cancelled", 0, None, 0, {}, [])
            log_path = self._create_log(plan)
            try:
                self._recreate(plan, cmds)  # cancel is ignored until the DB exists again
            except BaseException:
                _unlink(log_path)
                raise
            try:
                outcome = self._after_recreate(plan, cmds, fs, log_path, override, warnings)
            except Exception as exc:  # noqa: BLE001 - the DB exists again: never strand it silently
                detail = self._red(str(exc)) or type(exc).__name__
                return self._partial(db, log_path if log_path.exists() else None, 0, warnings,
                                     "Restore stopped unexpectedly after the database was "
                                     f"recreated: {detail}")
            if outcome.status != "ok":
                return outcome
        self.ctx.progress(100, "Done")
        self.ctx.log(f"Restore of {db} finished.")
        return outcome

    def _after_recreate(self, plan: RestorePlan, cmds: _Commands, fs: Optional[tuple],
                        log_path: Path, override: PgOverride, warnings: list) -> RestoreOutcome:
        """Extensions, data, analyze, filestore and quick check once the DB exists again."""
        target, db = plan.target, plan.db
        failed_ext = self._create_extensions(plan, cmds, warnings)
        if failed_ext:
            _unlink(log_path)
            return self._partial(db, None, 0, warnings,
                                 f"extension {failed_ext} could not be created; no data "
                                 "was restored")
        if self.ctx.cancel.cancelled:
            _unlink(log_path)
            return self._partial(db, None, 0, warnings, "cancelled before the data restore")
        reason, res, error_lines = self._restore_data(plan, cmds, log_path, warnings)
        errors = len(error_lines)
        if reason:
            return self._partial(db, log_path, errors, warnings, reason)
        if res.rc != 0 or errors:
            self._warn(warnings, f"Restore finished with {errors} error line(s) (pg_restore exit "
                                 f"code {res.rc}); see {log_path.name}. First ones:\n"
                                 + "\n".join(error_lines[:5]))
        shortfall = self._table_shortfall(target, db, plan.dump.toc, override)
        if shortfall:
            found, expected = shortfall
            self._warn(warnings, f"Only {found} of {expected} tables were restored; "
                                 f"see {log_path.name}.")
            return self._partial(db, log_path, errors, warnings, "tables are missing")
        self._vacuum(target, db, override, warnings)
        files = self._restore_filestore(fs, warnings) if fs else 0
        if fs is None:
            self._note_kept_filestore(plan, warnings)
        self.ctx.progress(None, "Checking the restored database…")
        quick = self._quick(target, db, plan.dump.toc, override)
        return RestoreOutcome("ok", errors, log_path, files, quick, warnings)

    def drop_incomplete(self, target: Connection, db: str) -> None:
        """Drop a database left partial by a failed or cancelled restore.

        The caller must have obtained explicit user confirmation first.
        """
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
        if os.path.lexists(dest) and not (dest.is_dir() and (
                filestore.is_filestore_dir(dest) or not any(dest.iterdir()))):
            raise JobError(f"{dest} exists and does not look like an Odoo filestore; "
                           "refusing to delete it.")
        src_real, dest_real = Path(os.path.realpath(src)), Path(os.path.realpath(dest))
        if src_real == dest_real or dest_real in src_real.parents or src_real in dest_real.parents:
            raise JobError(f"Filestore: the source {src} overlaps the destination {dest}.")
        return kind, src, dest

    def _build_commands(self, plan: RestorePlan, override: PgOverride,
                        script: Optional[Path]) -> _Commands:
        """`script`: rewritten schema to run with psql; None restores everything with pg_restore."""
        t, db = plan.target, plan.db
        b = self.builder
        return _Commands(
            dropdb=None if plan.is_new else self._cmd(b.dropdb, t, db, pg_override=override),
            createdb=self._cmd(b.createdb, t, db, plan.owner, plan.collate, plan.ctype,
                               pg_override=override),
            extensions=[(ext, self._cmd(b.psql, t, db, f'CREATE EXTENSION IF NOT EXISTS "{ext}"',
                                        pg_override=override))
                        for ext in plan.dump.toc.extensions],
            schema=None if script is None else self._cmd(b.psql_script, t, db, script,
                                                         pg_override=override),
            pg_restore=self._cmd(b.pg_restore, t, db, plan.dump.path, plan.owner, plan.jobs,
                                 sections=None if script is None else ("data", "post-data"),
                                 pg_override=override),
        )

    def _write_schema_script(self, plan: RestorePlan, cmd: Cmd, script: Path,
                             warnings: list) -> bool:
        """Extract the pre-data script; True when it must be rewritten and run with psql.

        pg_restore runs a table's CREATE and its ALTERs as one unit, so a single column option
        the target does not know (e.g. pg_attver) would lose the whole table. Only then is the
        schema rewritten, and only with the \\restrict safeguard against meta-command injection.
        """
        name = plan.dump.path.name
        self.ctx.progress(None, "Reading the schema…")
        self.ctx.log(f"Extracting the schema of {name}")
        rc, _out, err = self._capture(cmd, timeout=1800, redactor=self.ctx.redactor)
        if rc != 0:
            raise JobError(f"pg_restore could not extract the schema of {name}: "
                           f"{self._red(last_line(err)) or f'exit {rc}'}. Nothing was changed.")
        try:
            text = script.read_bytes().decode("utf-8", errors="surrogateescape")
        except OSError as exc:
            raise JobError(f"Cannot read the schema script: {exc.strerror or exc}. "
                           "Nothing was changed.") from None
        text, skipped = filter_column_options(text)
        if not skipped:
            return False
        key = self._require_restrict(text, sorted(skipped))
        text = f'SET ROLE "{plan.owner}";\n{text}'
        line = find_psql_meta_command(text, key)  # defense in depth on top of \\restrict
        if line:
            raise JobError(f"The schema script contains psql meta-commands (line {line}); "
                           "refusing to run it. Nothing was changed.")
        try:
            script.write_bytes(text.encode("utf-8", errors="surrogateescape"))
        except OSError as exc:
            raise JobError(f"Cannot write the schema script: {exc.strerror or exc}. "
                           "Nothing was changed.") from None
        detail = ", ".join(f"{opt} ({n})" for opt, n in sorted(skipped.items()))
        self._warn(warnings, f"Skipped {sum(skipped.values())} column option(s) not supported "
                             f"by the target server: {detail}.")
        return True

    def _require_restrict(self, script: str, options: list) -> str:
        """The script's \\restrict key, once both tools and the script carry the safeguard.

        The key must open the script: an older pg_restore keeps newlines in object names, so a
        later \\restrict could be planted after meta-commands that psql would already have run.
        """
        need = (f"This dump needs a schema rewrite (unsupported column options: "
                f"{', '.join(options)}), which requires PostgreSQL client tools with the "
                f"\\restrict safeguard ({_RESTRICT_VERSIONS}).")
        tools = [self.settings.tool(name) for name in ("pg_restore", "psql")]
        if not all(info is not None and supports_restrict(info.version) for info in tools):
            found = " / ".join(f"{name} {info.version}" if info else f"{name} not found"
                               for name, info in zip(("pg_restore", "psql"), tools))
            raise JobError(f"{need} Found {found}. Update them, then retry. Nothing was changed.")
        key, line = _restrict_key(script)
        if key:
            return key
        if not any(_RESTRICT_RE.fullmatch(ln.rstrip("\r")) for ln in script.split("\n")):
            raise JobError(f"{need} Update pg_restore and psql, then retry. Nothing was changed.")
        # numbered as psql would see it, after the SET ROLE line
        raise JobError(f"The schema script does not start with \\restrict (line {line + 1}); "
                       "refusing to run it. Nothing was changed.")

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

    def _create_extensions(self, plan: RestorePlan, cmds: _Commands, warnings: list) -> str:
        """Pre-create the dump's extensions; returns the first one that failed, or ""."""
        for ext, cmd in cmds.extensions:
            self.ctx.log(f"Creating extension {ext}")
            rc, _out, err = self._capture(cmd, timeout=300, redactor=self.ctx.redactor)
            if rc != 0:
                self._warn(warnings, f"Could not create extension {ext}: the connection user may "
                                     "lack the privilege, or the extension is not installed on "
                                     f"the server. {self._red(last_line(err)) or f'exit {rc}'}")
                return ext
        return ""

    def _restore_data(self, plan: RestorePlan, cmds: _Commands, log_path: Path,
                      warnings: list) -> tuple:
        """pg_restore (after the psql schema script, if any), all stderr going to the log.

        Returns (failure reason or "", pg_restore result or None, error lines).
        """
        self.ctx.log(f"Restoring {plan.dump.path.name} into {plan.db} (log: {log_path})")
        log_failed: list = []
        results = []
        reason, data = "", None
        with open(log_path, "w", encoding="utf-8") as fh:
            if cmds.schema is not None:
                self.ctx.progress(None, "Restoring the schema…")
                fh.write(_OWNER_PREFIX_NOTE + "\n")
                self.ctx.log(_OWNER_PREFIX_NOTE)
                schema, _ignored = self._run_logged(cmds.schema, fh, log_failed)
                results.append(schema)
                reason = self._hard_failure(schema, False, "psql", "schema restore")
                if not reason and self.ctx.cancel.cancelled:
                    reason = "cancelled during the schema restore"
            if not reason:
                self.ctx.progress(None, "Restoring data…")
                data, ignored = self._run_logged(cmds.pg_restore, fh, log_failed)
                results.append(data)
                reason = self._hard_failure(data, ignored, "pg_restore", "data restore")
        if log_failed:
            self._warn(warnings, f"Could not write all of {log_path.name}: {log_failed[0]}.")
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = "\n".join(ln for res in results for ln in res.stderr_tail)
        return reason, data, [ln for ln in text.splitlines() if _is_error_line(ln)]

    def _run_logged(self, cmd: Cmd, fh, log_failed: list) -> tuple:
        """Run with stderr into the open log; returns (result, ignored).

        `ignored` is True when pg_restore reported that it carried on past its errors.
        """
        ignored = []

        def on_line(line: str) -> None:
            line = self._red(line)
            if _IGNORED_MARK in line:
                ignored.append(True)
            if not log_failed:
                try:
                    fh.write(line + "\n")
                except (OSError, ValueError) as exc:  # ValueError: line after close
                    log_failed.append(getattr(exc, "strerror", None) or str(exc))
            self.ctx.log(line)

        res = self._run(cmd, None, on_line=on_line, cancel=self.ctx.cancel,
                        redactor=self.ctx.redactor)
        if res.error and not log_failed:
            fh.write(self._red(res.error) + "\n")
        return res, bool(ignored) or any(_IGNORED_MARK in ln for ln in res.stderr_tail)

    def _hard_failure(self, res: RunResult, ignored: bool, tool: str, stage: str) -> str:
        """Why the step did not run to completion, or "" when it did."""
        if res.error:
            return f"{tool} could not run: {self._red(res.error)}"
        if res.rc == 0:  # finished; a cancel that raced the exit does not undo that
            return ""
        if res.cancelled:
            return f"cancelled during the {stage}"
        if res.rc == 127:
            return f"{tool} could not start (exit 127)"
        if res.rc < 0:
            return f"{tool} was killed (signal {-res.rc})"
        if not ignored:
            last = self._red(last_line(res.stderr_tail))
            return f"{tool} stopped with exit code {res.rc}" + (f": {last}" if last else "")
        return ""

    def _table_shortfall(self, target: Connection, db: str, toc: TocInfo,
                         override: PgOverride) -> Optional[tuple]:
        """(found, expected) when far fewer public tables exist than the dump has; else None."""
        expected = toc.public_table_data
        if expected <= 0:
            return None
        try:
            rc, out, _err = self._psql(target, db, _TABLES_SQL, override)
        except JobError:
            return None
        value = out.strip()
        if rc != 0 or not value.isdigit():
            return None  # unknown: the quick check shows "?"
        found = int(value)
        if found * 100 >= expected * _MIN_TABLES_PERCENT:
            return None
        return found, expected

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
               "coalesce(client_addr::text,'local'), "
               "to_char(backend_start,'YYYY-MM-DD HH24:MI') "
               f"from pg_stat_activity where datname = {_sql_literal(db)} "
               "and pid <> pg_backend_pid() and backend_type = 'client backend'")
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

    def _check_owner(self, target: Connection, owner: str, override: PgOverride) -> None:
        """Refuse before anything is touched if the connection user cannot act as owner."""
        sql = ("select rolname = current_user or pg_has_role(current_user, rolname, 'MEMBER') "
               f"from pg_roles where rolname = {_sql_literal(owner, 'owner role')}")
        rc, out, err = self._psql(target, "postgres", sql, override)
        if rc != 0:
            raise JobError(f"Cannot check role {owner}: {last_line(err) or f'exit {rc}'}.")
        value = out.strip()
        if not value:
            raise JobError(f"Role {owner} does not exist on the target.")
        if value != "t":
            raise JobError(f"The connection user cannot act as role {owner}.")

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
        """Expected hostname checked, secrets registered and the tunnel (if any) open."""
        self._register(target)
        require_expected_hostname(self.builder, target, self._capture, self.ctx.redactor)
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
