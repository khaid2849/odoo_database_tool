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


def _inside(rel: str) -> bool:
    return rel != ".." and not rel.startswith("../") and not posixpath.isabs(rel)


def _check_link(member: tarfile.TarInfo, rel: str) -> str:
    """Return the link target relative to dest, rejecting anything outside it."""
    link = member.linkname
    if link.startswith("/") or "\\" in link:
        raise ValueError(f"Link escapes destination: {member.name!r} -> {link!r}")
    if member.issym():
        target = posixpath.normpath(posixpath.join(posixpath.dirname(rel), link))
    else:
        target = _strip_name(link)
        if not target:
            raise ValueError(f"Link escapes destination: {member.name!r} -> {link!r}")
        target = posixpath.normpath(target)
    if not _inside(target):
        raise ValueError(f"Link escapes destination: {member.name!r} -> {link!r}")
    return target


def _ensure_inside(dest_real: Path, path: Path) -> None:
    if os.path.commonpath([dest_real, Path(os.path.realpath(path))]) != str(dest_real):
        raise ValueError(f"Refusing to write outside destination: {path}")


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
            if not (m.isreg() or m.isdir() or m.issym() or m.islnk()):
                raise ValueError(f"Unsupported member type in archive: {m.name!r}")
            out = dest / rel
            if m.isdir():
                _ensure_inside(dest_real, out)
                out.mkdir(parents=True, exist_ok=True)
                os.chmod(out, 0o755)
                continue
            link_target = _check_link(m, rel) if (m.issym() or m.islnk()) else None
            out.parent.mkdir(parents=True, exist_ok=True)
            _ensure_inside(dest_real, out.parent)
            if m.issym():
                if out.is_symlink() or out.exists():
                    out.unlink()
                os.symlink(m.linkname, out)
            elif m.islnk():
                src = dest / link_target
                if not src.is_file():
                    raise ValueError(f"Hard link target not extracted: {m.name!r}")
                if out.exists() or out.is_symlink():
                    out.unlink()
                shutil.copyfile(src, out)
                os.chmod(out, 0o644)
            else:
                if out.is_symlink():
                    out.unlink()
                src_f = tf.extractfile(m)
                with open(out, "wb") as fh:
                    shutil.copyfileobj(src_f, fh, _CHUNK)
                os.chmod(out, 0o644)
            count += 1
            if on_file:
                on_file(count)
    return count


def count_files(p: Path) -> int:
    p = Path(p)
    if not p.is_dir():
        return 0
    return sum(len(files) for _, _, files in os.walk(p))


def copy_tree(src: Path, dest: Path, on_file: Optional[Callable[[int], None]] = None) -> int:
    """Copy a folder's files into dest (created if needed); returns files copied."""
    src, dest = Path(src), Path(dest)
    count = 0
    dest.mkdir(parents=True, exist_ok=True)
    for dirpath, dirnames, files in os.walk(src):
        rel = Path(dirpath).relative_to(src)
        (dest / rel).mkdir(parents=True, exist_ok=True)
        for name in files:
            shutil.copyfile(Path(dirpath) / name, dest / rel / name)
            count += 1
            if on_file:
                on_file(count)
    return count


def tar_directory(root: Path, db: str, out: Path, gzip: bool,
                  on_bytes: Optional[Callable[[int], None]] = None,
                  cancel: Optional[CancelToken] = None) -> int:
    """Tar <root>/<db> into out with top folder <db>; returns the file count."""
    validate_identifier(db, "database name")
    base = Path(root) / db
    if not base.is_dir():
        raise ValueError(f"Filestore folder not found: {base}")
    out = Path(out)
    total = 0
    count = 0
    try:
        with tarfile.open(out, "w:gz" if gzip else "w") as tf:
            tf.add(base, arcname=db, recursive=False)
            for dirpath, dirnames, files in os.walk(base):
                dirnames.sort()
                rel = Path(dirpath).relative_to(base)
                for d in dirnames:
                    tf.add(Path(dirpath) / d, arcname=posixpath.join(db, *rel.parts, d), recursive=False)
                for name in sorted(files):
                    if cancel is not None and cancel.cancelled:
                        raise InterruptedError("Cancelled")
                    path = Path(dirpath) / name
                    info = tf.gettarinfo(str(path), arcname=posixpath.join(db, *rel.parts, name))
                    if not info.isreg():
                        tf.addfile(info)
                    else:
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
