"""Thin, dependency-free (PyGObject only) wrapper over the XDG desktop portal.

Implements the two interfaces that matter for a desktop agent on GNOME/KDE
Wayland:

  * org.freedesktop.portal.ScreenCast  -- PipeWire video of a monitor/window
  * org.freedesktop.portal.RemoteDesktop -- the same video plus input injection

Both are what OBS, Discord, Chrome and every remote-control app use; the
compositor shows its own consent dialog and the shell's "screen is being
shared" indicator, so nothing here can capture or control the screen without
the user saying yes.
"""

from __future__ import annotations

import os
import secrets
import sys
import time

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")

from gi.repository import Gio, GLib  # noqa: E402

PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"
REMOTEDESKTOP = "org.freedesktop.portal.RemoteDesktop"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SESSION_IFACE = "org.freedesktop.portal.Session"

# SelectSources "types"
SOURCE_MONITOR = 1
SOURCE_WINDOW = 2
SOURCE_VIRTUAL = 4

# SelectDevices "types"
DEVICE_KEYBOARD = 1
DEVICE_POINTER = 2
DEVICE_TOUCHSCREEN = 4

# cursor_mode
CURSOR_HIDDEN = 1
CURSOR_EMBEDDED = 2

# persist_mode
PERSIST_TRANSIENT = 1
PERSIST_PERSISTENT = 2

_STATUS = {0: "success", 1: "cancelled by user", 2: "denied or failed"}


class PortalError(RuntimeError):
    pass


def debug(msg: str) -> None:
    if os.environ.get("GWA_DEBUG"):
        print(f"gwa[portal]: {msg}", file=sys.stderr, flush=True)


def _token(prefix: str = "gwa") -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def v(**kwargs) -> dict:
    """Build an a{sv} option dict from Python values."""
    out = {}
    for key, val in kwargs.items():
        if val is None:
            continue
        if isinstance(val, bool):
            out[key] = GLib.Variant("b", val)
        elif isinstance(val, int):
            out[key] = GLib.Variant("u", val)
        elif isinstance(val, float):
            out[key] = GLib.Variant("d", val)
        elif isinstance(val, str):
            out[key] = GLib.Variant("s", val)
        elif isinstance(val, (list, tuple)):
            out[key] = GLib.Variant("as", list(val))
        else:
            raise TypeError(f"cannot encode option {key!r}: {val!r}")
    return out


