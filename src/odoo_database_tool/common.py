"""Shared constants and helpers. Qt is imported lazily so the core stays Qt-free."""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path

APP_NAME = "Odoo Database Tool"
ORG_NAME = "khaid2849"
BUILD_NAME = "OdooDatabaseTool"


def odoo_data_dir(platform: str | None = None) -> Path:
    """Odoo's per-user data dir (holds filestore/<db>) for the given or current OS."""
    platform = platform or sys.platform
    home = Path.home()
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Odoo"
    if platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(local) / "OpenERP S.A" / "Odoo"
    return home / ".local" / "share" / "Odoo"


def slugify(text: str) -> str:
    """Lowercase ASCII slug for file names; 'connection' when nothing is left."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "connection"


def timestamp(now: datetime | None = None) -> str:
    """File-name timestamp, YYYYMMDD-HHMM."""
    return (now or datetime.now()).strftime("%Y%m%d-%H%M")


def make_icon(size: int = 256):
    """Database-cylinder icon drawn in code (window icon, icon files when packaging)."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QPainter, QPen, QPixmap

    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = float(size)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#2563eb"))
    p.drawRoundedRect(QRectF(s * .06, s * .06, s * .88, s * .88), s * .22, s * .22)

    left, width, cap, top, body = s * .26, s * .48, s * .16, s * .26, s * .40
    pen = QPen(QColor("white"), s * .045, Qt.SolidLine, Qt.RoundCap)
    # Side walls, two lower arcs for the bands, a filled top ellipse as the lid
    p.setPen(pen)
    p.drawLine(QPointF(left, top + cap / 2), QPointF(left, top + body + cap / 2))
    p.drawLine(QPointF(left + width, top + cap / 2), QPointF(left + width, top + body + cap / 2))
    for i in (1, 2):
        p.drawArc(QRectF(left, top + body * i / 2, width, cap), 180 * 16, 180 * 16)
    p.setBrush(QColor("white"))
    p.drawEllipse(QRectF(left, top, width, cap))
    p.end()
    return pm


def export_icon(path, size: int = 1024) -> None:
    """Write the app icon as a PNG (used when packaging)."""
    from PySide6.QtGui import QGuiApplication

    _app = QGuiApplication.instance() or QGuiApplication([])
    if not make_icon(size).save(str(path)):
        raise SystemExit(f"Could not write the icon: {path}")
