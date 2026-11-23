"""User settings stored in QSettings (env ODT_SETTINGS points at an ini file for tests)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtCore import QByteArray, QSettings

from .common import ORG_NAME
from .tools import ToolInfo, detect_tool


class AppSettings:
    def __init__(self, qsettings: QSettings | None = None):
        if qsettings is None:
            ini = os.environ.get("ODT_SETTINGS")
            qsettings = (QSettings(ini, QSettings.IniFormat) if ini
                         else QSettings(ORG_NAME, "OdooDatabaseTool"))
        self._q = qsettings
        self._tool_cache: dict[tuple[str, str], ToolInfo | None] = {}

    def _str(self, key: str, default: str = "") -> str:
        return str(self._q.value(key, default) or default)

    def tool_override(self, name: str) -> str:
        return self._str(f"tools/{name}")

    def set_tool_override(self, name: str, path: str) -> None:
        self._q.setValue(f"tools/{name}", path)
        for key in [k for k in self._tool_cache if k[0] == name]:
            del self._tool_cache[key]

    @property
    def ssh_override(self) -> str:
        return self._str("tools/ssh")

    @ssh_override.setter
    def ssh_override(self, path: str) -> None:
        self._q.setValue("tools/ssh", path)

    @property
    def addons_sets(self) -> dict[str, list[str]]:
        try:
            data = json.loads(self._str("neutralize/addons_sets", "{}"))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @addons_sets.setter
    def addons_sets(self, value: dict[str, list[str]]) -> None:
        self._q.setValue("neutralize/addons_sets", json.dumps(value))

    @property
    def dump_dir(self) -> Path:
        return Path(self._str("dump_dir", str(Path.home() / "Odoo DB Dumps")))

    @dump_dir.setter
    def dump_dir(self, value) -> None:
        self._q.setValue("dump_dir", str(value))

    @property
    def jobs(self) -> int:
        try:
            return max(1, int(self._q.value("jobs", 4)))
        except (TypeError, ValueError):
            return 4

    @jobs.setter
    def jobs(self, value: int) -> None:
        self._q.setValue("jobs", int(value))

    @property
    def theme(self) -> str:
        return self._str("theme", "system")

    @theme.setter
    def theme(self, value: str) -> None:
        self._q.setValue("theme", value)

    def clear_tool_cache(self) -> None:
        """Forget detected tools so the next lookup probes the disk again."""
        self._tool_cache.clear()

    @property
    def window_geometry(self) -> QByteArray:
        value = self._q.value("window/geometry")
        return value if isinstance(value, QByteArray) else QByteArray()

    @window_geometry.setter
    def window_geometry(self, value: QByteArray) -> None:
        self._q.setValue("window/geometry", value)

    def tool(self, name: str) -> ToolInfo | None:
        key = (name, self.tool_override(name))
        if key not in self._tool_cache:
            self._tool_cache[key] = detect_tool(name, key[1])
        return self._tool_cache[key]
