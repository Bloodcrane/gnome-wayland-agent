# gwa — a GNOME Wayland desktop agent

Screenshot, record, and control a Wayland desktop through the **XDG desktop
portal** — the same mechanism OBS, Chrome and every remote-control app uses.

The compositor owns consent: it shows the "Share Screen" dialog, you pick a
monitor or window, and GNOME keeps its orange **screen-sharing indicator** in
the top bar for as long as the session lives. Nothing here can capture or
click anything without that permission, and the permission can be revoked at
any time.

Verified on GNOME Shell 51.1 / Wayland, xdg-desktop-portal 1.22. It talks only
to standard portal interfaces, so it also runs on KDE Plasma 6 (and any
compositor that implements ScreenCast/RemoteDesktop).

```
gwa shot -o /tmp/screen.png      # one frame, one consent, then the session closes
gwa record -o demo.webm         # video of a screen or window
gwa daemon                      # hold one consent, expose a JSON control socket
gwa ctl move 640 400            # pointer, keyboard, scroll, screenshots
gwa ctl click
gwa ctl type "hello"
gwa mcp                         # MCP server on stdio, for AI agents
```

## Why a portal and not X11 tricks

Wayland gives applications no framebuffer and no input-injection API. The
supported route is `org.freedesktop.portal.ScreenCast` (PipeWire video) and
`org.freedesktop.portal.RemoteDesktop` (the same video plus synthetic input).
Going through it means:

- the user always chooses what is shared, per session;
- the shell advertises the session — the orange pill in the GNOME panel;
- the app never gets blanket control of the machine;
- the same code works on GNOME, KDE and (for video) wlroots.

## Requirements

| Component | Why |
|---|---|
| `xdg-desktop-portal` + `xdg-desktop-portal-gnome` (or `-kde`) | the interfaces |
| PyGObject (`python3-gobject`) | D-Bus client |
| GStreamer + `gst-plugin-pipewire` | decoding the PipeWire stream |
| PipeWire | transport |

Install (the script also checks all of this):

```bash
git clone https://github.com/Bloodcrane/gnome-wayland-agent.git
cd gnome-wayland-agent
./install.sh                 # symlink into ~/.local/bin, install .desktop
./install.sh --service       # optional: systemd user unit at login
```

No root, no pip, no build step. `./install.sh --uninstall` removes it.

Distro packages if something is missing:

```bash
# Arch
sudo pacman -S python-gobject gstreamer gst-plugins-base gst-plugin-pipewire \
               xdg-desktop-portal xdg-desktop-portal-gnome
# Debian/Ubuntu
sudo apt install python3-gi gstreamer1.0-tools gstreamer1.0-plugins-base \
                 gstreamer1.0-pipewire xdg-desktop-portal-gnome
# Fedora
sudo dnf install python3-gobject gstreamer1-plugins-base \
                 gstreamer1-plugin-pipewire xdg-desktop-portal-gnome
```

## First run

```bash
gwa doctor                    # dependencies, portal versions, stored consents
gwa shot -o ~/shot.png        # dialog appears: pick a display, click Share
```

Tick **"Remember this selection"** in the dialog and the token is stored
(`~/.local/state/gwa/state.json`, mode 0600); later runs skip the dialog.
Control sessions keep a separate token, so a screenshot permission never
implies input permission.

## Commands

```
gwa doctor                        health check + what is remembered
gwa shot [-o FILE] [--window] [--cursor] [--max-width N] [--json]
gwa record [-o FILE] [--duration S] [--fps N] [--window] [--cursor]
gwa daemon [--window] [--no-cursor] [--no-remember] [--quiet]
gwa ctl info | ping | quit
gwa ctl shot [-o FILE] [--max-width N]
gwa ctl move X Y                  absolute, in stream pixels
gwa ctl move-by DX DY             relative
gwa ctl click | down | up [--button N]     1 left, 2 middle, 3 right
gwa ctl scroll STEPS [--horizontal]        negative = up/left
gwa ctl key "ctrl+t" [--action tap|down|up]
gwa ctl type "text"
gwa monitors [--json]             logical monitor geometry (GNOME/Mutter)
gwa mcp                           MCP server on stdio (agents; see AGENTS.md)
gwa token shot|control [show|clear]
```

`--json` on any command prints machine-readable output.

`gwa ctl` auto-starts the daemon if it is not running, which is when the
consent dialog appears. The daemon holds one RemoteDesktop session, so a
whole agent run costs one dialog, not one per action.

## Using it from an agent

Two interfaces, same daemon underneath. **[AGENTS.md](AGENTS.md)** is the
contract: tools, coordinate rules and safety constraints.

### MCP (Claude Code, Claude Desktop, Cursor, …)

```bash
gwa mcp                          # JSON-RPC 2.0 over stdio
claude mcp add gwa -- gwa mcp    # Claude Code
```

```json
{"mcpServers": {"gwa": {"command": "gwa", "args": ["mcp"]}}}
```