class Portal:
    """One D-Bus connection to the portal; sessions die with it."""

    def __init__(self) -> None:
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:  # no session bus at all
            raise PortalError(f"no D-Bus session bus: {exc.message}") from exc
        self.sender = self.bus.get_unique_name()
        self._ctx = GLib.MainContext.default()
        self._sessions: list[str] = []

    # -- raw calls ---------------------------------------------------------
    def call(self, iface, method, signature="", args=(), reply=None):
        try:
            return self.bus.call_sync(
                PORTAL_NAME,
                PORTAL_PATH,
                iface,
                method,
                GLib.Variant(signature, args),
                GLib.VariantType(reply) if reply else None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
        except GLib.Error as exc:
            raise PortalError(f"{iface}.{method} failed: {exc.message}") from exc

    def version(self, iface) -> int:
        try:
            res = self.call(
                "org.freedesktop.DBus.Properties",
                "Get",
                "(ss)",
                (iface, "version"),
                "(v)",
            )
            return int(res.unpack()[0])
        except PortalError:
            return 0

    # -- request/response --------------------------------------------------
    def _request_path(self, token: str) -> str:
        """The portal names the request object after the caller's unique name
        and the handle_token we passed, so we can subscribe before calling and
        never lose the Response signal."""
        return f"{PORTAL_PATH}/request/{self.sender[1:].replace('.', '_')}/{token}"

    def call_request(self, iface, method, signature, args, token, timeout=600):
        path = self._request_path(token)
        holder: dict = {}
        done = [False]

        def on_response(_conn, _sender, obj_path, _iface, _signal, params):
            status, results = params.unpack()
            holder["status"] = int(status)
            holder["results"] = results
            done[0] = True
            loop.quit()

        def on_timeout():
            done[0] = True
            loop.quit()
            return False

        loop = GLib.MainLoop()
        sub = self.bus.signal_subscribe(
            PORTAL_NAME, REQUEST_IFACE, "Response", path, None,
            Gio.DBusSignalFlags.NONE, on_response,
        )
        timer = GLib.timeout_add_seconds(timeout, on_timeout)
        try:
            debug(f"{method}: calling, expecting request {path}")
            res = self.call(iface, method, signature, args, "(o)")
            actual = res.unpack()[0]
            debug(f"{method}: portal returned request {actual}")
            if actual and actual != path:  # portal disagreed with our guess
                self.bus.signal_unsubscribe(sub)
                sub = self.bus.signal_subscribe(
                    PORTAL_NAME, REQUEST_IFACE, "Response", actual, None,
                    Gio.DBusSignalFlags.NONE, on_response,
                )
            while not done[0]:
                self._ctx.iteration(True)
            debug(f"{method}: response status {holder.get('status')}")
        finally:
            GLib.source_remove(timer)
            self.bus.signal_unsubscribe(sub)

        if "status" not in holder:
            raise PortalError(
                "timed out waiting for the desktop portal (is a portal "
                "backend running for this desktop?)"
            )
        if holder["status"] != 0:
            raise PortalError(
                f"portal refused: {_STATUS.get(holder['status'], holder['status'])}"
            )
        return holder["results"]

    # -- session lifecycle -------------------------------------------------
    def close_session(self, handle: str) -> None:
        try:
            self.bus.call_sync(
                PORTAL_NAME, handle, SESSION_IFACE, "Close", None, None,
                Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.Error:
            pass
        if handle in self._sessions:
            self._sessions.remove(handle)

    def close_all(self) -> None:
        for handle in list(self._sessions):
            try:
                self.bus.call_sync(
                    PORTAL_NAME, handle, SESSION_IFACE, "Close", None, None,
                    Gio.DBusCallFlags.NONE, -1, None,
                )
            except GLib.Error:
                pass
        self._sessions.clear()


def _parse_streams(results: dict):
    streams = []
    for node_id, props in results.get("streams", []):
        props = dict(props or {})
        w, h = props.get("size", (0, 0))
        x, y = props.get("position", (0, 0))
        streams.append(
            {
                "node_id": int(node_id),
                "size": [int(w), int(h)],
                "position": [int(x), int(y)],
                "source_type": int(props.get("source_type", 0)),
                "id": props.get("id", ""),
                "mapping_id": props.get("mapping_id", ""),
            }
        )
    return streams


class ScreenCastSession:
    """Portal ScreenCast session: pick a screen once, grab frames forever."""

    def __init__(
        self,
        portal: Portal,
        sources: int = SOURCE_MONITOR,
        multiple: bool = False,
        cursor: int = CURSOR_HIDDEN,
        restore_token: str | None = None,
        persist: bool = True,
    ) -> None:
        self.portal = portal
        self.session_token = _token("gwa_s")
        req = _token("gwa_req")
        create = portal.call_request(
            SCREENCAST,
            "CreateSession",
            "(a{sv})",
            (
                v(
                    handle_token=req,
                    session_handle_token=self.session_token,
                ),
            ),
            req,
        )
        # The handle we predicted and the one returned must match; use the
        # response value because it is authoritative.
        self.handle = create["session_handle"]
        self.portal._sessions.append(self.handle)

        sel = v(
            types=sources,
            multiple=multiple,
            cursor_mode=cursor,
            persist_mode=PERSIST_PERSISTENT if persist else PERSIST_TRANSIENT,
            restore_token=restore_token,
        )
        req = _token("gwa_req")
        sel["handle_token"] = GLib.Variant("s", req)
        portal.call_request(
            SCREENCAST, "SelectSources", "(oa{sv})", (self.handle, sel), req,
        )
        req = _token("gwa_req")
        started = portal.call_request(
            SCREENCAST,
            "Start",
            "(osa{sv})",
            (self.handle, "", v(handle_token=req)),
            req,
        )
        self.streams = _parse_streams(started)
        self.restore_token = started.get("restore_token")
        if not self.streams:
            raise PortalError("portal returned no streams")

    def close(self) -> None:
        self.portal.close_session(self.handle)


class RemoteDesktopSession:
    """Portal RemoteDesktop session: ScreenCast (video) + input injection.

    This is the "remote control app" path: the compositor asks the user which
    screen to share and warns that input will be controlled, and the shell's
    orange sharing pill stays visible for the life of the session.
    """

    def __init__(
        self,
        portal: Portal,
        sources: int = SOURCE_MONITOR,
        devices: int = DEVICE_KEYBOARD | DEVICE_POINTER,
        multiple: bool = False,
        cursor: int = CURSOR_EMBEDDED,
        restore_token: str | None = None,
        persist: bool = True,
    ) -> None:
        self.portal = portal
        self.session_token = _token("gwa_s")
        req = _token("gwa_req")
        create = portal.call_request(
            REMOTEDESKTOP,
            "CreateSession",
            "(a{sv})",
            (
                v(
                    handle_token=req,
                    session_handle_token=self.session_token,
                ),
            ),
            req,
        )
        self.handle = create["session_handle"]
        self.portal._sessions.append(self.handle)

        req = _token("gwa_req")
        portal.call_request(
            REMOTEDESKTOP,
            "SelectDevices",
            "(oa{sv})",
            (
                self.handle,
                v(
                    handle_token=req,
                    types=devices,
                    persist_mode=PERSIST_PERSISTENT if persist else PERSIST_TRANSIENT,
                    restore_token=restore_token,
                ),
            ),
            req,
        )
        req = _token("gwa_req")
        # A RemoteDesktop session has no SelectSources of its own: screen
        # sources are attached by calling ScreenCast.SelectSources with this
        # session's handle (see the portal spec), and Start then returns both
        # the selected devices and the PipeWire streams.
        portal.call_request(
            SCREENCAST,
            "SelectSources",
            "(oa{sv})",
            (
                self.handle,
                v(
                    handle_token=req,
                    types=sources,
                    multiple=multiple,
                    cursor_mode=cursor,
                ),
            ),
            req,
        )
        req = _token("gwa_req")
        started = portal.call_request(
            REMOTEDESKTOP,
            "Start",
            "(osa{sv})",
            (self.handle, "", v(handle_token=req)),
            req,
        )
        self.streams = _parse_streams(started)
        self.restore_token = started.get("restore_token")
        self.devices = int(started.get("devices", 0))
        self.clipboard_enabled = bool(started.get("clipboard_enabled", False))
        debug(f"Start results: devices={self.devices} streams={len(self.streams)} "
              f"restore_token={'yes' if self.restore_token else 'no'} "
              f"keys={sorted(started)}")
        if not self.streams:
            raise PortalError("portal returned no streams")
        self.stream = self.streams[0]

    # -- input -------------------------------------------------------------
    # The Notify* methods take their arguments positionally after an (empty)
    # options dict — they are not packed into the vardict.
    def motion_absolute(self, x: float, y: float, stream: int | None = None):
        self.portal.call(
            REMOTEDESKTOP,
            "NotifyPointerMotionAbsolute",
            "(oa{sv}udd)",
            (self.handle, {}, stream or self.stream["node_id"], float(x), float(y)),
        )

    def motion_relative(self, dx: float, dy: float):
        self.portal.call(
            REMOTEDESKTOP, "NotifyPointerMotion", "(oa{sv}dd)",
            (self.handle, {}, float(dx), float(dy)),
        )

    def button(self, button: int, pressed: bool):
        """button: 1 left, 2 middle, 3 right."""
        self.portal.call(
            REMOTEDESKTOP, "NotifyPointerButton", "(oa{sv}iu)",
            (self.handle, {}, int(button), 1 if pressed else 0),
        )

    def axis(self, dx: float = 0.0, dy: float = 0.0):
        self.portal.call(
            REMOTEDESKTOP, "NotifyPointerAxis", "(oa{sv}dd)",
            (self.handle, {}, float(dx), float(dy)),
        )

    def axis_discrete(self, steps: int, horizontal: bool = False):
        self.portal.call(
            REMOTEDESKTOP, "NotifyPointerAxisDiscrete", "(oa{sv}ui)",
            (self.handle, {}, 1 if horizontal else 0, int(steps)),
        )

    def keysym(self, keysym: int, pressed: bool):
        self.portal.call(
            REMOTEDESKTOP, "NotifyKeyboardKeysym", "(oa{sv}iu)",
            (self.handle, {}, int(keysym), 1 if pressed else 0),
        )

    def keycode(self, keycode: int, pressed: bool):
        self.portal.call(
            REMOTEDESKTOP, "NotifyKeyboardKeycode", "(oa{sv}iu)",
            (self.handle, {}, int(keycode), 1 if pressed else 0),
        )

    def close(self) -> None:
        self.portal.close_session(self.handle)
