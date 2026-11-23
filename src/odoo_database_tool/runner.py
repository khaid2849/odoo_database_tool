"""Run external commands: stream stdout to a file, stderr to a log callback, with cancel."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .ssh import Cmd

CHUNK = 1024 * 1024
PROGRESS_STEP = 256 * 1024
KILL_GRACE = 5.0


class Redactor:
    """Replaces registered secrets with `****`."""

    def __init__(self) -> None:
        self._secrets: list[str] = []

    def add(self, secret: str | None) -> None:
        if secret:
            self._secrets.append(secret)
            # Longest first so a secret containing another is fully masked.
            self._secrets.sort(key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "****")
        return text


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)


@dataclass
class RunResult:
    rc: int
    stderr_tail: list[str]
    bytes_out: int
    cancelled: bool
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.cancelled and not self.error


def _popen(cmd: Cmd) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(cmd.env)
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(
        cmd.argv,
        stdin=subprocess.PIPE if cmd.stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        **kwargs,
    )


def _feed_stdin(proc: subprocess.Popen, data: bytes) -> None:
    try:
        proc.stdin.write(data)
        proc.stdin.flush()
    except OSError:
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


def _stop(proc: subprocess.Popen) -> None:
    """Terminate, then kill if the process ignores it."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(KILL_GRACE)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_capture(
    cmd: Cmd, *, timeout: float = 60, redactor: Redactor | None = None
) -> tuple[int, str, str]:
    """Run to completion and return (rc, stdout, stderr); rc 124 on timeout."""
    red = redactor or (lambda t: t)
    try:
        proc = _popen(cmd)
    except OSError as exc:
        return 127, "", red(str(exc))
    try:
        out, err = proc.communicate(cmd.stdin, timeout=timeout)
        rc, timed_out = proc.returncode, False
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        rc, timed_out = 124, True
    out_s = out.decode("utf-8", errors="replace")
    err_s = err.decode("utf-8", errors="replace")
    if timed_out:
        err_s = (err_s + "\ntimeout").lstrip("\n")
    return rc, red(out_s), red(err_s)


def run_stream(
    cmd: Cmd,
    out_path: Path | None,
    *,
    on_bytes: Callable[[int], None] | None = None,
    on_line: Callable[[str], None] | None = None,
    cancel: CancelToken | None = None,
    redactor: Redactor | None = None,
    tail: int = 200,
) -> RunResult:
    """Stream stdout to `out_path` (discarded if None) and stderr lines to `on_line`."""
    red = redactor or (lambda t: t)
    try:
        proc = _popen(cmd)
    except OSError as exc:
        return RunResult(127, [], 0, False, error=red(str(exc)))

    tail_lines: deque[str] = deque(maxlen=tail)

    def read_stderr() -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = red(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
            tail_lines.append(line)
            if on_line:
                on_line(line)

    err_thread = threading.Thread(target=read_stderr, daemon=True)
    err_thread.start()
    if cmd.stdin is not None:
        threading.Thread(target=_feed_stdin, args=(proc, cmd.stdin), daemon=True).start()

    done = threading.Event()

    def watch_cancel() -> None:
        while not done.is_set():
            if cancel.wait(0.1):
                _stop(proc)
                return

    if cancel is not None:
        threading.Thread(target=watch_cancel, daemon=True).start()

    error = ""
    total = 0
    reported = 0
    fh = None
    try:
        if out_path is not None:
            fh = open(out_path, "wb")
        while True:
            chunk = proc.stdout.read(CHUNK)
            if not chunk:
                break
            if fh is not None:
                try:
                    fh.write(chunk)
                except OSError as exc:
                    error = exc.strerror or str(exc)
                    _stop(proc)
                    break
            total += len(chunk)
            if on_bytes and total - reported >= PROGRESS_STEP:
                on_bytes(total)
                reported = total
    except OSError as exc:
        error = error or exc.strerror or str(exc)
        _stop(proc)
    finally:
        if fh is not None:
            try:
                fh.close()
            except OSError as exc:
                error = error or exc.strerror or str(exc)
        done.set()

    rc = proc.wait()
    err_thread.join(5)
    proc.stdout.close()
    proc.stderr.close()
    if on_bytes and total != reported:
        on_bytes(total)
    return RunResult(
        rc,
        list(tail_lines),
        total,
        bool(cancel and cancel.cancelled),
        red(error),
    )
