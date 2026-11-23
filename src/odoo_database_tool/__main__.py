"""Entry point: the `odoo-database-tool` command or `python -m odoo_database_tool`."""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .common import reset_frozen_dll_directory
from .store import ASKPASS_ENV, askpass_main


def main(argv: list[str] | None = None) -> None:
    reset_frozen_dll_directory()
    # SSH_ASKPASS mode: ssh passes the prompt as the only argument; no argparse or Qt.
    if ASKPASS_ENV in os.environ:
        sys.exit(askpass_main(os.environ))

    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="odoo-database-tool", description="Dump and restore Odoo databases.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--export-icon", metavar="PNG", help="write the app icon to a PNG file and exit (used when packaging)")
    parser.add_argument("--icon-size", type=int, default=1024, help=argparse.SUPPRESS)
    args, qt_args = parser.parse_known_args(argv)

    if args.export_icon:
        from .common import export_icon
        export_icon(args.export_icon, args.icon_size)
        return

    from .app import run
    sys.exit(run([sys.argv[0], *qt_args]))


if __name__ == "__main__":
    main()
