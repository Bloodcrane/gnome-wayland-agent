"""Client for the control daemon: talk to it, and start it when needed.

The daemon holds one portal session; every action in this file is a
newline-delimited JSON request over a 0600 unix socket.  Keeping this logic in
one module lets the CLI and the MCP server share it.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time

from . import state


class DaemonError(RuntimeError):
    pass


def socket_path() -> str:
    return state.socket_path()


def call(payload: dict, timeout: float = 120.0) -> dict:
    """Send one request, return one reply."""
    path = socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(path)
            conn.sendall((json.dumps(payload) + "\n").encode())
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise DaemonError(f"no control daemon at {path}") from exc
    if not data.strip():
        raise DaemonError("daemon closed the connection without replying")
    try:
        return json.loads(data.split(b"\n", 1)[0].decode())
    except ValueError as exc:
        raise DaemonError(f"daemon sent invalid JSON: {data[:200]!r}") from exc


def alive() -> bool:
    if not os.path.exists(socket_path()):
        return False
    try:
        return bool(call({"cmd": "ping"}, timeout=5).get("ok"))
    except (OSError, DaemonError):
        return False


def launcher() -> list[str]:
    """Command that starts the daemon, preferring the installed entry point."""
    exe = shutil.which("gwa")
    if exe:
        return [exe]
    return [sys.executable, os.path.realpath(sys.argv[0])]


def ensure(
    wait: float = 180.0,
    window: bool = False,
    cursor: bool = True,
    remember: bool = True,
    autostart: bool = True,
    hint: bool = True,
) -> None:
    """Make sure a daemon is running; on first use this raises the consent dialog."""
    if alive():
        return
    if not autostart:
        raise DaemonError("no control daemon is running (start it with `gwa daemon`)")

    if hint and sys.stderr.isatty():
        print(
            "gwa: waiting for the screen-share dialog — pick what to share and "
            "click Share (tick “Remember this selection” to skip this next time)",
            file=sys.stderr,
            flush=True,
        )

    cmd = launcher() + ["daemon", "--quiet"]
    if window:
        cmd.append("--window")
    if not cursor:
        cmd.append("--no-cursor")
    if not remember:
        cmd.append("--no-remember")
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=None,
        start_new_session=True,
        close_fds=True,
    )

    deadline = time.time() + wait
    while time.time() < deadline:
        if alive():
            return
        time.sleep(0.25)
    raise DaemonError(
        "control daemon did not come up — was the consent dialog dismissed?"
    )
