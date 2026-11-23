from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from odoo_database_tool import filestore as fs
from odoo_database_tool.runner import CancelToken


def make_store(root: Path, db: str = "mydb") -> Path:
    base = root / db
    (base / "0a").mkdir(parents=True)
    (base / "0a" / "f1").write_bytes(b"hello")
    (base / "ff").mkdir()
    (base / "ff" / "f2").write_bytes(b"world!!")
    (base / "ff" / "f3").write_bytes(b"")
    return base


def make_tar(path: Path, members, mode="w"):
    with tarfile.open(path, mode) as tf:
        for name, data in members:
            if isinstance(data, tarfile.TarInfo):
                tf.addfile(data)
                continue
            ti = tarfile.TarInfo(name)
            if data is None:
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            else:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))


def test_is_filestore_dir(tmp_path):
    base = make_store(tmp_path)
    assert fs.is_filestore_dir(base)
    assert not fs.is_filestore_dir(tmp_path)
    assert not fs.is_filestore_dir(tmp_path / "nope")
    (tmp_path / "zz").mkdir()
    assert not fs.is_filestore_dir(tmp_path / "zz")


def test_resolve_source(tmp_path):
    base = make_store(tmp_path / "p")
    assert fs.resolve_filestore_source(base) == ("dir", base)
    assert fs.resolve_filestore_source(base.parent) == ("dir", base)
    for n in ("a.tar", "a.tar.gz", "a.tgz"):
        f = tmp_path / n
        f.write_bytes(b"x")
        assert fs.resolve_filestore_source(f) == ("tar", f)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError):
        fs.resolve_filestore_source(other)
    make_store(tmp_path / "two", "a")
    make_store(tmp_path / "two", "b")
    with pytest.raises(ValueError):
        fs.resolve_filestore_source(tmp_path / "two")
    txt = tmp_path / "a.txt"
    txt.write_text("x")
    with pytest.raises(ValueError):
        fs.resolve_filestore_source(txt)


def test_target_and_safety(tmp_path):
    root = tmp_path / "filestore"
    root.mkdir()
    t = fs.filestore_target(root, "db1")
    assert t == root / "db1"
    fs.assert_safe_target(t, root, "db1")
    with pytest.raises(ValueError):
        fs.filestore_target(root, "../x")
    with pytest.raises(ValueError):
        fs.assert_safe_target(root / ".." / "x", root, "x")
    with pytest.raises(ValueError):
        fs.assert_safe_target(root / "db2", root, "db1")
    with pytest.raises(ValueError):
        fs.assert_safe_target(root, root, "db1")


@pytest.mark.parametrize("name,mode", [("a.tar", "w"), ("a.tar.gz", "w:gz"), ("a.tgz", "w:gz")])
def test_extract_strips_top(tmp_path, name, mode):
    a = tmp_path / name
    make_tar(a, [("odoo_src", None), ("odoo_src/0a", None), ("odoo_src/0a/f1", b"abc"), ("odoo_src/ff/f2", b"zz")], mode)
    dest = tmp_path / "dest"
    seen = []
    n = fs.extract_tar_strip(a, dest, on_file=seen.append)
    assert n == 2 and seen == [1, 2]
    assert (dest / "0a" / "f1").read_bytes() == b"abc"
    assert (dest / "ff" / "f2").read_bytes() == b"zz"
    assert not (dest / "odoo_src").exists()
    assert (dest / "0a" / "f1").stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("bad", ["top/../../evil", "/abs/evil", "top/a/../../../evil", "top/.."])
def test_extract_rejects_bad_paths(tmp_path, bad):
    work = tmp_path / "work"
    work.mkdir()
    a = work / "bad.tar"
    make_tar(a, [("top/ok", b"1"), (bad, b"x")])
    before = snapshot(tmp_path, work / "dest")
    with pytest.raises(ValueError):
        fs.extract_tar_strip(a, work / "dest")
    assert snapshot(tmp_path, work / "dest") == before


def test_extract_rejects_empty_stripped(tmp_path):
    a = tmp_path / "e.tar"
    make_tar(a, [("top", b"file-named-top")])
    with pytest.raises(ValueError):
        fs.extract_tar_strip(a, tmp_path / "d")


def _special(name, typ, linkname=""):
    ti = tarfile.TarInfo(name)
    ti.type = typ
    ti.linkname = linkname
    return ti


def snapshot(root: Path, exclude: Path):
    return {p for p in root.rglob("*") if exclude not in p.parents and p != exclude}


