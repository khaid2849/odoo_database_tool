from __future__ import annotations

import builtins
import sys
import threading
import time

from odoo_database_tool.runner import (
    CancelToken,
    Redactor,
    RunResult,
    run_capture,
    run_stream,
)
from odoo_database_tool.ssh import Cmd


def py(script, **kw):
    return Cmd([sys.executable, "-c", script], **kw)


def test_run_stream_writes_and_counts(tmp_path):
    out = tmp_path / "a.dump"
    seen = []
    res = run_stream(
        py("import sys; sys.stdout.buffer.write(b'x' * (3 * 1024 * 1024))"),
        out,
        on_bytes=seen.append,
    )
    assert res.ok
    assert out.stat().st_size == 3 * 1024 * 1024
    assert res.bytes_out == 3 * 1024 * 1024
    assert seen and seen[-1] == res.bytes_out


def test_run_stream_out_path_with_spaces(tmp_path):
    out = tmp_path / "Odoo DB Dumps" / "a b.dump"
    out.parent.mkdir()
    res = run_stream(py("import sys; sys.stdout.buffer.write(b'hello')"), out)
    assert res.ok
    assert out.read_bytes() == b"hello"


def test_run_stream_failure_keeps_part(tmp_path):
    out = tmp_path / "a.dump.part"
    res = run_stream(
        py("import sys; sys.stdout.buffer.write(b'0123456789'); sys.stdout.flush(); sys.exit(255)"),
        out,
    )
    assert res.rc == 255
    assert not res.ok
    assert out.read_bytes() == b"0123456789"


def test_run_stream_write_error(tmp_path, monkeypatch):
    out = tmp_path / "a.dump"
    real_open = builtins.open

    class BadFile:
        def __init__(self, f):
            self._f = f

        def write(self, data):
            raise OSError(28, "No space left on device")

        def close(self):
            self._f.close()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

    def fake_open(path, mode="r", *a, **k):
        f = real_open(path, mode, *a, **k)
        if str(path) == str(out):
            return BadFile(f)
        return f

    monkeypatch.setattr(builtins, "open", fake_open)
    res = run_stream(
        py("import sys, time\nfor _ in range(50):\n sys.stdout.buffer.write(b'x'*65536); sys.stdout.flush()\ntime.sleep(20)"),
        out,
    )
    assert "No space left" in res.error
    assert not res.ok


def test_run_stream_cancel(tmp_path):
    token = CancelToken()
    threading.Timer(0.3, token.cancel).start()
    start = time.time()
    res = run_stream(py("import time; time.sleep(30)"), tmp_path / "a.dump", cancel=token)
    assert res.cancelled
    assert not res.ok
    assert time.time() - start < 10


def test_run_stream_line_callback_and_tail_cap():
    lines = []
    res = run_stream(
        py("import sys\nfor i in range(5000): print('line', i, file=sys.stderr)"),
        None,
        on_line=lines.append,
    )
    assert len(lines) == 5000
    assert lines[0] == "line 0"
    assert len(res.stderr_tail) == 200
    assert res.stderr_tail[-1] == "line 4999"


def test_stdin_is_fed(tmp_path):
    out = tmp_path / "o"
    res = run_stream(
        py("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())", stdin=b"pw\n"),
        out,
    )
    assert res.ok
    assert out.read_bytes() == b"pw\n"


def test_env_merged_over_environ():
    rc, out, _ = run_capture(py("import os; print(os.environ['ODT_X'], 'PATH' in os.environ)", env={"ODT_X": "1"}))
    assert rc == 0 and out.strip() == "1 True"


def test_missing_executable():
    res = run_stream(Cmd(["/nonexistent/binary-xyz"]), None)
    assert res.rc == 127 and res.error and not res.ok


def test_run_capture_redacts_and_times_out():
    r = Redactor()
    r.add("s3cr3t")
    rc, out, err = run_capture(
        py("import sys; print('a s3cr3t'); print('e s3cr3t', file=sys.stderr)"), redactor=r
    )
    assert rc == 0 and "s3cr3t" not in err
    rc, _, err = run_capture(py("import time; time.sleep(30)"), timeout=0.5)
    assert rc == 124 and "timeout" in err


def test_run_capture_keeps_stdout_data_intact():
    # stdout is data the app parses (database names, versions…); a password that happens
    # to be part of a name ("odoo" in "dev_odoo18") must not corrupt it
    r = Redactor()
    r.add("odoo")
    rc, out, err = run_capture(
        py("import sys; print('dev_odoo18|42'); print('auth odoo failed', file=sys.stderr)"),
        redactor=r,
    )
    assert rc == 0 and out.strip() == "dev_odoo18|42"
    assert "odoo" not in err and "****" in err


def test_redactor_masks():
    r = Redactor()
    r.add("s3cr3t")
    r.add(None)
    r.add("")
    assert r("x s3cr3t y") == "x **** y"


def test_run_result_ok():
    assert RunResult(0, [], 0, False).ok
    assert not RunResult(0, [], 0, True).ok
    assert not RunResult(0, [], 0, False, "boom").ok
