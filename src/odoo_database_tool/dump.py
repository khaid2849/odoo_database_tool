"""Dump job: preflight checks, streamed pg_dump / filestore tar, verification, manifest. Qt-free."""
from __future__ import annotations

import os
import shutil
import stat
import tarfile
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NoReturn, Optional, Tuple

from . import filestore
from .archive import (Manifest, append_sha256sums, dump_filename, filestore_filename,
                      parse_toc, sha256_file, write_manifest)
from .jobs import (LIST_DATABASES_SQL, Check, JobContext, JobError, human_bytes, last_line,
                   open_tunnel, parse_database_sizes, register_secrets)
from .models import Connection, Kind, validate_identifier
from .runner import RunResult, run_capture, run_stream
from .ssh import Cmd, Tunnel, classify_ssh_error, reason_text
from .tools import parse_pg_version, pg_major

_SSH_KINDS = (Kind.SSH_DOCKER, Kind.SSH_HOST)
_INFO_SQL = "select current_setting('server_version'), pg_database_size(current_database())"
_ODOO_SQL = "select latest_version from ir_module_module where name='base'"
_SSH_HINTS = {
    "host_key_changed": "The SSH host key has changed. Check with the server admin before connecting.",
    "host_key_unknown": "The SSH host key is not trusted yet. Use Test on the Connections tab to accept it.",
    "auth_failed": "SSH authentication failed: check the key, agent or password.",
    "unreachable": "Cannot SSH: VPN/whitelist/key?",
    "connection_lost": "The SSH connection was lost. Check the network and retry.",
}
_TOOLS_HINT = "Install the PostgreSQL client tools or set the path in Settings."
_CANCELLED = "Cancelled. The partial file was deleted."


@dataclass
class DumpOptions:
    conn: Connection
    db: str
    with_db: bool
    with_filestore: bool
    gzip: bool
    dest: Path


@dataclass
class DumpResult:
    files: list[Path]
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Facts:
    """What preflight learned; reused by the dump itself."""

    pg_override: Optional[Tuple[str, int]] = None
    server_version: str = ""
    db_size: int = 0
    pg_dump_version: str = ""
    fs_files: int = 0
    fs_kb: int = 0
    odoo_version: Optional[str] = None


def _sibling(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _existing_parent(path: Path) -> Path:
    path = Path(path).expanduser()
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _dir_size(path: Path) -> int:
    """Bytes of regular files under path (symlinks skipped, as the local tar does)."""
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if not stat.S_ISLNK(st.st_mode):
                total += st.st_size
    return total


def _elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}m {s % 60:02d}s"


