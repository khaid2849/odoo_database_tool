#!/usr/bin/env python3
"""Package Odoo Database Tool as a standalone app for the current OS (PyInstaller).

    python -m pip install ".[build]"
    python scripts/build.py

Output in dist/:
    macOS    OdooDatabaseTool.app             + OdooDatabaseTool-macos-<arch>.zip
    Windows  OdooDatabaseTool.exe (1 file)    + OdooDatabaseTool-windows-<arch>.zip
    Linux    OdooDatabaseTool/OdooDatabaseTool     + OdooDatabaseTool-linux-<arch>.tar.gz
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC, DIST, BUILD = ROOT / "src", ROOT / "dist", ROOT / "build"
NAME = "OdooDatabaseTool"
BUNDLE_ID = "io.github.khaid2849.odoodatabasetool"


def run(cmd, **kw):
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def os_tag() -> str:
    return {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")


def arch_tag() -> str:
    m = platform.machine().lower()
    return {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(m, m)


def main() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit('PyInstaller is missing. Run first:  python -m pip install ".[build]"')

    BUILD.mkdir(exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(SRC), "QT_QPA_PLATFORM": "offscreen"}

    # 1. Icon (PyInstaller + Pillow convert the PNG to .icns / .ico)
    icon = BUILD / "icon.png"
    run([sys.executable, "-m", "odoo_database_tool", "--export-icon", icon], env=env)

    # 2. PyInstaller
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
           "--name", NAME, "--paths", SRC,
           "--hidden-import", "keyring.backends",  # macOS / Windows / SecretService backends are loaded by entry point
           "--collect-submodules", "keyring.backends",
           "--distpath", DIST, "--workpath", BUILD / "pyinstaller", "--specpath", BUILD]
    if sys.platform == "darwin":
        cmd += ["--icon", icon, "--osx-bundle-identifier", BUNDLE_ID]
    elif sys.platform == "win32":
        cmd += ["--icon", icon, "--onefile"]  # a single .exe, easy to share
    # Linux: no icon embedded in the binary
    run([*cmd, ROOT / "scripts" / "entry.py"])

    # 3. Archive for release
    base = DIST / f"{NAME}-{os_tag()}-{arch_tag()}"
    if sys.platform == "darwin":
        # ditto keeps the symlinks and signature inside the .app (shutil.make_archive breaks them)
        run(["ditto", "-c", "-k", "--keepParent", DIST / f"{NAME}.app", f"{base}.zip"])
        out = DIST / f"{NAME}.app"
    elif sys.platform == "win32":
        shutil.make_archive(str(base), "zip", DIST, f"{NAME}.exe")
        out = DIST / f"{NAME}.exe"
    else:
        shutil.make_archive(str(base), "gztar", DIST, NAME)
        out = DIST / NAME / NAME
    # ASCII only: the Windows CI console uses cp1252 and can't print emoji
    print(f"\nDone: {out}\n   Release archive: {base.name}.*")


if __name__ == "__main__":
    main()
