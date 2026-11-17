"""MCP (Model Context Protocol) server over stdio.

Lets an AI agent drive the desktop without shelling out: the client sends
JSON-RPC 2.0 messages on stdin, this server answers on stdout, and every tool
maps to one request on the control daemon's socket.

    {"command": "gwa", "args": ["mcp"]}

Nothing is captured or clicked until a tool is actually called, so merely
starting the server does not raise a consent dialog.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from typing import Any, Callable

from . import __version__, client, state

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}


def log(msg: str) -> None:
    print(f"gwa[mcp]: {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------
def _text(payload: Any, error: bool = False) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    result = {"content": [{"type": "text", "text": text}]}
    if error:
        result["isError"] = True
    return result


def _daemon(**request) -> dict:
    client.ensure(hint=False)
    reply = client.call(request)
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error", "daemon rejected the request"))
    return reply


def tool_screen_info(_args: dict) -> dict:
    return _text(_daemon(cmd="info"))


def tool_screenshot(args: dict) -> dict:
    path = args.get("path") or os.path.join(state.runtime_dir(), "mcp-shot.png")
    reply = _daemon(
        cmd="shot",
        path=path,
        max_width=args.get("max_width"),
    )
    with open(reply["path"], "rb") as fh:
        encoded = base64.b64encode(fh.read()).decode()
    stream = reply.get("stream", {})
    meta = {
        "path": reply["path"],
        "width": reply["width"],
        "height": reply["height"],
        "stream_size": stream.get("size"),
        "note": (
            "Coordinates for pointer_move/click are in STREAM pixels "
            f"({stream.get('size', [None, None])[0]}x{stream.get('size', [None, None])[1]}); "
            "scale from this image if max_width was used."
        ),
    }
    return {
        "content": [
            {"type": "image", "data": encoded, "mimeType": "image/png"},
            {"type": "text", "text": json.dumps(meta)},
        ]
    }


def tool_pointer_move(args: dict) -> dict:
    return _text(_daemon(cmd="move", x=args["x"], y=args["y"]))


def tool_click(args: dict) -> dict:
    return _text(
        _daemon(
            cmd="button",
            button=args.get("button", 1),
            action=args.get("action", "click"),
        )
    )


def tool_scroll(args: dict) -> dict:
    return _text(
        _daemon(
            cmd="scroll",
            steps=args.get("steps", 0),
            horizontal=bool(args.get("horizontal", False)),
        )
    )


def tool_key(args: dict) -> dict:
    return _text(_daemon(cmd="key", combo=args["combo"]))


def tool_type_text(args: dict) -> dict:
    return _text(_daemon(cmd="type", text=args["text"]))


def tool_monitors(_args: dict) -> dict:
    from .cli import monitor_list

    try:
        return _text({"ok": True, "monitors": monitor_list()})
    except RuntimeError as exc:
        return _text(str(exc), error=True)


TOOLS: list[dict] = [
    {
        "name": "screen_info",
        "description": (
            "Describe the portal session: stream node, stream pixel size, "
            "position, granted devices (keyboard/pointer). Call this first."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": tool_screen_info,
    },
    {
        "name": "screenshot",
        "description": (
            "Capture the shared screen as a PNG and return it as an image. "
            "Coordinates for pointer_move/click are stream pixels: if you pass "
            "max_width, scale image pixels by stream_size.width / image width."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Where to write the PNG."},
                "max_width": {
                    "type": "integer",
                    "description": "Downscale to this width (keeps aspect ratio).",
                },
            },
            "additionalProperties": False,
        },
        "handler": tool_screenshot,
    },
    {
        "name": "pointer_move",
        "description": "Move the pointer to absolute stream coordinates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
            },
            "required": ["x", "y"],
            "additionalProperties": False,
        },
        "handler": tool_pointer_move,
    },
    {
        "name": "click",
        "description": "Click (or press/release) a mouse button at the current pointer position.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "button": {
                    "type": "integer",
                    "enum": [1, 2, 3],
                    "description": "1 left (default), 2 middle, 3 right.",
                },
                "action": {
                    "type": "string",
                    "enum": ["click", "down", "up"],
                    "description": "click (default) presses and releases.",
                },
            },
            "additionalProperties": False,
        },
        "handler": tool_click,
    },
    {
        "name": "scroll",
        "description": "Scroll the wheel: positive steps go down/right, negative up/left.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "steps": {"type": "integer"},
                "horizontal": {"type": "boolean"},
            },
            "required": ["steps"],
            "additionalProperties": False,
        },
        "handler": tool_scroll,
    },
    {
        "name": "key",
        "description": (
            "Tap a key or combination, e.g. 'Return', 'Escape', 'ctrl+t', "
            "'ctrl+shift+Tab'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"combo": {"type": "string"}},
            "required": ["combo"],
            "additionalProperties": False,
        },
        "handler": tool_key,
    },
    {
        "name": "type_text",
        "description": (
            "Type literal text into the focused window. Never use it for "
            "passwords, API keys or payment details."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "handler": tool_type_text,
    },
    {
        "name": "monitors",
        "description": (
            "Logical monitor layout from Mutter (position, size, scale). Useful "
            "for choosing a screenshot region, not for click coordinates."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": tool_monitors,
    },
]

HANDLERS: dict[str, Callable[[dict], dict]] = {
    tool["name"]: tool["handler"] for tool in TOOLS
}


def public_tools() -> list[dict]:
    return [{k: v for k, v in tool.items() if k != "handler"} for tool in TOOLS]


# --------------------------------------------------------------------------
# JSON-RPC plumbing
# --------------------------------------------------------------------------
def _reply(msg_id, result: dict) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n")
    sys.stdout.flush()


def _error(msg_id, code: int, message: str) -> None:
    sys.stdout.write(
        json.dumps(
            {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
        )
        + "\n"
    )
    sys.stdout.flush()


def handle(message: dict) -> None:
    msg_id = message.get("id")
    method = message.get("method")

    # Notifications (no id) never get a response.
    if method == "notifications/initialized" or (
        msg_id is None and method and method.startswith("notifications/")
    ):
        return

    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
        _reply(
            msg_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "gwa", "version": __version__},
                "instructions": (
                    "Wayland desktop control through the XDG desktop portal. "
                    "Call screen_info, then screenshot, then act with "
                    "pointer_move/click/key/type_text. Coordinates are stream "
                    "pixels. The user must approve the share dialog once."
                ),
            },
        )
        return

    if method == "ping":
        _reply(msg_id, {})
        return

    if method == "tools/list":
        _reply(msg_id, {"tools": public_tools()})
        return

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            _error(msg_id, -32602, f"unknown tool: {name!r}")
            return
        try:
            _reply(msg_id, handler(params.get("arguments") or {}))
        except Exception as exc:  # tool failures are results, not protocol errors
            _reply(msg_id, _text(f"{type(exc).__name__}: {exc}", error=True))
        return

    if method in ("resources/list", "resources/templates/list"):
        _reply(msg_id, {"resources": []})
        return
    if method == "prompts/list":
        _reply(msg_id, {"prompts": []})
        return

    if msg_id is not None:
        _error(msg_id, -32601, f"method not found: {method!r}")


def serve() -> int:
    log(f"listening on stdio (protocol {PROTOCOL_VERSION})")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _error(None, -32700, "parse error")
            continue
        if not isinstance(message, dict):
            _error(None, -32600, "invalid request")
            continue
        try:
            handle(message)
        except Exception as exc:  # never let one bad message kill the server
            log(f"error handling {message.get('method')!r}: {exc}")
            if message.get("id") is not None:
                _error(message["id"], -32603, f"internal error: {exc}")
    return 0
