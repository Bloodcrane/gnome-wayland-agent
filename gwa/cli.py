"""Command line interface."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

import gi

gi.require_version("Gio", "2.0")

from gi.repository import Gio  # noqa: E402

from . import __version__, capture, client, state
from .portal import (
    CURSOR_EMBEDDED,
    CURSOR_HIDDEN,
    REMOTEDESKTOP,
    SCREENCAST,
    SOURCE_MONITOR,
    SOURCE_WINDOW,
    Portal,
    PortalError,
    ScreenCastSession,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _emit(payload: dict, as_json: bool, human: str | None = None) -> None:
    if as_json or human is None:
        print(json.dumps(payload, sort_keys=False))
    else:
        print(human)


def _default_shot_path() -> str:
    base = os.path.expanduser("~/Pictures/Screenshots")
    if not os.path.isdir(os.path.expanduser("~/Pictures")):
        base = state.state_dir()
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, time.strftime("gwa-%Y%m%d-%H%M%S.png"))


def _wait_hint() -> None:
    if sys.stderr.isatty():
        print(
            "gwa: waiting for the screen-share dialog — pick what to share and "
            "click Share (tick “Remember this selection” to skip this next time)",
            file=sys.stderr,
            flush=True,
        )


def _ensure_daemon(args) -> None:
    client.ensure(
        wait=float(getattr(args, "wait", 180) or 180),
        window=getattr(args, "window", False),
        cursor=not getattr(args, "no_cursor", False),
        remember=not getattr(args, "no_remember", False),
        autostart=not getattr(args, "no_autostart", False),
        hint=True,
    )


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_shot(args) -> int:
    sources = SOURCE_WINDOW if args.window else SOURCE_MONITOR
    cursor = CURSOR_EMBEDDED if args.cursor else CURSOR_HIDDEN
    portal = Portal()
    token = None if args.no_remember else state.get_token("screencast")
    _wait_hint()
    try:
        try:
            session = ScreenCastSession(
                portal, sources=sources, cursor=cursor, restore_token=token
            )
        except PortalError:
            if not token:
                raise
            state.set_token("screencast", None)
            session = ScreenCastSession(portal, sources=sources, cursor=cursor)
    except PortalError as exc:
        print(f"gwa: {exc}", file=sys.stderr)
        return 2
    try:
        if session.restore_token:
            state.set_token("screencast", session.restore_token)
        out = args.out or _default_shot_path()
        result = capture.grab(
            session.streams[0]["node_id"],
            out,
            max_width=args.max_width,
            timeout=args.timeout,
            size=tuple(session.streams[0]["size"]),
        )
        payload = {
            "ok": True,
            "path": result["path"],
            "bytes": result["bytes"],
            "width": result["width"],
            "height": result["height"],
            "stream": session.streams[0],
        }
        _emit(payload, args.json, result["path"])
        return 0
    except capture.CaptureError as exc:
        print(f"gwa: {exc}", file=sys.stderr)
        return 3
    finally:
        session.close()


def cmd_record(args) -> int:
    sources = SOURCE_WINDOW if args.window else SOURCE_MONITOR
    cursor = CURSOR_EMBEDDED if args.cursor else CURSOR_HIDDEN
    portal = Portal()
    token = None if args.no_remember else state.get_token("screencast")
    _wait_hint()
    try:
        session = ScreenCastSession(
            portal, sources=sources, cursor=cursor, restore_token=token
        )
    except PortalError as exc:
        print(f"gwa: {exc}", file=sys.stderr)
        return 2
    try:
        if session.restore_token:
            state.set_token("screencast", session.restore_token)
        out = args.out or time.strftime("gwa-%Y%m%d-%H%M%S.webm")
        print(f"gwa: recording to {os.path.abspath(out)}", file=sys.stderr)
        result = capture.record(
            session.streams[0]["node_id"], out, duration=args.duration, fps=args.fps
        )
        _emit({"ok": True, **result}, args.json, result["path"])
        return 0
    finally:
        session.close()


def cmd_daemon(args) -> int:
    from .daemon import run_daemon

    return run_daemon(
        sock_path=args.socket,
        window=args.window,
        cursor=not args.no_cursor,
        remember=not args.no_remember,
        quiet=args.quiet,
    )


def _human(payload: dict, verb: str) -> str:
    if not payload.get("ok"):
        return f"error: {payload.get('error', 'unknown')}"
    if verb == "shot":
        return payload.get("path", "ok")
    if verb == "info":
        streams = payload.get("streams", [])
        lines = [
            f"daemon pid {payload.get('pid')} · socket {payload.get('socket')}",
            f"remembered selection: {'yes' if payload.get('remember') else 'no'}",
        ]
        for s in streams:
            lines.append(
                f"stream {s['node_id']}: {s['size'][0]}x{s['size'][1]} "
                f"at +{s['position'][0]}+{s['position'][1]}"
            )
        return "\n".join(lines)
    return "ok" if not payload.get("path") else str(payload["path"])


def cmd_ctl(args) -> int:
    _ensure_daemon(args)
    verb = args.verb
    if verb == "shot":
        payload = {
            "cmd": "shot",
            "path": args.out,
            "max_width": args.max_width,
        }
    elif verb == "move":
        payload = {"cmd": "move", "x": args.x, "y": args.y}
    elif verb == "move-by":
        payload = {"cmd": "move_by", "dx": args.dx, "dy": args.dy}
    elif verb in ("click", "down", "up"):
        payload = {
            "cmd": "button",
            "button": args.button,
            "action": {"click": "click", "down": "down", "up": "up"}[verb],
        }
    elif verb == "scroll":
        payload = {"cmd": "scroll", "steps": args.steps, "horizontal": args.horizontal}
    elif verb == "key":
        payload = {"cmd": "key", "combo": args.combo, "action": args.action}
    elif verb == "type":
        payload = {"cmd": "type", "text": args.text}
    elif verb == "info":
        payload = {"cmd": "info"}
    elif verb == "ping":
        payload = {"cmd": "ping"}
    elif verb == "quit":
        payload = {"cmd": "quit"}
    else:  # pragma: no cover - argparse restricts this
        raise SystemExit(f"unknown ctl verb {verb}")

    try:
        reply = client.call(payload, timeout=args.timeout)
    except (OSError, client.DaemonError) as exc:
        print(f"gwa: {exc}", file=sys.stderr)
        return 2
    _emit(reply, args.json, _human(reply, verb))
    return 0 if reply.get("ok") else 1


def monitor_list() -> list[dict]:
    """Logical monitor geometry straight from Mutter (GNOME only).

    Mutter's GetCurrentState returns logical monitors as
    (x, y, scale, transform, primary, [connectors], props) and puts the pixel
    size on each monitor's current mode, so the logical size has to be derived.
    """
    portal = Portal()
    try:
        res = portal.bus.call_sync(
            "org.gnome.Mutter.DisplayConfig",
            "/org/gnome/Mutter/DisplayConfig",
            "org.gnome.Mutter.DisplayConfig",
            "GetCurrentState",
            None,
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
    except Exception as exc:
        raise RuntimeError(f"monitor discovery needs GNOME: {exc}") from exc
    _serial, monitors, logical, _props = res.unpack()

    current_mode: dict[str, tuple[int, int]] = {}
    for monitor in monitors:
        connector = monitor[0][0]
        for mode in monitor[1]:
            if mode[6].get("is-current"):
                current_mode[connector] = (int(mode[1]), int(mode[2]))
                break

    out = []
    for lm in logical:
        x, y, scale, transform, primary, connectors, props = lm
        width = props.get("width")
        height = props.get("height")
        if not width or not height:
            px = current_mode.get(connectors[0][0], (0, 0)) if connectors else (0, 0)
            width = round(px[0] / scale)
            height = round(px[1] / scale)
        if int(transform) in (1, 3, 5, 7):
            width, height = height, width
        out.append(
            {
                "x": int(x), "y": int(y),
                "width": int(width), "height": int(height),
                "scale": float(scale),
                "primary": bool(primary),
                "transform": int(transform),
                "connector": connectors[0][0] if connectors else "",
            }
        )
    return out


def cmd_monitors(args) -> int:
    try:
        out = monitor_list()
    except RuntimeError as exc:
        print(f"gwa: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"ok": True, "monitors": out}, indent=2))
    else:
        for m in out:
            print(
                f"{'primary ' if m['primary'] else '        '}"
                f"{m['width']}x{m['height']} at +{m['x']}+{m['y']} "
                f"(scale {m['scale']:g}, {m['connector']})"
            )
    return 0


def _session_info() -> tuple[str, str, str]:
    """Desktop, session type and live compositor.

    A shell inherited from an earlier login can report a stale
    XDG_CURRENT_DESKTOP, so prefer the user manager's environment and confirm
    with the process that is actually compositing.
    """
    env: dict[str, str] = {}
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            if "=" in line:
                key, val = line.split("=", 1)
                env[key] = val
    except (OSError, subprocess.SubprocessError):
        pass
    desktop = env.get("XDG_CURRENT_DESKTOP") or os.environ.get("XDG_CURRENT_DESKTOP", "?")
    stype = env.get("XDG_SESSION_TYPE") or os.environ.get("XDG_SESSION_TYPE", "?")
    compositor = ""
    for name in ("gnome-shell", "kwin_wayland", "sway", "Hyprland", "weston"):
        if shutil.which("pgrep") and subprocess.run(
            ["pgrep", "-x", name], capture_output=True
        ).returncode == 0:
            compositor = name
            break
    return desktop, stype, compositor


def cmd_doctor(args) -> int:
    ok = True
    checks: list[tuple[str, bool, str]] = []

    desktop, session, compositor = _session_info()
    where = f"{desktop} / {session}" + (f" / {compositor}" if compositor else "")
    checks.append(("wayland session", session == "wayland", where))
    portal = None
    try:
        portal = Portal()
        sc = portal.version(SCREENCAST)
        rd = portal.version(REMOTEDESKTOP)
        checks.append(("ScreenCast portal", sc >= 2, f"version {sc}"))
        checks.append(("RemoteDesktop portal", rd >= 2, f"version {rd}"))
        if sc < 2 or rd < 2:
            ok = False
    except PortalError as exc:
        checks.append(("desktop portal", False, str(exc)))
        ok = False

    checks.append(("gst-launch-1.0", capture.have_gst(),
                   shutil.which("gst-launch-1.0") or "missing"))
    try:
        probe = subprocess.run(
            ["gst-inspect-1.0", "pipewiresrc"], capture_output=True, text=True, timeout=10
        )
        checks.append(("gst pipewiresrc", probe.returncode == 0, "available"
                       if probe.returncode == 0 else "missing gst-plugin-pipewire"))
        if probe.returncode != 0:
            ok = False
    except (OSError, subprocess.SubprocessError):
        checks.append(("gst pipewiresrc", False, "gst-inspect-1.0 unavailable"))
        ok = False

    try:
        probes = os.listdir("/usr/share/xdg-desktop-portal/portals")
        backend = ", ".join(
            p[:-7] for p in probes if p.endswith(".portal") and "gtk" not in p
        )
    except OSError:
        backend = "unknown"
    checks.append(("portal backends", True, backend))

    token_s = state.get_token("screencast")
    token_r = state.get_token("remote")
    checks.append(("remembered shot selection", True, "yes" if token_s else "no"))
    checks.append(("remembered control selection", True, "yes" if token_r else "no"))
    checks.append(("control daemon", True, "running" if client.alive() else "stopped"))

    width = max(len(name) for name, _, _ in checks)
    for name, good, detail in checks:
        mark = "ok  " if good else "FAIL"
        print(f"[{mark}] {name:<{width}}  {detail}")
    return 0 if ok else 1


def cmd_mcp(args) -> int:
    from .mcp import serve

    return serve()


def cmd_token(args) -> int:
    kind = "screencast" if args.kind == "shot" else "remote"
    if args.action == "clear":
        state.set_token(kind, None)
        print(f"gwa: cleared the remembered {args.kind} selection")
    else:
        token = state.get_token(kind)
        print(token or "")
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gwa",
        description="GNOME/KDE Wayland desktop agent over the XDG desktop portal "
                    "(screen capture + input injection, always with user consent).",
    )
    p.add_argument("--version", action="version", version=f"gwa {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    shot = sub.add_parser("shot", help="grab one frame of a screen or window")
    shot.add_argument("-o", "--out", help="output PNG (default: ~/Pictures/Screenshots)")
    shot.add_argument("--window", action="store_true", help="pick a window instead of a monitor")
    shot.add_argument("--cursor", action="store_true", help="include the pointer in the frame")
    shot.add_argument("--max-width", type=int, help="downscale to this width (useful for vision models)")
    shot.add_argument("--timeout", type=float, default=25.0, help="grab timeout in seconds")
    shot.add_argument("--no-remember", action="store_true", help="ignore the stored selection")
    shot.add_argument("--json", action="store_true")
    shot.set_defaults(func=cmd_shot)

    rec = sub.add_parser("record", help="record a screen or window to a video file")
    rec.add_argument("-o", "--out", help="output file (.webm, or .mp4 for H.264)")
    rec.add_argument("--duration", type=float, default=None, help="stop after N seconds (default: Ctrl-C)")
    rec.add_argument("--fps", type=int, default=30)
    rec.add_argument("--window", action="store_true")
    rec.add_argument("--cursor", action="store_true")
    rec.add_argument("--no-remember", action="store_true")
    rec.add_argument("--json", action="store_true")
    rec.set_defaults(func=cmd_record)

    daemon = sub.add_parser("daemon", help="run the control daemon (one consent, JSON socket)")
    daemon.add_argument("--socket", help="override socket path")
    daemon.add_argument("--window", action="store_true", help="share a window instead of a monitor")
    daemon.add_argument("--no-cursor", action="store_true", help="hide the pointer")
    daemon.add_argument("--no-remember", action="store_true")
    daemon.add_argument("--quiet", action="store_true")
    daemon.set_defaults(func=cmd_daemon)

    ctl = sub.add_parser("ctl", help="send a command to the control daemon")
    ctl.add_argument("--json", action="store_true")
    ctl.add_argument("--timeout", type=float, default=120.0)
    ctl.add_argument("--no-autostart", action="store_true")
    ctl.add_argument("--wait", type=float, default=180.0, help="seconds to wait for consent")
    ctl.add_argument("--window", action="store_true", help="(autostart) share a window")
    ctl.add_argument("--no-cursor", action="store_true", help="(autostart) hide the pointer")
    ctl.add_argument("--no-remember", action="store_true", help="(autostart) don't store consent")
    cs = ctl.add_subparsers(dest="verb", required=True)

    cs.add_parser("info", help="session, streams and geometry")
    cs.add_parser("ping")
    sshot = cs.add_parser("shot", help="screenshot through the control session")
    sshot.add_argument("-o", "--out")
    sshot.add_argument("--max-width", type=int)

    mv = cs.add_parser("move", help="move the pointer to absolute stream coordinates")
    mv.add_argument("x", type=float)
    mv.add_argument("y", type=float)

    mb = cs.add_parser("move-by", help="move the pointer relative to its position")
    mb.add_argument("dx", type=float)
    mb.add_argument("dy", type=float)

    for name, text in (("click", "press and release"), ("down", "hold"), ("up", "release")):
        c = cs.add_parser(name, help=text)
        c.add_argument("--button", type=int, default=1, help="1 left (default), 2 middle, 3 right")

    sc = cs.add_parser("scroll", help="wheel steps (negative is up)")
    sc.add_argument("steps", type=int)
    sc.add_argument("--horizontal", action="store_true")

    key = cs.add_parser("key", help="tap or hold a key / combination, e.g. ctrl+t")
    key.add_argument("combo")
    key.add_argument("--action", choices=("tap", "down", "up"), default="tap")

    typ = cs.add_parser("type", help="type text into the focused window")
    typ.add_argument("text")

    cs.add_parser("quit", help="stop the daemon and end the portal session")
    for subparser in cs.choices.values():
        # SUPPRESS keeps a `gwa ctl --json …` value from being reset by the
        # subcommand's own default.
        subparser.add_argument(
            "--json", action="store_true", default=argparse.SUPPRESS
        )
    ctl.set_defaults(func=cmd_ctl)

    mon = sub.add_parser("monitors", help="logical monitor geometry (GNOME)")
    mon.add_argument("--json", action="store_true")
    mon.set_defaults(func=cmd_monitors)

    mcp = sub.add_parser("mcp", help="run an MCP server on stdio (for AI agents)")
    mcp.set_defaults(func=cmd_mcp)

    doc = sub.add_parser("doctor", help="check dependencies, portal versions, consents")
    doc.set_defaults(func=cmd_doctor)

    tok = sub.add_parser("token", help="show or clear remembered portal selections")
    tok.add_argument("kind", choices=("shot", "control"))
    tok.add_argument("action", choices=("show", "clear"), default="show", nargs="?")
    tok.set_defaults(func=cmd_token)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except PortalError as exc:
        print(f"gwa: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