Tools: `screen_info`, `screenshot` (image returned inline), `pointer_move`,
`click`, `scroll`, `key`, `type_text`, `monitors`. Starting the server is free;
the first tool call raises the consent dialog.

### JSON socket

The daemon listens on `$XDG_RUNTIME_DIR/gwa/control.sock` (0700 directory,
0600 socket, per-user) and speaks newline-delimited JSON:

```jsonc
{"cmd": "info"}                                  // streams, sizes, devices, pid
{"cmd": "shot", "path": "/tmp/a.png", "max_width": 1568}
{"cmd": "move", "x": 640, "y": 400}
{"cmd": "button", "button": 1, "action": "click"}
{"cmd": "key", "combo": "ctrl+t"}
{"cmd": "type", "text": "hello"}
{"cmd": "scroll", "steps": -3}
{"cmd": "quit"}
```

Every reply is one JSON object with `"ok": true|false`.

**Coordinates are in stream pixels.** The stream size comes from the portal
(`info` → `streams[0].size`) and matches the PNG you get from `shot`; if you
resize with `--max-width`, scale back before clicking. On a HiDPI screen the
stream is the logical size, not the panel resolution — never click using
`gwa monitors` geometry unless the scale is 1.

```python
import json, socket, os
def call(**req):
    path = os.path.join(os.environ["XDG_RUNTIME_DIR"], "gwa/control.sock")
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(path)
        s.sendall((json.dumps(req) + "\n").encode())
        return json.loads(s.makefile().readline())

print(call(cmd="info"))
```

See `examples/agent.md` for a full loop (look → decide → click → verify).

## How it works

1. `ScreenCast.CreateSession` → `SelectSources(types=monitor|window,
   cursor_mode, persist_mode=2, restore_token)` → `Start`. The dialog is shown
   by `Start`; the response carries `streams = [(node_id, {size, position,
   …})]` and, if remembered, a `restore_token`.
2. Frames: `gst-launch-1.0 pipewiresrc path=<node_id> num-buffers=1 !
   videoconvert ! pngenc ! filesink`, one short-lived pipeline per grab. For
   video: `… ! vp8enc ! webmmux`.
3. Control: `RemoteDesktop.CreateSession` → `SelectDevices(keyboard|pointer)`
   → `ScreenCast.SelectSources` **on the same session handle** → `Start`. That
   yields input permission plus the PipeWire stream in one consent.
   Input is `NotifyPointerMotionAbsolute{stream, x, y}`, `NotifyPointerButton`
   (evdev codes: 272/273/274), `NotifyPointerAxisDiscrete`,
   `NotifyKeyboardKeysym`.
4. The session lives as long as the D-Bus connection that created it, which is
   why the daemon exists: a one-shot command must hold the bus through the
   grab, and a long agent run needs a socket, not a dialog per action.

## Security model

- Consent is per app, per session, granted by the compositor; the picker is
  the boundary. `gwa` never clicks it for you.
- Screen capture and input control use separate sessions and separate tokens.
- Tokens live in `~/.local/state/gwa/state.json` (0600) and can be cleared with
  `gwa token shot clear` / `gwa token control clear`; revoking in GNOME
  Settings has the same effect, and `gwa` falls back to asking again.
- The daemon socket is 0600 inside a 0700 directory: same-user only. Any
  process running as you can use it, exactly like `xdotool` on X11.
- The orange indicator stays on while a control session lives — that is the
  point. `gwa ctl quit` closes it.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Dialog never appears, request hangs | Not in an unlocked graphical session, or talking to the wrong bus. Check `loginctl show-session … -p Type -p Active` and `ps -eo comm \| grep -E 'gnome-shell\|kwin_wayland'`. Over SSH set `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$UID/bus`. |
| "Unknown application" in the dialog | The portal could not match the executable to a `.desktop` file. `./install.sh` installs `gwa.desktop`; run `gwa` from `PATH`. |
| `portal returned no streams` | Old `xdg-desktop-portal-gnome`, or the session was started without screen sources. Update the portal; check `gwa doctor` versions. |
| Grab times out | `gst-plugin-pipewire` missing, or the compositor paused the stream. `gst-inspect-1.0 pipewiresrc` must succeed inside the session. |
| `degenerate WxH frame` | The downscale caps failed to negotiate. Update GStreamer, or grab at native size (no `--max-width`). |
| Stale token after revoking permission | Handled: the token is dropped and one interactive attempt is made. |
| Indicator never goes away | A session was not closed. `gwa ctl quit`; a crashed client releases its session when the bus connection dies. |

## Notes on other desktops

- **KDE Plasma 6**: same portals, same commands; the picker looks different.
- **wlroots (sway/Hyprland)**: `xdg-desktop-portal-wlr` implements ScreenCast
  only — `shot`/`record` work, `ctl` does not (no RemoteDesktop).
- **X11**: works through the portal too, but on X11 you do not need it.

## Licence

MIT.
