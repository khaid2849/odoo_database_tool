"""Shared plumbing for long-running jobs: checks, context and errors. Qt-free."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .runner import CancelToken, Redactor


@dataclass
class Check:
    """One preflight result; a warning has ok=True and warning=True (it never blocks)."""

    name: str
    ok: bool
    detail: str = ""
    hint: str = ""
    warning: bool = False


@dataclass
class JobContext:
    """Callbacks a job reports through; progress takes a percent (or None) and step text."""

    log: Callable[[str], None]
    progress: Callable[[Optional[int], str], None]
    cancel: CancelToken = field(default_factory=CancelToken)
    redactor: Redactor = field(default_factory=Redactor)


class JobError(Exception):
    """A failure whose message is safe to show in the UI (no secrets)."""


def human_bytes(n: float) -> str:
    """'1.5 GB' style size."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
