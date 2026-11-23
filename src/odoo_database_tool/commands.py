"""Build the exact `Cmd` for every remote/local operation per connection kind. Qt-free.

Security boundary: local commands are argv lists; remote strings are built from fixed
templates where every user value is validated or `shlex.quote`d, and secrets never
appear in argv (container env, SSH stdin, or the child environment instead).
"""
from __future__ import annotations

import re
import shlex
import shutil
from pathlib import Path

from .models import Connection, Kind, validate_identifier, validate_remote_path
from .ssh import Cmd, ssh_cmd

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_KEYS = ("host", "port", "user", "password")
_NICE = "nice -n 10 "
_LOCAL_HOSTS = ("", "localhost", "127.0.0.1", "::1")
_KIND_LABELS = {Kind.SSH_DOCKER: "SSH + Docker", Kind.SSH_HOST: "SSH + host Postgres",
                Kind.POSTGRES: "Direct Postgres", Kind.LOCAL: "Local Postgres"}

# odoo.conf fallback: $1 is the conf path; exports libpq env vars, then shifts so the
# tool arguments see the same positionals as in env-name mode. "False" means unset;
# no db_host leaves PGHOST unset so libpq uses the socket, as Odoo does.
_CONF_PRELUDE = (
    'c=$1; shift; '
    'v() { x=$(sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$c" | tail -n 1'
    ' | sed "s/[[:space:]]*\\$//"); [ "$x" = False ] || printf %s "$x"; }; '
    'h=$(v db_host); p=$(v db_port); u=$(v db_user); w=$(v db_password); '
    'PGPORT=${p:-5432}; export PGPORT; '
    'if [ -n "$h" ]; then PGHOST=$h; export PGHOST; fi; '
    'if [ -n "$u" ]; then PGUSER=$u; export PGUSER; fi; '
    'if [ -n "$w" ]; then PGPASSWORD=$w; export PGPASSWORD; fi; '
)
_STATS_SCRIPT = 'cd "$1/$2" && find . -type f | wc -l && du -sk . | cut -f1'
_READ_PASSWORD = "IFS= read -r PGPASSWORD; export PGPASSWORD; "
_EXEC_ARGS = _READ_PASSWORD + 'exec "$@"'


def _q(value) -> str:
    return shlex.quote(str(value))


def _sh_c(script: str, args: list[str]) -> str:
    """`sh -c '<script>' sh <args…>`: user values arrive as positionals, never in the script."""
    return " ".join(["sh", "-c", _q(script), "sh"] + [_q(a) for a in args])


