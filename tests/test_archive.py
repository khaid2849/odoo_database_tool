from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from odoo_database_tool.archive import (
    Manifest,
    append_sha256sums,
    dump_filename,
    filestore_filename,
    lookup_sha256,
    matching_filestore,
    newest_dump,
    parse_toc,
    read_manifest,
    sha256_file,
    write_manifest,
)

NOW = datetime(2026, 11, 23, 10, 15)


def test_parse_toc_sample():
    text = (Path(__file__).parent / "data" / "toc_sample.txt").read_text()
    info = parse_toc(text)
    assert info.dumped_from == "17.12 (Ubuntu 17.12-1.pgdg24.04+1)"
    assert info.dumped_by == "17.4 (Ubuntu 17.4-1.pgdg24.04+2)"
    assert info.entries == 9
    assert info.table_data == 3 and info.public_table_data == 3
    assert info.extensions == ["vector"]


def test_parse_toc_counts_public_table_data_separately():
    info = parse_toc("5; 0 1 TABLE DATA public a odoo\n"
                     "6; 0 2 TABLE DATA audit log odoo\n"
                     "7; 0 3 TABLE DATA publicity b odoo\n")
    assert info.table_data == 3 and info.public_table_data == 1


def test_parse_toc_empty():
    info = parse_toc("")
    assert info.dumped_from is None and info.dumped_by is None
    assert info.entries == 0 and info.table_data == 0 and info.extensions == []
    assert info.public_table_data == 0


def test_filenames():
    assert dump_filename("odoo_dev", "My Server", NOW) == "odoo_dev_my-server_20261123-1015.dump"
    assert filestore_filename("db", "My Server", False, NOW) == "filestore_db_my-server_20261123-1015.tar"
    assert filestore_filename("db", "My Server", True, NOW).endswith(".tar.gz")


def test_sha256_file_multi_chunk(tmp_path):
    f = tmp_path / "a.bin"
    data = b"x" * (2 * 1024 * 1024 + 5)
    f.write_bytes(data)
    import hashlib

    assert sha256_file(f) == hashlib.sha256(data).hexdigest()


def test_filenames_and_sums_with_spaces_in_folder(tmp_path):
    folder = tmp_path / "Odoo DB Dumps"
    folder.mkdir()
    append_sha256sums(folder, "my db_x_1.dump", "aa" * 32)
    append_sha256sums(folder, "other.dump", "bb" * 32)
    assert (folder / "SHA256SUMS").read_text().splitlines()[0] == "aa" * 32 + "  my db_x_1.dump"
    assert lookup_sha256(folder, "my db_x_1.dump") == "aa" * 32
    assert lookup_sha256(folder, "missing.dump") is None


def test_lookup_last_match_wins_and_no_file(tmp_path):
    assert lookup_sha256(tmp_path, "a.dump") is None
    append_sha256sums(tmp_path, "a.dump", "11" * 32)
    append_sha256sums(tmp_path, "a.dump", "22" * 32)
    assert lookup_sha256(tmp_path, "a.dump") == "22" * 32


def _manifest():
    return Manifest(kind="dump", connection="My Server", connection_kind="ssh", database="db",
                    created_at="2026-11-23T10:15:00", size=10, sha256="ab", server_version="17.12",
                    pg_dump_version="17.4", odoo_version="18.0", file_count=3)


def test_manifest_round_trip(tmp_path):
    f = tmp_path / "x.dump.json"
    write_manifest(f, _manifest())
    assert read_manifest(f) == _manifest()
    assert not list(tmp_path.glob("*.tmp"))


def test_manifest_missing_invalid_unknown_keys(tmp_path):
    assert read_manifest(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert read_manifest(bad) is None
    f = tmp_path / "x.json"
    write_manifest(f, _manifest())
    import json

    d = json.loads(f.read_text())
    d["future"] = 1
    f.write_text(json.dumps(d))
    assert read_manifest(f) == _manifest()


def test_newest_dump(tmp_path):
    assert newest_dump(tmp_path) is None
    (tmp_path / "db_c_20261122-0900.dump").write_bytes(b"1")
    (tmp_path / "db_c_20261123-0900.dump").write_bytes(b"1")
    (tmp_path / "db_c_20261124-0900.dump.part").write_bytes(b"1")
    assert newest_dump(tmp_path).name == "db_c_20261123-0900.dump"


def test_matching_filestore_by_name(tmp_path):
    dump = tmp_path / "db_my-server_20261123-1015.dump"
    dump.write_bytes(b"1")
    (tmp_path / "filestore_db_my-server_20261122-1000.tar").write_bytes(b"1")
    (tmp_path / "filestore_db_my-server_20261123-1016.tar.gz").write_bytes(b"1")
    (tmp_path / "filestore_db_my-server_20261124-1016.tar.gz.part").write_bytes(b"1")
    (tmp_path / "filestore_other_my-server_20261125-1000.tar").write_bytes(b"1")
    (tmp_path / "filestore_db_prod_20261125-1000.tar").write_bytes(b"1")
    assert matching_filestore(dump).name == "filestore_db_my-server_20261123-1016.tar.gz"


def test_matching_filestore_uses_manifest(tmp_path):
    dump = tmp_path / "renamed.dump"
    dump.write_bytes(b"1")
    write_manifest(tmp_path / "renamed.dump.json", _manifest())
    (tmp_path / "filestore_db_my-server_20261122-1000.tar").write_bytes(b"1")
    assert matching_filestore(dump).name == "filestore_db_my-server_20261122-1000.tar"


def test_matching_filestore_none(tmp_path):
    dump = tmp_path / "db_c_20261123-1015.dump"
    dump.write_bytes(b"1")
    assert matching_filestore(dump) is None


def test_manifest_non_dict_json_is_none(tmp_path):
    for text in ("[1, 2]", '"text"', "42", "null"):
        f = tmp_path / "m.json"
        f.write_text(text)
        assert read_manifest(f) is None


def test_suffixed_names_still_newest_and_matching(tmp_path):
    (tmp_path / "db_c_20261123-0900.dump").write_bytes(b"1")
    later = tmp_path / "db_c_20261123-0900-2.dump"
    later.write_bytes(b"1")
    os.utime(later, (time.time() + 60, time.time() + 60))
    assert newest_dump(tmp_path) == later
    fs_later = tmp_path / "filestore_db_c_20261123-0900-2.tar"
    fs_later.write_bytes(b"1")
    os.utime(fs_later, (time.time() + 60, time.time() + 60))
    (tmp_path / "filestore_db_c_20261123-0900.tar").write_bytes(b"1")
    assert matching_filestore(later) == fs_later
