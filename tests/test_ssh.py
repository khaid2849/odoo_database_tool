from __future__ import annotations

import socket
import stat
import sys
import textwrap

import pytest

from odoo_database_tool import ssh
from odoo_database_tool.models import SshTarget
from odoo_database_tool.ssh import (
    Tunnel, add_known_host, classify_ssh_error, reason_text, ssh_argv, ssh_cmd, supports_askpass,
)


@pytest.fixture(autouse=True)
def _askpass(monkeypatch):
    monkeypatch.setattr(ssh, "askpass_program", lambda: "/fake/askpass")


def test_ssh_argv_alias_batchmode():
    a = ssh_argv("ssh", SshTarget(alias="my-server"))
    assert a[0] == "ssh" and "BatchMode=yes" in a and a[-1] == "my-server" and "-p" not in a
    assert "StrictHostKeyChecking=yes" in a


def test_ssh_argv_explicit_fields():
    a = ssh_argv("ssh", SshTarget(host="10.0.0.5", port=234, user="admin99",
                                  identity_file="/k", proxy_jump="j@h:234"), extra=["-N"])
    assert a[-1] == "10.0.0.5" and a[a.index("-p") + 1] == "234"
    assert a[a.index("-l") + 1] == "admin99" and a[a.index("-i") + 1] == "/k"
    assert a[a.index("-J") + 1] == "j@h:234" and a[-2] == "-N"


def test_password_mode_uses_askpass_no_batchmode():
    c = ssh_cmd("ssh", SshTarget(host="h", use_password=True), "hostname", conn_id="c1")
    assert "BatchMode=yes" not in c.argv and c.env["SSH_ASKPASS_REQUIRE"] == "force"
    assert c.env["ODT_ASKPASS_KEY"] == "c1:ssh_password" and c.env["SSH_ASKPASS"] == "/fake/askpass"
    assert c.argv[-2:] == ["h", "hostname"]


def test_key_mode_has_no_askpass_env():
    c = ssh_cmd("ssh", SshTarget(host="h"), "hostname", conn_id="c1", stdin=b"x")
    assert c.env == {} and c.stdin == b"x"


@pytest.mark.parametrize("v,ok", [((8, 4), True), ((9, 0), True), ((8, 3), False), (None, False)])
def test_supports_askpass(v, ok):
    assert supports_askpass(v) is ok


@pytest.mark.parametrize("stderr,rc,kind", [
    ("@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\nHost key verification failed.",
     255, "host_key_changed"),
    ("Host key verification failed.", 255, "host_key_unknown"),
    ("Permission denied (publickey,password).", 255, "auth_failed"),
    ("ssh: connect to host 1.2.3.4 port 22: Operation timed out", 255, "unreachable"),
    ("Connection timed out during banner exchange", 255, "unreachable"),
    ("ssh: Could not resolve hostname x", 255, "unreachable"),
    ("client_loop: send disconnect: Broken pipe", 255, "connection_lost"),
    ("something else", 1, None),
    ("Got permission denied while trying to connect to the Docker daemon socket at "
     "unix:///var/run/docker.sock", 1, None),
    ("psql: error: connection to server at \"h\" failed: Connection refused", 2, None),
    ("", 0, None)])
def test_classify_ssh_error(stderr, rc, kind):
    assert classify_ssh_error(stderr, rc) == kind


def test_reason_text():
    assert reason_text("connection_lost") == "the connection was lost"
    assert reason_text("unknown") == "the SSH command failed"


def test_add_known_host_appends(tmp_path):
    kh = tmp_path / ".ssh" / "known_hosts"
    add_known_host(["h ssh-ed25519 AAA"], kh)
    assert kh.read_text() == "h ssh-ed25519 AAA\n"
    kh.write_text("old line")  # no trailing newline
    add_known_host(["h2 ssh-rsa BBB", "h2 ssh-ed25519 CCC"], kh)
    assert kh.read_text() == "old line\nh2 ssh-rsa BBB\nh2 ssh-ed25519 CCC\n"
    if sys.platform != "win32":
        assert stat.S_IMODE(kh.parent.stat().st_mode) == 0o700


def _fake_ssh(tmp_path, body):
    p = tmp_path / "fakessh.py"
    p.write_text("#!%s\n" % sys.executable + textwrap.dedent(body))
    p.chmod(0o755)
    return str(p)


LISTENER = """
    import socket, sys, time
    a = sys.argv
    lp = int(a[a.index("-L") + 1].split(":")[1])
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", lp)); s.listen(5)
    while True:
        time.sleep(0.1)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="shebang fake ssh")
def test_tunnel_opens_and_closes(tmp_path):
    fake = _fake_ssh(tmp_path, LISTENER)
    with Tunnel(fake, SshTarget(host="h"), "c1", "db.internal", 5432) as t:
        with socket.create_connection(("127.0.0.1", t.local_port), timeout=2):
            pass
        proc = t._proc
        assert proc.poll() is None
    assert proc.poll() is not None


@pytest.mark.skipif(sys.platform == "win32", reason="shebang fake ssh")
def test_tunnel_failure_reports_auth(tmp_path):
    fake = _fake_ssh(tmp_path, """
        import sys
        sys.stderr.write("Permission denied (publickey).\\n")
        sys.exit(255)
    """)
    with pytest.raises(RuntimeError, match="auth"):
        with Tunnel(fake, SshTarget(host="h"), "c1", "db", 5432):
            pass


@pytest.mark.skipif(sys.platform == "win32", reason="shebang fake ssh")
def test_tunnel_password_uses_tunnel_secret_field(tmp_path):
    out = tmp_path / "env.txt"
    fake = _fake_ssh(tmp_path, "    import os\n    open(%r, 'w').write(os.environ['ODT_ASKPASS_KEY'])\n" % str(out)
                     + LISTENER)
    with Tunnel(fake, SshTarget(host="h", use_password=True), "c9", "db", 5432):
        pass
    assert out.read_text() == "c9:tunnel_password"


@pytest.mark.parametrize("host,spec", [
    ("db.internal", "127.0.0.1:5000:db.internal:5432"),
    ("10.0.0.5", "127.0.0.1:5000:10.0.0.5:5432"),
    ("::1", "127.0.0.1:5000:[::1]:5432"),
    ("fd00::5", "127.0.0.1:5000:[fd00::5]:5432"),
    ("[fd00::5]", "127.0.0.1:5000:[fd00::5]:5432"),
])
def test_forward_spec_brackets_ipv6(host, spec):
    assert ssh.forward_spec(5000, host, 5432) == spec