class DumpJob:
    """Dump one database and/or its filestore into a folder (spec §9.4)."""

    def __init__(self, builder, settings, ctx: JobContext, run=run_stream, capture=run_capture,
                 tunnel_factory=Tunnel):
        self.builder = builder
        self.settings = settings
        self.ctx = ctx
        self._run = run
        self._capture = capture
        self._tunnel_factory = tunnel_factory
        self._registered: set[str] = set()

    # --- public ----------------------------------------------------------------------

    def list_databases(self, conn: Connection) -> list[tuple[str, int]]:
        """(name, size in bytes) of every non-template database, queried where the dump runs."""
        self._register_secrets(conn)
        with ExitStack() as stack:
            try:
                override = self._open_tunnel(stack, conn)
                cmd = self.builder.psql(conn, "postgres", LIST_DATABASES_SQL, pg_override=override)
            except (RuntimeError, OSError, ValueError) as exc:
                raise JobError(self._red(str(exc))) from None
            rc, out, err = self._capture_cmd(cmd)
        if rc != 0:
            raise JobError(f"Cannot list databases: {self._failure(conn, rc, err)}.")
        return parse_database_sizes(out)

    def preflight(self, opts: DumpOptions) -> list[Check]:
        """Run the checks; failures come back as Check(ok=False), never as exceptions."""
        self._register_secrets(opts.conn)
        with ExitStack() as stack:
            checks, _facts = self._inspect(opts, stack)
        return checks

    def test_connection(self, conn: Connection) -> list[Check]:
        """Connections-tab Test: SSH/hostname, container, listing, server and tool versions."""
        self._register_secrets(conn)
        checks: list[Check] = []
        facts = _Facts()
        with ExitStack() as stack:
            if conn.ssh is not None or conn.tunnel is not None:
                check = self._check_hostname(conn, (conn.ssh or conn.tunnel).expected_hostname)
                checks.append(check)
                if not check.ok:
                    return checks
            if conn.kind == Kind.POSTGRES and conn.tunnel is not None:
                try:
                    facts.pg_override = self._open_tunnel(stack, conn)
                except (RuntimeError, OSError) as exc:
                    checks.append(Check("SSH tunnel", False, self._red(str(exc)),
                                        _SSH_HINTS["unreachable"]))
                    return checks
                checks.append(Check("SSH tunnel", True, f"127.0.0.1:{facts.pg_override[1]} → "
                                                        f"{conn.pg.host}:{conn.pg.port}"))
            if conn.kind == Kind.SSH_DOCKER:
                check = self._check_container(conn)
                checks.append(check)
                if not check.ok:
                    return checks
            opts = DumpOptions(conn, "postgres", True, False, False, Path("."))
            db_checks = self._check_database(opts, facts)
            checks.extend(db_checks)
            if db_checks[0].ok:
                checks.append(self._check_listing(conn, facts))
            checks.append(self._check_pg_restore(facts))
        return checks

    def run(self, opts: DumpOptions, *, confirmed_production: bool = False) -> DumpResult:
        conn = opts.conn
        if conn.production and not confirmed_production:
            raise JobError(f"Production connection '{conn.name}': confirm the dump of "
                           f"{opts.db} before starting.")
        self._register_secrets(conn)
        files: list[Path] = []
        warnings: list[str] = []
        with ExitStack() as stack:
            self.ctx.progress(None, "Checking…")
            checks, facts = self._inspect(opts, stack)
            for c in checks:
                self.ctx.log(self._format_check(c))
            failed = [c for c in checks if not c.ok]
            if failed:
                raise JobError("Checks failed:\n" + "\n".join(
                    f"- {c.name}: {c.detail}" for c in failed))
            dest = Path(opts.dest).expanduser()
            try:
                dest.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise JobError(f"Cannot create {dest}: {exc.strerror or exc}") from None
            now = datetime.now()
            if opts.with_db:
                files.append(self._dump_db(opts, facts, dest, now, warnings))
            if opts.with_filestore:
                try:
                    files.append(self._dump_filestore(opts, facts, dest, now, warnings))
                except JobError as exc:
                    if files:
                        raise JobError(f"{exc}\nThe database dump was saved: {files[0]}") from None
                    raise
        self.ctx.progress(100, "Done")
        self.ctx.log("Done: " + ", ".join(f.name for f in files))
        return DumpResult(files, warnings)

    # --- preflight -------------------------------------------------------------------

    def _inspect(self, opts: DumpOptions, stack: ExitStack) -> tuple[list[Check], _Facts]:
        conn = opts.conn
        checks: list[Check] = []
        facts = _Facts()

        def stop(check: Check) -> tuple[list[Check], _Facts]:
            checks.append(check)
            return checks, facts

        if not (opts.with_db or opts.with_filestore):
            return stop(Check("Selection", False, "Nothing to dump.",
                              "Tick Database and/or Filestore."))
        try:
            validate_identifier(opts.db, "database name")
        except ValueError as exc:
            return stop(Check("Database name", False, str(exc)))
        if opts.with_filestore and not conn.supports_filestore():
            return stop(Check("Filestore", False, "This connection has no filestore to read.",
                              "Set the filestore root in the connection, or untick Filestore."))

        guard = conn.ssh or conn.tunnel
        if guard is not None and guard.expected_hostname:
            check = self._check_hostname(conn, guard.expected_hostname)
            if not check.ok:  # never touch a machine that is not the expected one
                return stop(check)
            checks.append(check)
        if conn.kind == Kind.POSTGRES and conn.tunnel is not None:
            try:
                facts.pg_override = self._open_tunnel(stack, conn)
            except (RuntimeError, OSError) as exc:
                return stop(Check("SSH tunnel", False, self._red(str(exc)), _SSH_HINTS["unreachable"]))
            checks.append(Check("SSH tunnel", True, f"127.0.0.1:{facts.pg_override[1]} → "
                                                    f"{conn.pg.host}:{conn.pg.port}"))
        if conn.kind == Kind.SSH_DOCKER:
            check = self._check_container(conn)
            if not check.ok:
                return stop(check)
            checks.append(check)
        if opts.with_db:
            checks.extend(self._check_database(opts, facts))
        if opts.with_filestore:
            checks.append(self._check_filestore(opts, facts))
        checks.append(self._check_space(opts, facts))
        if opts.with_db:
            checks.append(self._check_pg_restore(facts))
        return checks, facts

    def _check_hostname(self, conn: Connection, expected: str = "") -> Check:
        name = "Hostname"
        try:
            rc, out, err = self._capture_cmd(self.builder.hostname(conn), timeout=30)
        except ValueError as exc:
            return Check(name, False, str(exc))
        if rc != 0:
            return Check(name, False, f"Cannot run hostname: {self._failure(conn, rc, err, ssh=True)}",
                         self._hint(conn, rc, err, "Check the SSH settings of this connection.",
                                    ssh=True))
        actual = out.strip()
        if expected and actual != expected:
            return Check(name, False, f"The machine is '{actual}', expected '{expected}'.",
                         "Wrong machine? Check the SSH host/alias and the expected hostname.")
        return Check(name, True, actual)

    def _check_container(self, conn: Connection) -> Check:
        name = "Container"
        try:
            rc, out, err = self._capture_cmd(self.builder.container_running(conn), timeout=30)
        except ValueError as exc:
            return Check(name, False, str(exc))
        if rc != 0:
            return Check(name, False, f"Cannot inspect container {conn.container}: "
                                      f"{self._failure(conn, rc, err)}",
                         self._hint(conn, rc, err, "Check the container name (and sudo -n)."))
        if out.strip() != "true":
            return Check(name, False, f"Container {conn.container} is not running.",
                         "Start the container or check its name.")
        return Check(name, True, f"{conn.container} is running")

    def _check_database(self, opts: DumpOptions, facts: _Facts) -> list[Check]:
        conn, name = opts.conn, "Database"
        try:
            cmd = self.builder.psql(conn, opts.db, _INFO_SQL, pg_override=facts.pg_override)
        except ValueError as exc:
            return [Check(name, False, str(exc))]
        rc, out, err = self._capture_cmd(cmd)
        version, sep, size = out.strip().rpartition("|")
        if rc != 0 or not sep or not size.isdigit():
            reason = self._failure(conn, rc, err) if rc != 0 else "unexpected output"
            return [Check(name, False, f"Cannot query {opts.db}: {reason}",
                          self._hint(conn, rc, err, "Check the database name, credentials, "
                                                    "pg_hba and firewall."))]
        facts.server_version, facts.db_size = version.strip(), int(size)
        return [Check(name, True, f"{opts.db}: {human_bytes(facts.db_size)}, "
                                  f"PostgreSQL {facts.server_version}"),
                self._check_pg_dump(conn, facts)]

    def _check_pg_dump(self, conn: Connection, facts: _Facts) -> Check:
        name = "pg_dump version"
        if conn.kind in _SSH_KINDS:
            where = "the container" if conn.kind == Kind.SSH_DOCKER else "the server"
            try:
                rc, out, err = self._capture_cmd(self.builder.pg_dump_version(conn))
            except ValueError as exc:
                return Check(name, False, str(exc))
            version = parse_pg_version(out) if rc == 0 else None
            if not version:
                return Check(name, False, f"Cannot read the pg_dump version on {where}: "
                                          f"{self._failure(conn, rc, err)}",
                             self._hint(conn, rc, err, "Is pg_dump installed there?"))
        else:
            where = "this computer"
            info = self.settings.tool("pg_dump")
            if info is None:
                return Check(name, False, "pg_dump not found on this computer.", _TOOLS_HINT)
            version = info.version
        facts.pg_dump_version = version
        server_major = pg_major(facts.server_version)
        if pg_major(version) < server_major:
            hint = (f"The container needs pg_dump {server_major} or newer." if conn.kind == Kind.SSH_DOCKER
                    else f"Install pg_dump {server_major} or newer"
                         + (" and set its path in Settings." if where == "this computer" else "."))
            return Check(name, False, f"pg_dump {version} on {where} is older than the server "
                                      f"({facts.server_version}).", hint)
        return Check(name, True, f"pg_dump {version} on {where}")

    def _check_listing(self, conn: Connection, facts: _Facts) -> Check:
        name = "Database list"
        try:
            cmd = self.builder.psql(conn, "postgres", LIST_DATABASES_SQL, pg_override=facts.pg_override)
        except ValueError as exc:
            return Check(name, False, str(exc))
        rc, out, err = self._capture_cmd(cmd)
        if rc != 0:
            return Check(name, False, f"Cannot list databases: {self._failure(conn, rc, err)}",
                         self._hint(conn, rc, err, "Check the credentials and pg_hba."))
        return Check(name, True, f"{len(parse_database_sizes(out))} databases")

    def _check_filestore(self, opts: DumpOptions, facts: _Facts) -> Check:
        conn, name = opts.conn, "Filestore"
        if conn.kind == Kind.LOCAL:
            base = Path(conn.effective_filestore_root()) / opts.db
            if not base.is_dir():
                return Check(name, False, f"Filestore folder not found: {base}",
                             "Check the filestore root of this connection.")
            files, kb = filestore.count_files(base), (_dir_size(base) + 1023) // 1024
        else:
            try:
                cmd = self.builder.filestore_stats(conn, opts.db)
            except ValueError as exc:
                return Check(name, False, str(exc))
            rc, out, err = self._capture_cmd(cmd)
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if rc != 0 or len(lines) < 2 or not (lines[0].isdigit() and lines[1].isdigit()):
                root = conn.effective_filestore_root()
                return Check(name, False, f"Cannot read {root}/{opts.db}: "
                                          f"{self._failure(conn, rc, err)}",
                             self._hint(conn, rc, err, "Check the filestore root (and sudo -n) "
                                                       "of this connection."))
            files, kb = int(lines[0]), int(lines[1])
        facts.fs_files, facts.fs_kb = files, kb
        return Check(name, True, f"{files} files, {human_bytes(kb * 1024)}")

    @staticmethod
    def _check_space(opts: DumpOptions, facts: _Facts) -> Check:
        name = "Free space"
        need = (facts.db_size if opts.with_db else 0)
        if opts.with_filestore:
            need += facts.fs_kb * 1024 * 11 // 10
        folder = _existing_parent(opts.dest)
        try:
            free = shutil.disk_usage(str(folder)).free
        except OSError as exc:
            return Check(name, False, f"Cannot read free space at {folder}: {exc.strerror or exc}")
        if free < need:
            return Check(name, False, f"{human_bytes(free)} free in {folder}, "
                                      f"need {human_bytes(need)}.",
                         "Free some space or choose another folder.")
        return Check(name, True, f"{human_bytes(free)} free, need {human_bytes(need)}")

    def _check_pg_restore(self, facts: _Facts) -> Check:
        name = "pg_restore"
        info = self.settings.tool("pg_restore")
        if info is None:
            return Check(name, True, "pg_restore not found; the dump will not be verified.",
                         _TOOLS_HINT, warning=True)
        if facts.pg_dump_version and info.major < pg_major(facts.pg_dump_version):
            return Check(name, True, f"pg_restore {info.version} is older than pg_dump "
                                     f"{facts.pg_dump_version}; the dump will not be verified.",
                         f"Install pg_restore {pg_major(facts.pg_dump_version)} or newer.",
                         warning=True)
        return Check(name, True, f"pg_restore {info.version}")

    # --- database dump ---------------------------------------------------------------

    def _dump_db(self, opts: DumpOptions, facts: _Facts, dest: Path, now: datetime,
                 warnings: list[str]) -> Path:
        conn = opts.conn
        out = dest / dump_filename(opts.db, conn.name, now)
        part, log = _sibling(out, ".part"), _sibling(out, ".log")
        try:
            cmd = self.builder.dump_db(conn, opts.db, pg_override=facts.pg_override)
        except ValueError as exc:
            raise JobError(str(exc)) from None
        self._check_cancel()
        self.ctx.log(f"Dumping {opts.db} to {out}")
        self.ctx.progress(None, "Dumping database…")
        start = time.monotonic()

        def on_bytes(n: int) -> None:
            elapsed = _elapsed(time.monotonic() - start)
            self.ctx.progress(None, f"Dumping database… {human_bytes(n)} ({elapsed})")

        res = self._run(cmd, part, on_bytes=on_bytes, on_line=self.ctx.log,
                        cancel=self.ctx.cancel, redactor=self.ctx.redactor)
        self._check_stream(res, conn, part, log, "pg_dump", (0,))
        self.ctx.log(f"Database dump: {human_bytes(res.bytes_out)} in "
                     f"{_elapsed(time.monotonic() - start)}")
        self._verify_db(part, out, log, res.stderr_tail, facts, warnings)
        self._promote(part, out, log, res.stderr_tail)
        self._seal(out, opts, facts, "db")
        return out

    def _verify_db(self, part: Path, out: Path, log: Path, tail: list[str], facts: _Facts,
                   warnings: list[str]) -> None:
        """Check the still-partial file; a bad dump never gets the final name."""
        info = self.settings.tool("pg_restore")
        if info is None:
            return self._warn(warnings, f"pg_restore not found: {out.name} was not verified.")
        if facts.pg_dump_version and info.major < pg_major(facts.pg_dump_version):
            return self._warn(warnings, f"pg_restore {info.version} is older than pg_dump "
                                        f"{facts.pg_dump_version}: {out.name} was not verified.")
        self.ctx.progress(None, "Verifying database dump…")
        rc, text, err = self._capture_cmd(Cmd([info.path, "-l", str(part)]), timeout=600)
        if rc != 0:
            self._reject(part, log, tail + err.splitlines(),
                         f"pg_restore cannot read the dump: {last_line(err) or f'exit {rc}'}.")
        toc = parse_toc(text)
        self.ctx.log(f"TOC entries: {toc.entries}, TABLE DATA: {toc.table_data}")
        if toc.table_data == 0:
            self._reject(part, log, tail, "The dump has no table data.")

    # --- filestore -------------------------------------------------------------------

    def _dump_filestore(self, opts: DumpOptions, facts: _Facts, dest: Path, now: datetime,
                        warnings: list[str]) -> Path:
        conn = opts.conn
        out = dest / filestore_filename(opts.db, conn.name, opts.gzip, now)
        part, log = _sibling(out, ".part"), _sibling(out, ".log")
        total = facts.fs_kb * 1024

        def on_bytes(n: int) -> None:
            # Remote bytes include tar headers (and gzip changes them): cap below 100.
            pct = min(99, n * 100 // total) if total else None
            self.ctx.progress(pct, f"Copying filestore… {human_bytes(n)} of {human_bytes(total)}")

        self._check_cancel()
        self.ctx.log(f"Archiving filestore of {opts.db} to {out}")
        self.ctx.progress(0 if total else None, "Copying filestore…")
        if conn.kind == Kind.LOCAL:
            self._tar_local(opts, part, log, on_bytes)
            tail = []
        else:
            try:
                cmd = self.builder.filestore_tar(conn, opts.db, opts.gzip)
            except ValueError as exc:
                raise JobError(str(exc)) from None
            res = self._run(cmd, part, on_bytes=on_bytes, on_line=self.ctx.log,
                            cancel=self.ctx.cancel, redactor=self.ctx.redactor)
            self._check_stream(res, conn, part, log, "tar", (0, 1))
            tail = res.stderr_tail
            if res.rc == 1:  # GNU tar: files changed while being read (live filestore)
                self._warn(warnings, "tar reported files changed during the copy (normal on a "
                                     f"live system); see {log.name}.")
        count = self._verify_filestore(part, out, log, tail, facts, warnings)
        self._promote(part, out, log, tail)
        self._seal(out, opts, facts, "filestore", count)
        return out

    def _tar_local(self, opts: DumpOptions, part: Path, log: Path, on_bytes) -> None:
        root = Path(opts.conn.effective_filestore_root())
        try:
            filestore.tar_directory(root, opts.db, part, opts.gzip, on_bytes=on_bytes,
                                    cancel=self.ctx.cancel)
        except InterruptedError:
            _unlink(part)
            raise JobError(_CANCELLED) from None
        except (OSError, ValueError, tarfile.TarError) as exc:
            msg = self._red(str(exc))
            self._write_log(log, [msg])
            raise JobError(f"Filestore archive failed: {msg}. Log: {log.name}.") from None

    def _verify_filestore(self, part: Path, out: Path, log: Path, tail: list[str], facts: _Facts,
                          warnings: list[str]) -> int:
        """List the still-partial archive; an unreadable one never gets the final name."""
        self.ctx.progress(None, "Verifying filestore archive…")
        try:
            with tarfile.open(part, "r|*") as tf:
                count = sum(1 for m in tf if not m.isdir())
        except (OSError, tarfile.TarError) as exc:
            self._reject(part, log, tail, f"The filestore archive is unreadable: {exc}.")
        self.ctx.log(f"Files in archive: {count} (source had {facts.fs_files} when checked)")
        diff = abs(facts.fs_files - count)
        if diff > facts.fs_files // 100 + 5:
            self._warn(warnings, f"The archive file count ({count}) differs from the source "
                                 f"({facts.fs_files}) by {diff}; check before relying on {out.name}.")
        return count

    # --- shared steps ----------------------------------------------------------------

    def _check_stream(self, res: RunResult, conn: Connection, part: Path, log: Path,
                      what: str, ok_rcs: tuple) -> None:
        """Delete the .part on cancel; keep .part + .log and raise on failure."""
        if res.cancelled or self.ctx.cancel.cancelled:
            _unlink(part)
            _unlink(log)
            raise JobError(_CANCELLED)
        if res.error or res.rc not in ok_rcs:
            self._write_log(log, res.stderr_tail)
            raise JobError(f"{self._stream_failure(conn, res, what)} "
                           f"Partial file kept: {part.name}. Log: {log.name}.")

    def _reject(self, part: Path, log: Path, tail: list[str], reason: str) -> NoReturn:
        """Verification failed: keep the file as .part, explain in the .log, raise."""
        reason = self._red(reason)
        self._write_log(log, list(tail) + [f"Verification failed: {reason}"])
        raise JobError(f"{reason} The file was kept as {part.name}. Log: {log.name}.")

    def _promote(self, part: Path, out: Path, log: Path, tail: list[str]) -> None:
        """Give a verified file its final name; keep a .log only if stderr had content."""
        try:
            os.replace(part, out)
        except OSError as exc:
            raise JobError(f"Cannot rename {part.name}: {exc.strerror or exc}") from None
        if any(line.strip() for line in tail):
            self._write_log(log, tail)
        else:
            _unlink(log)

    def _stream_failure(self, conn: Connection, res: RunResult, what: str) -> str:
        if res.error:
            return f"{what} failed: {self._red(res.error)}."
        if conn.kind in _SSH_KINDS and res.rc == 255:
            kind = classify_ssh_error("\n".join(res.stderr_tail), 255)
            if kind in (None, "connection_lost"):
                return f"Connection lost during {what} (ssh exit 255)."
            return f"SSH failed during {what}: {reason_text(kind)}."
        last = self._red(last_line(res.stderr_tail))
        return f"{what} failed (exit {res.rc})" + (f": {last}." if last else ".")

    def _seal(self, out: Path, opts: DumpOptions, facts: _Facts, kind: str,
              file_count: int = 0) -> None:
        """Append the checksum and write the manifest next to a finished file."""
        self.ctx.progress(None, f"Checksumming {out.name}…")
        try:
            digest = sha256_file(out)
            append_sha256sums(out.parent, out.name, digest)
            write_manifest(_sibling(out, ".json"), Manifest(
                kind=kind, connection=opts.conn.name, connection_kind=opts.conn.kind.value,
                database=opts.db, created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                size=out.stat().st_size, sha256=digest, server_version=facts.server_version,
                pg_dump_version=facts.pg_dump_version if kind == "db" else "",
                odoo_version=self._odoo_version(opts, facts), file_count=file_count))
        except OSError as exc:
            raise JobError(f"Cannot write checksum/manifest for {out.name}: "
                           f"{exc.strerror or exc}") from None
        self.ctx.log(f"Saved {out.name} ({human_bytes(out.stat().st_size)}), sha256 {digest}")

    def _odoo_version(self, opts: DumpOptions, facts: _Facts) -> str:
        """`base` module version, or "" when it cannot be read (not an Odoo DB, no access)."""
        if facts.odoo_version is None:
            facts.odoo_version = ""
            try:
                cmd = self.builder.psql(opts.conn, opts.db, _ODOO_SQL, pg_override=facts.pg_override)
                rc, out, _err = self._capture_cmd(cmd)
            except ValueError:
                return ""
            if rc == 0:
                facts.odoo_version = out.strip().splitlines()[0].strip() if out.strip() else ""
        return facts.odoo_version

    # --- helpers ---------------------------------------------------------------------

    def _register_secrets(self, conn: Connection) -> None:
        if conn.id in self._registered:
            return
        self._registered.add(conn.id)
        register_secrets(self.ctx.redactor, self.builder.secrets, conn)

    def _open_tunnel(self, stack: ExitStack, conn: Connection) -> Optional[Tuple[str, int]]:
        """Open the SSH tunnel of a direct-Postgres connection; returns the pg_override."""
        return open_tunnel(stack, self._tunnel_factory, self.builder.ssh_bin, conn)

    def _capture_cmd(self, cmd: Cmd, timeout: float = 60) -> tuple[int, str, str]:
        return self._capture(cmd, timeout=timeout, redactor=self.ctx.redactor)

    def _red(self, text: str) -> str:
        return self.ctx.redactor(text)

    def _failure(self, conn: Connection, rc: int, err: str, ssh: bool = False) -> str:
        kind = classify_ssh_error(err, rc) if ssh or conn.kind in _SSH_KINDS else None
        if kind:
            return reason_text(kind)
        return self._red(last_line(err)) or f"exit code {rc}"

    @staticmethod
    def _hint(conn: Connection, rc: int, err: str, default: str, ssh: bool = False) -> str:
        kind = classify_ssh_error(err, rc) if ssh or conn.kind in _SSH_KINDS else None
        return _SSH_HINTS.get(kind or "", default)

    def _write_log(self, path: Path, lines: list[str]) -> None:
        try:
            path.write_text("".join(self._red(line) + "\n" for line in lines), encoding="utf-8")
        except OSError:
            pass

    def _warn(self, warnings: list[str], text: str) -> None:
        warnings.append(text)
        self.ctx.log(f"Warning: {text}")

    def _check_cancel(self) -> None:
        if self.ctx.cancel.cancelled:
            raise JobError("Cancelled.")

    @staticmethod
    def _format_check(c: Check) -> str:
        status = "WARN" if c.warning else ("OK" if c.ok else "FAIL")
        hint = f" ({c.hint})" if c.hint and (c.warning or not c.ok) else ""
        return f"[{status}] {c.name}: {c.detail}{hint}"
