"""Archive helpers: pg_restore TOC parsing, checksums, manifests and file naming."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path

from .common import slugify, timestamp

SUMS_FILE = "SHA256SUMS"
_TS = r"(\d{8}-\d{4})"
_SEQ = r"(?:-\d+)?"  # "-2", "-3"… when a name was already taken in the same minute
_DUMP_RE = re.compile(r"^(?P<db>.+)_(?P<slug>[a-z0-9-]+)_" + _TS + _SEQ + r"\.dump$")


@dataclass
class TocInfo:
    dumped_from: str | None
    dumped_by: str | None
    entries: int
    table_data: int
    extensions: list[str]


def parse_toc(text: str) -> TocInfo:
    """Summarise the output of `pg_restore -l`."""
    dumped_from = dumped_by = None
    entries = table_data = 0
    extensions: list[str] = []
    for line in text.splitlines():
        if line.startswith(";"):
            m = re.match(r";\s+Dumped from database version:\s*(.+?)\s*$", line)
            if m:
                dumped_from = m.group(1)
            m = re.match(r";\s+Dumped by pg_dump version:\s*(.+?)\s*$", line)
            if m:
                dumped_by = m.group(1)
        elif re.match(r"^\d+;", line):
            entries += 1
            if re.match(r"^\d+; \d+ \d+ TABLE DATA ", line):
                table_data += 1
            m = re.match(r"^\d+; \d+ \d+ EXTENSION - (\S+)", line)
            if m:
                extensions.append(m.group(1))
    return TocInfo(dumped_from, dumped_by, entries, table_data, extensions)


def dump_filename(db: str, conn_name: str, now: datetime | None = None) -> str:
    return "%s_%s_%s.dump" % (db, slugify(conn_name), timestamp(now))


def filestore_filename(db: str, conn_name: str, gzip: bool, now: datetime | None = None) -> str:
    return "filestore_%s_%s_%s.tar%s" % (db, slugify(conn_name), timestamp(now), ".gz" if gzip else "")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def append_sha256sums(folder: Path, name: str, digest: str) -> None:
    with open(Path(folder) / SUMS_FILE, "a", encoding="utf-8", newline="\n") as f:
        f.write("%s  %s\n" % (digest, name))


def lookup_sha256(folder: Path, name: str) -> str | None:
    """Hex digest of the last SHA256SUMS line for `name`, or None."""
    try:
        lines = (Path(folder) / SUMS_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    found = None
    for line in lines:
        digest, sep, fname = line.partition("  ")
        if sep and fname == name:
            found = digest
    return found


@dataclass
class Manifest:
    kind: str
    connection: str
    connection_kind: str
    database: str
    created_at: str
    size: int
    sha256: str
    server_version: str = ""
    pg_dump_version: str = ""
    odoo_version: str = ""
    file_count: int = 0


def write_manifest(file: Path, m: Manifest) -> None:
    file = Path(file)
    tmp = file.with_name(file.name + ".tmp")
    tmp.write_text(json.dumps(asdict(m), indent=2), encoding="utf-8")
    os.replace(tmp, file)


def read_manifest(file: Path) -> Manifest | None:
    try:
        data = json.loads(Path(file).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        names = {f.name for f in fields(Manifest)}
        return Manifest(**{k: v for k, v in data.items() if k in names})
    except (OSError, ValueError, TypeError):
        return None


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def newest_dump(folder: Path) -> Path | None:
    """Newest finished *.dump in `folder` (name timestamp, then mtime)."""
    best, best_key = None, None
    for p in Path(folder).glob("*.dump"):
        m = _DUMP_RE.match(p.name)
        key = (m.group(3) if m else "", _mtime(p))
        if best_key is None or key > best_key:
            best, best_key = p, key
    return best


def matching_filestore(dump: Path) -> Path | None:
    """Newest filestore archive beside `dump` for the same database and connection."""
    dump = Path(dump)
    man = read_manifest(dump.with_name(dump.name + ".json"))
    if man is not None:
        db, slug = man.database, slugify(man.connection)
    else:
        m = _DUMP_RE.match(dump.name)
        if not m:
            return None
        db, slug = m.group("db"), m.group("slug")
    pat = re.compile("^filestore_%s_%s_%s%s\\.tar(\\.gz)?$" % (re.escape(db), re.escape(slug), _TS, _SEQ))
    best, best_key = None, None
    for p in dump.parent.iterdir():
        m = pat.match(p.name)
        if m:
            key = (m.group(1), _mtime(p))
            if best_key is None or key > best_key:
                best, best_key = p, key
    return best