@pytest.mark.parametrize(
    "typ,link",
    [
        (tarfile.SYMTYPE, "../../outside"),
        (tarfile.SYMTYPE, "f1"),
        (tarfile.LNKTYPE, "../outside"),
        (tarfile.LNKTYPE, "top/f1"),
        (tarfile.CHRTYPE, ""),
        (tarfile.FIFOTYPE, ""),
    ],
)
def test_extract_rejects_links_and_devices(tmp_path, typ, link):
    work = tmp_path / "work"
    work.mkdir()
    a = work / "s.tar"
    make_tar(a, [("top/f1", b"1"), ("x", _special("top/special", typ, link))])
    before = snapshot(tmp_path, work / "dest")
    with pytest.raises(ValueError):
        fs.extract_tar_strip(a, work / "dest")
    assert snapshot(tmp_path, work / "dest") == before


def test_extract_refuses_preexisting_symlink_in_dest(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "link").symlink_to(outside, target_is_directory=True)
    a = tmp_path / "a.tar"
    make_tar(a, [("top/link/new/f", b"x")])
    before = snapshot(tmp_path, dest)
    with pytest.raises(ValueError):
        fs.extract_tar_strip(a, dest)
    assert snapshot(tmp_path, dest) == before
    assert list(outside.iterdir()) == []


def test_copy_skips_symlinks_keeps_empty_dirs(tmp_path):
    src = make_store(tmp_path / "s")
    (src / "empty").mkdir()
    outside = tmp_path / "outdir"
    outside.mkdir()
    (outside / "secret").write_bytes(b"s")
    (src / "linkdir").symlink_to(outside, target_is_directory=True)
    (src / "0a" / "linkfile").symlink_to(outside / "secret")
    dest = tmp_path / "copy"
    assert fs.copy_tree(src, dest) == 3
    assert (dest / "empty").is_dir()
    assert not (dest / "linkdir").exists() and not (dest / "linkdir").is_symlink()
    assert not (dest / "0a" / "linkfile").is_symlink() and not (dest / "0a" / "linkfile").exists()


def test_tar_skips_symlinks(tmp_path):
    root = tmp_path / "root"
    base = make_store(root, "mydb")
    outside = tmp_path / "outdir"
    outside.mkdir()
    (outside / "secret").write_bytes(b"s")
    (base / "linkdir").symlink_to(outside, target_is_directory=True)
    (base / "0a" / "linkfile").symlink_to(outside / "secret")
    out = tmp_path / "o.tar"
    assert fs.tar_directory(root, "mydb", out, False) == 3
    with tarfile.open(out) as tf:
        assert not any("link" in n for n in tf.getnames())


def test_tar_open_failure_keeps_existing_out(tmp_path):
    root = tmp_path / "root"
    make_store(root, "mydb")
    out = tmp_path / "nodir" / "o.tar"
    with pytest.raises(OSError):
        fs.tar_directory(root, "mydb", out, False)
    keep = tmp_path / "keep"
    keep.mkdir()
    with pytest.raises(OSError):
        fs.tar_directory(root, "mydb", keep, False)  # a directory: open fails
    assert keep.is_dir()


def test_copy_and_count(tmp_path):
    base = make_store(tmp_path)
    seen = []
    n = fs.copy_tree(base, tmp_path / "copy", on_file=seen.append)
    assert n == 3 and seen == [1, 2, 3]
    assert fs.count_files(tmp_path / "copy") == 3
    assert fs.count_files(tmp_path / "missing") == 0


@pytest.mark.parametrize("gz", [False, True])
def test_tar_roundtrip(tmp_path, gz):
    root = tmp_path / "root"
    make_store(root, "mydb")
    out = tmp_path / ("o.tar.gz" if gz else "o.tar")
    totals = []
    n = fs.tar_directory(root, "mydb", out, gz, on_bytes=totals.append)
    assert n == 3
    assert totals and totals[-1] == 12
    with tarfile.open(out) as tf:
        assert "mydb/0a/f1" in tf.getnames()
    dest = tmp_path / "dest"
    assert fs.extract_tar_strip(out, dest) == 3
    assert fs.count_files(dest) == 3
    assert (dest / "ff" / "f2").read_bytes() == b"world!!"


def test_tar_cancel_removes_partial(tmp_path):
    root = tmp_path / "root"
    make_store(root, "mydb")
    tok = CancelToken()
    tok.cancel()
    out = tmp_path / "o.tar"
    with pytest.raises(InterruptedError):
        fs.tar_directory(root, "mydb", out, False, cancel=tok)
    assert not out.exists()
