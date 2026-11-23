"""Odoo filestore helpers: detection, safe tar extraction, copy and local tar."""
from __future__ import annotations

import os
import posixpath
import re
import shutil
import tarfile
from pathlib import Path
from typing import Callable, Optional, Tuple

from .models import validate_identifier
from .runner import CancelToken

_HEX2 = re.compile(r"^[0-9a-fA-F]{2}$")
_TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz")
_CHUNK = 1024 * 1024


def is_filestore_dir(p: Path) -> bool:
    """True when p holds at least one subfolder named with 2 hex chars."""
    try:
        return p.is_dir() and any(c.is_dir() and _HEX2.match(c.name) for c in p.iterdir())
    except OSError:
        return False


def resolve_filestore_source(p: Path) -> Tuple[str, Path]:
    """Classify a restore source as ("tar", file) or ("dir", filestore folder)."""
    if p.is_file():
        if p.name.lower().endswith(_TAR_SUFFIXES):
            return ("tar", p)
        raise ValueError(f"Not a .tar/.tar.gz/.tgz file: {p}")
    if not p.is_dir():
        raise ValueError(f"Path does not exist: {p}")
    if is_filestore_dir(p):
        return ("dir", p)
    subs = sorted(c for c in p.iterdir() if is_filestore_dir(c))
    if len(subs) == 1:
        return ("dir", subs[0])
    if not subs:
        raise ValueError(f"No filestore folder found in {p}")
    raise ValueError(f"{p} holds {len(subs)} filestore folders; pick one")


def filestore_target(root: Path, db: str) -> Path:
    """Return root/db after validating the database name."""
    return Path(root) / validate_identifier(db, "database name")


def assert_safe_target(target: Path, root: Path, db: str) -> None:
    validate_identifier(db, "database name")
    expected = Path(root).resolve() / db
    if Path(target).resolve() != expected:
        raise ValueError(f"Refusing filestore path {target}: must be exactly {expected}")


def _strip_name(name: str) -> str:
    """Drop the archive's top folder and validate; returns "" for the top entry."""
    if name.startswith("/") or "\\" in name or re.match(r"^[A-Za-z]:", name):
        raise ValueError(f"Unsafe path in archive: {name!r}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ValueError(f"Unsafe path in archive: {name!r}")
    return "/".join(parts[1:])


def _safe_output(dest_real: Path, rel: str) -> Path:
    """Validate rel against dest before anything is created; refuse symlinks on the way."""
    cur = dest_real
    for part in rel.split("/"):
        cur = cur / part
        if cur.is_symlink():
            raise ValueError(f"Refusing to write through a symlink: {cur}")
    anc = cur
    while not os.path.lexists(anc):
        anc = anc.parent
    if os.path.commonpath([dest_real, Path(os.path.realpath(anc))]) != str(dest_real):
        raise ValueError(f"Refusing to write outside destination: {cur}")
    return cur


def extract_tar_strip(archive: Path, dest: Path,
                      on_file: Optional[Callable[[int], None]] = None) -> int:
    """Extract a tar[.gz] into dest, stripping the top folder; returns files written."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    dest_real = Path(os.path.realpath(dest))
    count = 0
    with tarfile.open(archive, "r|*") as tf:
        for m in tf:
            rel = _strip_name(m.name)
            if not rel:
                if m.isdir():
                    continue
                raise ValueError(f"Unsafe empty path in archive: {m.name!r}")
            if m.issym() or m.islnk():
                raise ValueError("links are not allowed in a filestore archive")
            if not (m.isreg() or m.isdir()):
                raise ValueError(f"Unsupported member type in archive: {m.name!r}")
            out = _safe_output(dest_real, rel)
            if m.isdir():
                out.mkdir(parents=True, exist_ok=True)
                os.chmod(out, 0o755)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, "wb") as fh:
                shutil.copyfileobj(tf.extractfile(m), fh, _CHUNK)
            os.chmod(out, 0o644)
            count += 1
            if on_file:
                on_file(count)
    return count


def count_files(p: Path) -> int:
    """Count files under p (0 if missing)."""
    p = Path(p)
    if not p.is_dir():
        return 0
    return sum(len(files) for _, _, files in os.walk(p))


def copy_tree(src: Path, dest: Path, on_file: Optional[Callable[[int], None]] = None) -> int:
    """Copy a folder into dest (symlinks skipped, empty dirs kept); returns files copied."""
    src, dest = Path(src), Path(dest)
    count = 0
    dest.mkdir(parents=True, exist_ok=True)
    for dirpath, dirnames, files in os.walk(src):
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
        rel = Path(dirpath).relative_to(src)
        (dest / rel).mkdir(parents=True, exist_ok=True)
        for name in files:
            f = Path(dirpath) / name
            if f.is_symlink():
                continue
            shutil.copyfile(f, dest / rel / name)
            count += 1
            if on_file:
                on_file(count)
    return count


def tar_directory(root: Path, db: str, out: Path, gzip: bool,
                  on_bytes: Optional[Callable[[int], None]] = None,
                  cancel: Optional[CancelToken] = None) -> int:
    """Tar <root>/<db> into out with top folder <db> (symlinks skipped); returns the file count."""
    validate_identifier(db, "database name")
    base = Path(root) / db
    if not base.is_dir():
        raise ValueError(f"Filestore folder not found: {base}")
    out = Path(out)
    total = 0
    count = 0
    tf = tarfile.open(out, "w:gz" if gzip else "w")
    try:
        with tf:
            tf.add(base, arcname=db, recursive=False)
            for dirpath, dirnames, files in os.walk(base):
                dirnames[:] = sorted(d for d in dirnames if not (Path(dirpath) / d).is_symlink())
                rel = Path(dirpath).relative_to(base)
                for d in dirnames:
                    tf.add(Path(dirpath) / d, arcname=posixpath.join(db, *rel.parts, d), recursive=False)
                for name in sorted(files):
                    if cancel is not None and cancel.cancelled:
                        raise InterruptedError("Cancelled")
                    path = Path(dirpath) / name
                    if path.is_symlink():
                        continue
                    info = tf.gettarinfo(str(path), arcname=posixpath.join(db, *rel.parts, name))
                    with open(path, "rb") as fh:
                        tf.addfile(info, fh)
                    total += info.size
                    count += 1
                    if on_bytes:
                        on_bytes(total)
    except BaseException:
        _rm(out)
        raise
    return count


def _rm(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass
