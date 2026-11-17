# AGENTS.md

Instructions for AI coding agents (Claude Code, Codex, Cursor, Hermes, …)
working in this repository or using the tool it ships.

## What this is

`gwa` is a desktop agent for GNOME/KDE **Wayland**: screenshots, screen
recording, and input injection through the XDG desktop portal. The compositor
shows the consent dialog; the shell shows its orange sharing indicator; the
user can revoke at any time. There is no other supported path on Wayland — do
not try `xdotool`, `scrot`, `import`, or `/dev/uinput` here.

**Read `README.md` for the user-facing story.** This file is the contract for
driving it.

## Setup (once per machine)

```bash
./install.sh                 # symlinks ~/.local/bin/gwa, installs a .desktop
gwa doctor                   # must be all [ok] before you rely on it
```

Dependencies: `python3-gobject`, `gstreamer` + `gst-plugin-pipewire`,
`xdg-desktop-portal` with a GNOME/KDE backend. `install.sh` prints the distro
commands when something is missing.

## Two ways to drive it

### 1. MCP server (preferred from an agent host)

```bash
gwa mcp
```

JSON-RPC 2.0 on stdio. Tools: `screen_info`, `screenshot`, `pointer_move`,
`click`, `scroll`, `key`, `type_text`, `monitors`. `screenshot` returns the
image inline (base64 PNG) plus a text part with the path and stream size.

Claude Code:

```bash
claude mcp add gwa -- gwa mcp
```

Claude Desktop / other JSON-config clients:

```json
{"mcpServers": {"gwa": {"command": "gwa", "args": ["mcp"]}}}
```

Starting the server does **not** capture anything; the first tool call is what
raises the consent dialog.

### 2. CLI + JSON socket

```bash
gwa ctl info                            # stream node, size, devices
gwa ctl shot -o /tmp/look.png --max-width 1568
gwa ctl move 640 400 && gwa ctl click
gwa ctl key ctrl+t
gwa ctl type "hello"
gwa ctl quit                            # end the session, drop the indicator
```

`gwa ctl` auto-starts the daemon. The socket is
`$XDG_RUNTIME_DIR/gwa/control.sock`, newline-delimited JSON, 0600:

```jsonc
{"cmd":"info"} {"cmd":"shot","path":"/tmp/a.png","max_width":1568}
{"cmd":"move","x":640,"y":400} {"cmd":"button","button":1,"action":"click"}
{"cmd":"key","combo":"ctrl+t"} {"cmd":"type","text":"hello"}
{"cmd":"scroll","steps":-3} {"cmd":"quit"}
```

Every reply is one object with `"ok": true|false`.

## Coordinate contract — read this twice

- Click coordinates are **stream pixels**, identical to the pixel size of the
  PNG returned by `screenshot` (before any `max_width` downscale).
- If you downscaled, multiply by `stream_size.width / image_width`.
- `gwa monitors` returns *logical* geometry for choosing a region; it is not
  the click space on scaled displays.
- Always `pointer_move` before `click`: the button goes wherever the compositor
  last accepted a motion.

## Safety rules (hard)

1. **Never click the portal consent dialog, a password prompt, a payment
   sheet, or a 2FA challenge.** That dialog is the security boundary this tool
   is built around. Start the action, then ask the user to approve.
2. **Never type secrets** — no passwords, API keys, card numbers or codes.
3. **Verify every state-changing action** with a fresh `screenshot`; `ok: true`
   means "the compositor accepted the event", not "the app did what you meant".
4. **Do not fight for focus.** Input goes to the focused window; if the user is
   typing elsewhere, stop and ask instead of stealing focus.
5. **Close what you open:** `gwa ctl quit` when done, so the orange indicator
   goes away. Delete screenshots you no longer need.

## Debugging

| Symptom | Meaning |
|---|---|
| dialog never appears / call hangs | not in an unlocked graphical session, or wrong `DBUS_SESSION_BUS_ADDRESS` |
| `portal returned no streams` | old portal backend; check `gwa doctor` versions |
| grab times out | `gst-plugin-pipewire` missing, or the compositor paused the stream |
| `degenerate WxH frame` | downscale caps failed to negotiate; grab at native size |
| clicks do nothing, motion works | wrong button code (must be evdev 272/273/274; the CLI maps 1/2/3 for you) |

`GWA_DEBUG=1` prints every portal call and its `Response` status to stderr.

## Repo conventions

- Python 3.10+, stdlib + PyGObject only; no pip dependencies, no build step.
- `gwa/portal.py` is the D-Bus layer, `capture.py` the GStreamer layer,
  `daemon.py` the socket server, `cli.py` the CLI, `mcp.py` the MCP server,
  `client.py` the daemon client. Keep the layering.
- Anything user-visible must work on both GNOME and KDE; do not hard-code
  monitor names, paths or desktop-specific shortcuts.
