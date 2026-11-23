"""Build and run system `ssh` commands, host-key helpers and tunnels. Qt-free."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .models import SshTarget
from .store import ASKPASS_ENV, askpass_program
from .tools import parse_openssh_version

_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
_TUNNEL_WAIT = 15.0


@dataclass
class Cmd:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    stdin: bytes | None = None


def openssh_version(ssh_bin: str) -> tuple[int, int] | None:
    """Version reported by `ssh -V`, or None if it cannot run."""
    try:
        r = subprocess.run([ssh_bin, "-V"], capture_output=True, text=True,
                           timeout=5, **_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_openssh_version((r.stdout or "") + (r.stderr or ""))


def supports_askpass(version: tuple[int, int] | None) -> bool:
    """SSH_ASKPASS_REQUIRE needs OpenSSH 8.4+."""
    return version is not None and version >= (8, 4)


def ssh_argv(ssh_bin: str, t: SshTarget, *, extra: list[str] = ()) -> list[str]:
    argv = [ssh_bin, "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15"]
    if not t.use_password:
        argv += ["-o", "BatchMode=yes"]
    if not t.alias:
        if t.port:
            argv += ["-p", str(t.port)]
        if t.user:
            argv += ["-l", t.user]
        if t.identity_file:
            argv += ["-i", t.identity_file]
        if t.proxy_jump:
            argv += ["-J", t.proxy_jump]
    return argv + list(extra) + [t.destination()]


def askpass_env(conn_id: str, field: str) -> dict[str, str]:
    return {
        "SSH_ASKPASS": askpass_program(),
        "SSH_ASKPASS_REQUIRE": "force",
        ASKPASS_ENV: f"{conn_id}:{field}",
        "DISPLAY": os.environ.get("DISPLAY", ":0"),
    }


def ssh_cmd(ssh_bin: str, t: SshTarget, remote: str, *, conn_id: str,
            secret_field: str = "ssh_password", stdin: bytes | None = None) -> Cmd:
    env = askpass_env(conn_id, secret_field) if t.use_password else {}
    return Cmd(ssh_argv(ssh_bin, t) + [remote], env, stdin)


def _run(argv: list[str], input_text: str | None = None, timeout: int = 20):
    return subprocess.run(argv, input=input_text, capture_output=True, text=True,
                          timeout=timeout, **_NO_WINDOW)


def _sibling_tool(ssh_bin: str, name: str) -> str:
    """ssh-keygen/ssh-keyscan next to ssh_bin first, then PATH."""
    exe = name + (".exe" if sys.platform == "win32" else "")
    sibling = Path(ssh_bin).parent / exe
    if sibling.is_file():
        return str(sibling)
    return shutil.which(name) or name


def resolve_host_port(ssh_bin: str, t: SshTarget) -> tuple[str, int]:
    """Real host and port; for ~/.ssh/config aliases asks `ssh -G`."""
    if not t.alias:
        return t.host, t.port
    host, port = t.alias, 22
    try:
        r = _run([ssh_bin, "-G", t.alias], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return host, port
    for line in r.stdout.splitlines():
        key, _, val = line.partition(" ")
        if key == "hostname" and val.strip():
            host = val.strip()
        elif key == "port" and val.strip().isdigit():
            port = int(val.strip())
    return host, port


def _known_hosts_name(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def host_key_known(host: str, port: int, ssh_bin: str = "ssh") -> bool:
    try:
        r = _run([_sibling_tool(ssh_bin, "ssh-keygen"), "-F", _known_hosts_name(host, port)],
                 timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def fetch_host_keys(host: str, port: int, ssh_bin: str = "ssh") -> tuple[list[str], list[str]]:
    """Raw known_hosts lines from ssh-keyscan plus their fingerprints."""
    try:
        r = _run([_sibling_tool(ssh_bin, "ssh-keyscan"), "-T", "10", "-p", str(port), host],
                 timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"ssh-keyscan failed: {e}") from None
    lines = [ln for ln in r.stdout.splitlines() if ln.strip() and not ln.startswith("#")]
    if not lines:
        raise RuntimeError(f"Could not fetch host keys from {host}:{port}")
    try:
        f = _run([_sibling_tool(ssh_bin, "ssh-keygen"), "-lf", "-"], "\n".join(lines) + "\n")
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"ssh-keygen failed: {e}") from None
    return lines, [ln.strip() for ln in f.stdout.splitlines() if ln.strip()]


def add_known_host(lines: list[str], known_hosts: Path | None = None) -> None:
    """Append lines to known_hosts without touching existing content."""
    path = known_hosts or Path.home() / ".ssh" / "known_hosts"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing = path.read_bytes() if path.exists() else b""
    prefix = b"\n" if existing and not existing.endswith(b"\n") else b""
    with open(path, "ab") as f:
        f.write(prefix + "".join(ln + "\n" for ln in lines).encode())


def classify_ssh_error(stderr: str, rc: int) -> str | None:
    if rc == 0:
        return None
    s = (stderr or "").lower()
    if "host identification has changed" in s:
        return "host_key_changed"
    if "host key verification failed" in s:
        return "host_key_unknown"
    if "permission denied" in s or "authentication failed" in s:
        return "auth_failed"
    if any(m in s for m in ("timed out", "connection refused", "no route to host",
                            "could not resolve hostname", "network is unreachable",
                            "banner exchange")):
        return "unreachable"
    if any(m in s for m in ("broken pipe", "connection reset", "connection closed",
                            "send disconnect")):
        return "connection_lost"
    return None


_REASONS = {
    "host_key_changed": "the host key has changed (possible man-in-the-middle)",
    "host_key_unknown": "the host key is not trusted yet",
    "auth_failed": "authentication failed",
    "unreachable": "the host is unreachable",
    "connection_lost": "the connection was lost",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Tunnel:
    """`ssh -N -L` to remote_host:remote_port on a free local port, as a context manager."""

    def __init__(self, ssh_bin: str, t: SshTarget, conn_id: str,
                 remote_host: str, remote_port: int):
        self.ssh_bin, self.t, self.conn_id = ssh_bin, t, conn_id
        self.remote_host, self.remote_port = remote_host, remote_port
        self.local_port = 0
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> "Tunnel":
        self.local_port = _free_port()
        argv = ssh_argv(self.ssh_bin, self.t, extra=[
            "-N", "-o", "ExitOnForwardFailure=yes",
            "-L", f"127.0.0.1:{self.local_port}:{self.remote_host}:{self.remote_port}"])
        env = dict(os.environ)
        if self.t.use_password:
            env.update(askpass_env(self.conn_id, "ssh_password"))
        self._proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                      text=True, **_NO_WINDOW)
        try:
            self._wait_ready()
        except BaseException:
            self._stop()
            raise
        return self

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + _TUNNEL_WAIT
        while time.monotonic() < deadline:
            rc = self._proc.poll()
            if rc is not None:
                err = self._proc.stderr.read() if self._proc.stderr else ""
                kind = classify_ssh_error(err, rc)
                reason = _REASONS.get(kind or "", "ssh exited with code %d" % rc)
                raise RuntimeError(f"SSH tunnel failed: {reason} ({kind or 'error'})")
            try:
                with socket.create_connection(("127.0.0.1", self.local_port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("SSH tunnel failed: timed out waiting for the local port")

    def _stop(self) -> None:
        p = self._proc
        if p is None:
            return
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        if p.stderr:
            p.stderr.close()

    def __exit__(self, *exc) -> None:
        self._stop()
