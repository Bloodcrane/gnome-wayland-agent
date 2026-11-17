"""Long-lived control daemon: one portal consent, then a JSON socket.

The daemon is what makes the desktop usable by an agent. It holds a single
RemoteDesktop portal session (screen picker + input permission), then serves
newline-delimited JSON on a 0600 unix socket in $XDG_RUNTIME_DIR/gwa.

Commands (one JSON object per line, one reply per line):

  {"cmd": "ping"}
  {"cmd": "info"}
  {"cmd": "shot", "path": "/tmp/a.png", "max_width": 1568}
  {"cmd": "move", "x": 640, "y": 400}
  {"cmd": "move_by", "dx": 10, "dy": -5}
  {"cmd": "button", "button": 1, "action": "click"}   # click|down|up
  {"cmd": "scroll", "steps": -3}                       # - is up, + is down
  {"cmd": "key", "combo": "ctrl+t"}                    # press+release
  {"cmd": "key", "keysym": "Return", "action": "tap"}
  {"cmd": "type", "text": "hello"}
  {"cmd": "quit"}
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")

from gi.repository import GLib  # noqa: E402

from . import __version__, capture, keys, state
from .portal import (
    CURSOR_EMBEDDED,
    CURSOR_HIDDEN,
    DEVICE_KEYBOARD,
    DEVICE_POINTER,
    SOURCE_MONITOR,
    SOURCE_WINDOW,
    Portal,
    PortalError,
    RemoteDesktopSession,
)

# The portal's NotifyPointerButton takes linux/input-event-codes.h button
# codes, not 1/2/3: BTN_LEFT=0x110 (272), BTN_RIGHT=0x111 (273),
# BTN_MIDDLE=0x112 (274).  Users (and agents) think in 1/2/3, so map them.
BUTTON_MAP = {1: 272, 2: 274, 3: 273}


def default_shot_path() -> str:
    base = os.path.expanduser("~/Pictures/Screenshots")
    if not os.path.isdir(os.path.expanduser("~/Pictures")):
        base = state.state_dir()
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, time.strftime("gwa-%Y%m%d-%H%M%S.png"))


class ControlDaemon:
    def __init__(
        self,
        sock_path: str | None = None,
        window: bool = False,
        cursor: bool = True,
        remember: bool = True,
        quiet: bool = False,
    ) -> None:
        self.path = sock_path or state.socket_path()
        self.window = window
        self.cursor = CURSOR_EMBEDDED if cursor else CURSOR_HIDDEN
        self.remember = remember
        self.quiet = quiet
        self.portal: Portal | None = None
        self.session: RemoteDesktopSession | None = None
        self.server: socket.socket | None = None
        self.clients: dict[int, socket.socket] = {}
        self.buffers: dict[int, bytes] = {}
        self.loop: GLib.MainLoop | None = None
        self._shutting_down = False

    # -- lifecycle ---------------------------------------------------------
    def log(self, msg: str) -> None:
        if not self.quiet:
            print(f"gwa: {msg}", file=sys.stderr, flush=True)

    def open_session(self) -> None:
        sources = SOURCE_WINDOW if self.window else SOURCE_MONITOR
        token = state.get_token("remote") if self.remember else None
        last: Exception | None = None
        for attempt in (1, 2):
            try:
                self.portal = Portal()
                self.session = RemoteDesktopSession(
                    self.portal,
                    sources=sources,
                    devices=DEVICE_KEYBOARD | DEVICE_POINTER,
                    cursor=self.cursor,
                    restore_token=token,
                    persist=self.remember,
                )
                break
            except PortalError as exc:
                last = exc
                if token and attempt == 1:
                    # A stale/revoked restore token must not wedge the daemon.
                    self.log("stored selection was refused, asking again")
                    token = None
                    state.set_token("remote", None)
                    continue
                raise
        assert self.session
        if self.session.restore_token:
            state.set_token("remote", self.session.restore_token)
        self.log(
            "controlling stream {} ({}x{}) devices={}".format(
                self.session.stream["node_id"],
                self.session.stream["size"][0],
                self.session.stream["size"][1],
                self.session.devices,
            )
        )

    def start(self) -> None:
        self.open_session()
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        if os.path.exists(self.path):
            os.unlink(self.path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.path)
        os.chmod(self.path, 0o600)
        srv.listen(8)
        self.server = srv
        GLib.io_add_watch(
            srv.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN, self._on_accept
        )
        self.loop = GLib.MainLoop()
        signal.signal(signal.SIGTERM, lambda *_: self.shutdown())
        signal.signal(signal.SIGINT, lambda *_: self.shutdown())
        # stdout is the readiness channel for launchers: one JSON line, then done.
        print(
            json.dumps(
                {
                    "ok": True,
                    "ready": True,
                    "socket": self.path,
                    "pid": os.getpid(),
                    "stream": self.session.stream,
                }
            ),
            flush=True,
        )
        self.loop.run()

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self.log("shutting down")
        for conn in list(self.clients.values()):
            try:
                conn.close()
            except OSError:
                pass
        self.clients.clear()
        if self.server:
            try:
                self.server.close()
            except OSError:
                pass
        try:
            if os.path.exists(self.path):
                os.unlink(self.path)
        except OSError:
            pass
        if self.portal and self.session:
            try:
                self.session.close()
            except Exception:
                pass
        if self.loop:
            self.loop.quit()

    # -- socket serving ----------------------------------------------------
    def _on_accept(self, *_args):
        try:
            conn, _ = self.server.accept()
        except OSError:
            return True
        conn.setblocking(True)
        conn.settimeout(30)
        self.clients[conn.fileno()] = conn
        self.buffers[conn.fileno()] = b""
        GLib.io_add_watch(
            conn.fileno(),
            GLib.PRIORITY_DEFAULT,
            GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR,
            self._on_readable,
            conn,
        )
        return True

    def _drop(self, conn: socket.socket) -> None:
        fd = conn.fileno()
        self.clients.pop(fd, None)
        self.buffers.pop(fd, None)
        try:
            conn.close()
        except OSError:
            pass

    def _on_readable(self, _fd, condition, conn: socket.socket):
        if condition & (GLib.IO_HUP | GLib.IO_ERR):
            self._drop(conn)
            return False
        try:
            chunk = conn.recv(65536)
        except OSError:
            self._drop(conn)
            return False
        if not chunk:
            self._drop(conn)
            return False
        fd = conn.fileno()
        buf = self.buffers.get(fd, b"") + chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                request = json.loads(line.decode("utf-8"))
                reply = self.handle(request)
            except Exception as exc:  # never kill the daemon over one bad call
                reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            try:
                conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
            except OSError:
                self._drop(conn)
                return False
        self.buffers[fd] = buf
        return True

    # -- commands ----------------------------------------------------------
    def handle(self, req: dict) -> dict:
        assert self.session
        cmd = (req.get("cmd") or "").lower()

        if cmd in ("ping", "hello"):
            return {"ok": True, "pid": os.getpid(), "version": __version__}

        if cmd == "info":
            return {
                "ok": True,
                "version": __version__,
                "pid": os.getpid(),
                "socket": self.path,
                "streams": self.session.streams,
                "devices": self.session.devices,
                "remember": bool(state.get_token("remote")),
            }

        if cmd == "shot":
            path = req.get("path") or default_shot_path()
            result = capture.grab(
                self.session.stream["node_id"],
                path,
                max_width=req.get("max_width"),
                timeout=float(req.get("timeout", 25)),
                size=tuple(self.session.stream["size"]),
            )
            return {"ok": True, **result, "stream": self.session.stream}

        if cmd == "move":
            self.session.motion_absolute(float(req["x"]), float(req["y"]))
            return {"ok": True}

        if cmd == "move_by":
            self.session.motion_relative(float(req.get("dx", 0)), float(req.get("dy", 0)))
            return {"ok": True}

        if cmd == "button":
            button = BUTTON_MAP.get(int(req.get("button", 1)), int(req.get("button", 1)))
            action = req.get("action", "click")
            if action in ("down", "press"):
                self.session.button(button, True)
            elif action in ("up", "release"):
                self.session.button(button, False)
            else:
                self.session.button(button, True)
                time.sleep(0.02)
                self.session.button(button, False)
            return {"ok": True}

        if cmd == "scroll":
            steps = int(req.get("steps", 0))
            dx = int(req.get("dx", 0))
            if steps:
                self.session.axis_discrete(steps, horizontal=bool(req.get("horizontal")))
            if dx:
                self.session.axis_discrete(dx, horizontal=True)
            if not steps and not dx:
                self.session.axis(float(req.get("ddy", 0)), float(req.get("ddx", 0)))
            return {"ok": True}

        if cmd == "key":
            if "combo" in req:
                syms = keys.parse_combo(req["combo"])
            else:
                syms = [keys.keysym_for(str(req.get("keysym", "Return")))]
            action = req.get("action", "tap")
            if action == "down":
                for sym in syms:
                    self.session.keysym(sym, True)
            elif action == "up":
                for sym in reversed(syms):
                    self.session.keysym(sym, False)
            else:
                for sym in syms:
                    self.session.keysym(sym, True)
                for sym in reversed(syms):
                    self.session.keysym(sym, False)
            return {"ok": True, "keysyms": syms}

        if cmd == "type":
            text = str(req.get("text", ""))
            for ch in text:
                self.session.keysym(keys.char_keysym(ch), True)
                self.session.keysym(keys.char_keysym(ch), False)
                time.sleep(0.008)
            return {"ok": True, "chars": len(text)}

        if cmd == "quit":
            GLib.timeout_add(50, lambda: (self.shutdown(), False)[1])
            return {"ok": True, "bye": True}

        return {"ok": False, "error": f"unknown command: {cmd!r}"}


def run_daemon(**kwargs) -> int:
    daemon = ControlDaemon(**kwargs)
    try:
        daemon.start()
    except PortalError as exc:
        print(f"gwa: {exc}", file=sys.stderr)
        return 2
    return 0
