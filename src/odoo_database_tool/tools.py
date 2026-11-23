"""Locate and probe local PostgreSQL client tools and ssh. Qt-free."""
from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .common import child_env

PG_TOOLS = ("pg_dump", "pg_restore", "psql", "createdb", "dropdb", "vacuumdb")

# Install-dir globs per platform (spec 9.3); module-level so tests can patch them.
GLOB_ROOTS: dict[str, list[str]] = {
    "darwin": [
        "/opt/homebrew/opt/postgresql@*/bin",
        "/usr/local/opt/postgresql@*/bin",
        "/Applications/Postgres.app/Contents/Versions/*/bin",
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ],
    "linux": ["/usr/lib/postgresql/*/bin", "/usr/local/pgsql/bin"],
    "win32": ["C:/Program Files/PostgreSQL/*/bin"],
}
WINDOWS_SSH = "C:/Windows/System32/OpenSSH/ssh.exe"
_TIMEOUT = 5


@dataclass
class ToolInfo:
    name: str
    path: str
    version: str

    @property
    def major(self) -> int:
        return pg_major(self.version)


def parse_pg_version(text: str) -> str | None:
    """Version number from `<tool> --version` output, e.g. '18.6' or '19devel'."""
    m = re.search(r"\(PostgreSQL\)\s+(\d+(?:\.\d+)*(?:devel|(?:alpha|beta|rc)\d*)?)", text or "")
    return m.group(1) if m else None


def pg_major(version: str) -> int:
    """Major version: 17 for '17.12 (Ubuntu...)', 9 for '9.6.3'."""
    m = re.match(r"\s*(\d+)", version)
    return int(m.group(1)) if m else 0


def parse_openssh_version(text: str) -> tuple[int, int] | None:
    m = re.search(r"OpenSSH_(?:for_Windows_)?(\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _path_key(p: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", p))


def candidate_dirs(platform: str | None = None) -> list[Path]:
    """PATH entries first, then known install dirs with the highest version first."""
    platform = platform or sys.platform
    seen: set[str] = set()
    out: list[Path] = []

    def add(d: str) -> None:
        if d and d not in seen:
            seen.add(d)
            out.append(Path(d))

    for d in os.environ.get("PATH", "").split(os.pathsep):
        add(d)
    key = "linux" if platform.startswith("linux") else platform
    for pattern in GLOB_ROOTS.get(key, []):
        # Sort within each pattern so unversioned dirs keep their listed order
        for d in sorted(glob.glob(pattern), key=_path_key, reverse=True):
            add(d)
    return out


def _exe(name: str) -> str:
    return name + ".exe" if sys.platform == "win32" else name


def _run_version(path: str, flag: str) -> str | None:
    """Output of `<path> <flag>` (stdout + stderr), or None if it cannot run."""
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        r = subprocess.run([path, flag], capture_output=True, text=True,
                           timeout=_TIMEOUT, env=child_env(), **kwargs)
    except (OSError, subprocess.SubprocessError):
        return None
    return (r.stdout or "") + (r.stderr or "")


def _probe(name: str, path: str) -> ToolInfo | None:
    if not os.path.isfile(path):
        return None
    version = parse_pg_version(_run_version(path, "--version") or "")
    return ToolInfo(name, path, version) if version else None


def _version_key(info: ToolInfo) -> tuple:
    """Numeric parts, then 1 for a release so it outranks its pre-releases (18.0 > 18rc1)."""
    m = re.match(r"\d+(?:\.\d+)*", info.version)
    numbers = tuple(int(n) for n in m.group(0).split(".")) if m else ()
    return numbers + (0 if not m or m.end() < len(info.version) else 1,)


def detect_tool(name: str, override: str = "") -> ToolInfo | None:
    """The override if set, else the highest-version `name` found (ties: earlier wins)."""
    if override:
        return _probe(name, override)
    best: ToolInfo | None = None
    for d in candidate_dirs():
        info = _probe(name, str(d / _exe(name)))
        if info and (best is None or _version_key(info) > _version_key(best)):
            best = info
    return best


def detect_ssh(override: str = "") -> str | None:
    """Path of a working ssh client, or None."""
    def works(p: str) -> bool:
        return os.path.isfile(p) and parse_openssh_version(_run_version(p, "-V") or "") is not None

    if override:
        return override if works(override) else None
    paths = [str(d / _exe("ssh")) for d in candidate_dirs()]
    if sys.platform == "win32":
        paths.append(WINDOWS_SSH)
    else:
        paths.append("/usr/bin/ssh")
    for p in paths:
        if works(p):
            return p
    return None