class CommandBuilder:
    """`pg_override` (tunnel host/port) only applies to POSTGRES/LOCAL connections."""

    def __init__(self, settings, secrets, ssh_bin: str):
        self.settings = settings
        self.secrets = secrets
        self.ssh_bin = ssh_bin

    # --- connection checks -------------------------------------------------------

    def hostname(self, conn: Connection) -> Cmd:
        if conn.ssh is not None:
            return self._ssh(conn, "hostname")
        if conn.tunnel is not None:
            return ssh_cmd(self.ssh_bin, conn.tunnel, "hostname", conn_id=conn.id,
                           secret_field="tunnel_password")
        raise ValueError("This connection has no SSH host to check.")

    def container_running(self, conn: Connection) -> Cmd:
        self._require(conn, Kind.SSH_DOCKER)
        ctr = validate_identifier(conn.container, "container name")
        return self._ssh(conn, f"{self._sudo(conn)}docker inspect -f "
                               f"{_q('{{.State.Running}}')} {ctr}")

    def list_containers(self, conn: Connection) -> Cmd:
        """Names of the running containers, one per line."""
        self._require(conn, Kind.SSH_DOCKER)
        return self._ssh(conn, f"{self._sudo(conn)}docker ps --format {_q('{{.Names}}')}")

    # --- source side (where the dump runs) ---------------------------------------

    def psql(self, conn: Connection, db: str, sql: str, *, pg_override=None) -> Cmd:
        """Run one statement and print rows unaligned, without headers."""
        validate_identifier(db, "database name")
        if conn.kind == Kind.SSH_DOCKER:
            return self._docker_pg(conn, "psql", '-X -d "$1" -Atc "$2"', [db, sql])
        if conn.kind == Kind.SSH_HOST:
            return self._host_pg(conn, "psql", ["-X", "-d", db, "-Atc", sql])
        return self._local_pg(conn, "psql", ["-X", "-d", db, "-Atc", sql], pg_override)

    def pg_dump_version(self, conn: Connection) -> Cmd:
        if conn.kind == Kind.SSH_DOCKER:
            return self._ssh(conn, self._docker_exec(conn, "pg_dump --version"))
        if conn.kind == Kind.SSH_HOST:
            return self._ssh(conn, f"{self._pg_sudo(conn)}pg_dump --version")
        return Cmd([self._tool("pg_dump"), "--version"])

    def dump_db(self, conn: Connection, db: str, *, pg_override=None) -> Cmd:
        """Custom-format pg_dump streamed to stdout."""
        validate_identifier(db, "database name")
        if conn.kind == Kind.SSH_DOCKER:
            return self._docker_pg(conn, "pg_dump", '-Fc -d "$1"', [db], nice=True)
        if conn.kind == Kind.SSH_HOST:
            return self._host_pg(conn, "pg_dump", ["-Fc", "-d", db], nice=True)
        return self._local_pg(conn, "pg_dump", ["-Fc", "-d", db], pg_override)

    def filestore_stats(self, conn: Connection, db: str) -> Cmd:
        """Prints two lines: file count, then size in KB of `<root>/<db>`."""
        root = self._filestore_root(conn, db)
        if conn.kind == Kind.SSH_DOCKER:
            return self._ssh(conn, self._docker_exec(conn, _sh_c(_STATS_SCRIPT, [root, db])))
        if conn.kind == Kind.SSH_HOST:
            return self._ssh(conn, self._sudo(conn) + _sh_c(_STATS_SCRIPT, [root, db]))
        return Cmd(["sh", "-c", _STATS_SCRIPT, "sh", root, db])

    def filestore_tar(self, conn: Connection, db: str, gzip: bool) -> Cmd:
        """`tar c[z]f - -C <root> <db>` streamed to stdout."""
        root = self._filestore_root(conn, db)
        flags = "czf" if gzip else "cf"
        if conn.kind == Kind.LOCAL:
            return Cmd([shutil.which("tar") or "tar", flags, "-", "-C", root, db])
        tar = f"{_NICE}tar {flags} - -C {_q(root)} {db}"
        if conn.kind == Kind.SSH_DOCKER:
            return self._ssh(conn, self._docker_exec(conn, tar))
        return self._ssh(conn, self._sudo(conn) + tar)

    # --- restore side (local tools, POSTGRES/LOCAL targets only) ------------------

    def dropdb(self, conn: Connection, db: str, *, pg_override=None) -> Cmd:
        validate_identifier(db, "database name")
        return self._local_pg(conn, "dropdb", [db], pg_override)

    def createdb(self, conn: Connection, db: str, owner: str, collate: str, ctype: str,
                 *, pg_override=None) -> Cmd:
        validate_identifier(db, "database name")
        args = ["-T", "template0", "-E", "UTF8", f"--lc-collate={collate}",
                f"--lc-ctype={ctype}", "-O", owner, db]
        return self._local_pg(conn, "createdb", args, pg_override)

    def pg_restore(self, conn: Connection, db: str, dump: Path, owner: str, jobs: int,
                   *, pg_override=None) -> Cmd:
        validate_identifier(db, "database name")
        args = ["--no-owner", "--no-privileges", f"--role={owner}",
                "-j", str(max(1, int(jobs))), "-d", db, str(dump)]
        return self._local_pg(conn, "pg_restore", args, pg_override)

    def vacuum_analyze(self, conn: Connection, db: str, *, pg_override=None) -> Cmd:
        validate_identifier(db, "database name")
        return self._local_pg(conn, "vacuumdb", ["--analyze-in-stages", "-d", db],
                              pg_override)

    def psql_file(self, conn: Connection, db: str, sql_file: Path, *, pg_override=None) -> Cmd:
        validate_identifier(db, "database name")
        args = ["-X", "-v", "ON_ERROR_STOP=1", "--single-transaction", "-d", db,
                "-f", str(sql_file)]
        return self._local_pg(conn, "psql", args, pg_override)

    # --- helpers -----------------------------------------------------------------

    @staticmethod
    def _require(conn: Connection, *kinds: Kind) -> None:
        if conn.kind not in kinds:
            raise ValueError(f"Not supported for a {_KIND_LABELS[conn.kind]} connection.")

    def _ssh(self, conn: Connection, remote: str, stdin: bytes | None = None) -> Cmd:
        return ssh_cmd(self.ssh_bin, conn.ssh, remote, conn_id=conn.id, stdin=stdin)

    @staticmethod
    def _sudo(conn: Connection) -> str:
        return "sudo -n " if conn.ssh is not None and conn.ssh.use_sudo else ""

    @staticmethod
    def _pg_sudo(conn: Connection) -> str:
        if not conn.pg_os_user:
            return ""
        return f"sudo -n -u {validate_identifier(conn.pg_os_user, 'Postgres OS user')} "

    def _tool(self, name: str) -> str:
        info = self.settings.tool(name)
        if info is None:
            raise ValueError(f"{name} not found. Install the PostgreSQL client tools "
                             "or set the path in Settings.")
        return info.path

    def _docker_exec(self, conn: Connection, command: str) -> str:
        ctr = validate_identifier(conn.container, "container name")
        return f"{self._sudo(conn)}docker exec -e LC_ALL=C {ctr} {command}"

    def _docker_pg(self, conn: Connection, tool: str, tool_args: str, args: list[str],
                   nice: bool = False) -> Cmd:
        """Postgres tool inside the container; credentials come from its env or odoo.conf."""
        if conn.odoo_conf_path:
            prelude, endpoint = _CONF_PRELUDE, ""
            args = [validate_remote_path(conn.odoo_conf_path)] + args
        else:
            n = self._env_names(conn)
            prelude = f'PGPASSWORD="${n["password"]}"; export PGPASSWORD; '
            endpoint = f' -h "${n["host"]}" -p "${n["port"]}" -U "${n["user"]}"'
        script = f"{prelude}exec {_NICE if nice else ''}{tool} -w{endpoint} {tool_args}"
        return self._ssh(conn, self._docker_exec(conn, _sh_c(script, args)))

    @staticmethod
    def _env_names(conn: Connection) -> dict:
        names = conn.env_names or {}
        for key in _ENV_KEYS:
            value = names.get(key)
            if not isinstance(value, str) or not _ENV_NAME_RE.fullmatch(value):
                raise ValueError(f"Invalid environment variable name for {key}: {value!r}")
        return names

    def _host_pg(self, conn: Connection, tool: str, args: list[str], nice: bool = False) -> Cmd:
        """Postgres tool on the SSH host; a stored password is fed through stdin."""
        pg = conn.pg
        sudo = self._pg_sudo(conn)
        argv = [tool, "-w"]
        # Peer auth as an OS user needs the Unix socket: drop a loopback name (keep a socket dir).
        socket = bool(sudo) and pg.host.strip() in _LOCAL_HOSTS
        if pg.host and not socket:
            argv += ["-h", pg.host]
        if not (socket and int(pg.port) == 5432):
            argv += ["-p", str(int(pg.port))]
        if pg.user:
            argv += ["-U", pg.user]
        command = " ".join(_q(a) for a in argv + args)
        password = None if pg.no_password else self.secrets.get(conn.id, "pg_password")
        nice_ = _NICE if nice else ""
        if not password:
            remote = f"exec {nice_}{sudo}{command}"
        elif sudo:  # sudo resets the environment, so read the password after it
            remote = f"exec {nice_}{sudo}sh -c {_q(_EXEC_ARGS)} sh {command}"
        else:
            remote = f"{_READ_PASSWORD}exec {nice_}{command}"
        return self._ssh(conn, remote, (password + "\n").encode() if password else None)

    def _local_pg(self, conn: Connection, tool: str, args: list[str], pg_override) -> Cmd:
        """Local Postgres client; password and SSL mode go in the child environment."""
        self._require(conn, Kind.POSTGRES, Kind.LOCAL)
        pg = conn.pg
        host, port = pg_override or (pg.host, pg.port)
        argv = [self._tool(tool)]
        if host:
            argv += ["-h", str(host)]
        argv += ["-p", str(int(port))]
        if pg.user:
            argv += ["-U", pg.user]
        argv += ["-w"] + args
        env = {}
        password = None if pg.no_password else self.secrets.get(conn.id, "pg_password")
        if password:
            env["PGPASSWORD"] = password
        if pg.sslmode:
            env["PGSSLMODE"] = pg.sslmode
        return Cmd(argv, env)

    @staticmethod
    def _filestore_root(conn: Connection, db: str) -> str:
        validate_identifier(db, "database name")
        if not conn.supports_filestore():
            raise ValueError("This connection has no filestore to read.")
        root = conn.effective_filestore_root()
        return root if conn.kind == Kind.LOCAL else validate_remote_path(root)
