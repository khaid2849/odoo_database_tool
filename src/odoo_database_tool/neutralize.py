"""Neutralize a restored database by running the modules' neutralize.sql through psql. Qt-free."""
from __future__ import annotations

import os
import re
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .jobs import (JobContext, JobError, last_line, open_tunnel, register_secrets,
                   require_expected_hostname)
from .models import validate_identifier
from .runner import run_capture, run_stream
from .ssh import Tunnel

_SERIES_RE = re.compile(r"(saas~\d+\.\d+|\d+\.\d+)(?:\.|$)")
_SUBDIRS = ("odoo/addons", "addons", "")
_INSTALLED_SQL = ("select name from ir_module_module "
                  "where state in ('installed','to upgrade','to remove') order by name")
_BASE_VERSION_SQL = "select latest_version from ir_module_module where name = 'base'"


@dataclass
class NeutralizeResult:
    """Outcome of neutralize(); it never raises, a failure is ok=False with a reason."""

    ok: bool
    series: Optional[str]
    files: list = field(default_factory=list)
    reason: str = ""
    missing: list = field(default_factory=list)  # installed modules with no folder in the set


def odoo_series(latest_version: Optional[str]) -> Optional[str]:
    """'18.0.1.3' -> '18.0', 'saas~17.4.1.0' -> 'saas~17.4'; None when not an Odoo version."""
    match = _SERIES_RE.match((latest_version or "").strip())
    return match.group(1) if match else None


def neutralize_sql_files(modules: list, dirs: list) -> list:
    """First data/neutralize.sql per module found in dirs, `base` first, then given order."""
    ordered = sorted(set(modules), key=lambda m: (m != "base", modules.index(m)))
    found = []
    for module in ordered:
        try:
            validate_identifier(module, "module name")
        except ValueError:
            continue
        path = _find_module_file(module, dirs)
        if path is not None:
            found.append(path)
    return found


def missing_modules(modules: list, dirs: list) -> list:
    """Valid module names whose folder is in none of dirs (sorted); they cannot be neutralized."""
    missing = []
    for module in sorted(set(modules)):
        try:
            validate_identifier(module, "module name")
        except ValueError:
            continue
        if not any((Path(d).expanduser() / sub / module).is_dir()
                   for d in dirs for sub in _SUBDIRS):
            missing.append(module)
    return missing


def _find_module_file(module: str, dirs: list) -> Optional[Path]:
    for d in dirs:
        root = Path(d).expanduser()
        for sub in _SUBDIRS:
            path = root / sub / module / "data" / "neutralize.sql"
            if path.is_file():
                return path
    return None


def neutralize(builder, target, db: str, addons_sets: dict, ctx: JobContext, run=run_stream,
               capture=run_capture, pg_override=None, tunnel_factory=Tunnel) -> NeutralizeResult:
    """Run the installed modules' neutralize.sql on db; every failure is reported as ok=False."""
    register_secrets(ctx.redactor, builder.secrets, target)
    try:
        require_expected_hostname(builder, target, capture, ctx.redactor)
        with ExitStack() as stack:
            if pg_override is None:
                pg_override = open_tunnel(stack, tunnel_factory, builder.ssh_bin, target)
            return _neutralize(builder, target, db, addons_sets, ctx, run, capture, pg_override)
    except (JobError, ValueError) as exc:
        return NeutralizeResult(False, None, [], ctx.redactor(str(exc)))
    except (RuntimeError, OSError) as exc:  # tunnel could not be opened
        return NeutralizeResult(False, None, [], ctx.redactor(str(exc)))


def _query(builder, target, db, sql, ctx, capture, pg_override) -> str:
    cmd = builder.psql(target, db, sql, pg_override=pg_override)
    rc, out, err = capture(cmd, timeout=60, redactor=ctx.redactor)
    if rc != 0:
        raise JobError(f"Cannot query {db}: {ctx.redactor(last_line(err)) or f'exit {rc}'}.")
    return out


def _neutralize(builder, target, db, addons_sets, ctx, run, capture, pg_override):
    version = _query(builder, target, db, _BASE_VERSION_SQL, ctx, capture, pg_override).strip()
    series = odoo_series(version)
    if series is None:
        return NeutralizeResult(False, None, [], f"Cannot read the Odoo version of {db} "
                                                 f"(base version: {version or 'none'}).")
    dirs = addons_sets.get(series)
    if not dirs:
        return NeutralizeResult(False, series, [], f"No addons set for Odoo {series}: add one in "
                                                   "Settings to enable neutralize.")
    out = _query(builder, target, db, _INSTALLED_SQL, ctx, capture, pg_override)
    modules = [line.strip() for line in out.splitlines() if line.strip()]
    for name in modules:
        try:
            validate_identifier(name, "module name")
        except ValueError:
            ctx.log(f"Skipping module with an unusable name: {name!r}")
    files = neutralize_sql_files(modules, dirs)
    if not files or files[0].parent.parent.name != "base":
        return NeutralizeResult(False, series, files, f"base/data/neutralize.sql not found in "
                                                      f"the Odoo {series} addons set.")
    missing = missing_modules(modules, dirs)
    if missing:
        ctx.log(f"Warning: modules not found in the Odoo {series} addons set (not neutralized): "
                + ", ".join(missing))
    fd, tmp_name = tempfile.mkstemp(suffix=".sql", prefix="odt_neutralize_")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for path in files:
                module = path.parent.parent.name
                ctx.log(f"Neutralize: {module} ({path})")
                fh.write(f"-- {module}\n{path.read_text(encoding='utf-8')}\n")
        ctx.progress(None, "Neutralizing…")
        cmd = builder.psql_file(target, db, tmp, pg_override=pg_override)
        res = run(cmd, None, on_line=ctx.log, cancel=ctx.cancel, redactor=ctx.redactor)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    if not res.ok:
        detail = ctx.redactor(res.error or last_line(res.stderr_tail)) or f"exit {res.rc}"
        return NeutralizeResult(False, series, files, f"Neutralize failed: {detail}", missing)
    ctx.log(f"Neutralized {db} with {len(files)} module file(s).")
    return NeutralizeResult(True, series, files, missing=missing)
