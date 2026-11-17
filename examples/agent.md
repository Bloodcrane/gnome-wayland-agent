# Driving `gwa` from an agent

The daemon is the whole point: one consent dialog, then a socket that speaks
JSON. Everything below assumes `gwa daemon` is running (or let `gwa ctl`
auto-start it).

## The loop

```python
import json, os, socket, time

SOCK = os.path.join(os.environ["XDG_RUNTIME_DIR"], "gwa", "control.sock")


def call(**req):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(120)
        conn.connect(SOCK)
        conn.sendall((json.dumps(req) + "\n").encode())
        line = conn.makefile().readline()
    reply = json.loads(line)
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error"))
    return reply


info = call(cmd="info")
stream = info["streams"][0]
w, h = stream["size"]
print(f"controlling {w}x{h} at +{stream['position'][0]}+{stream['position'][1]}")

shot = call(cmd="shot", path="/tmp/look.png", max_width=1568)   # for a vision model
# … decide where to click from the image, in ORIGINAL stream coordinates …
call(cmd="move", x=640, y=400)
call(cmd="click")
time.sleep(0.4)
call(cmd="shot", path="/tmp/look2.png", max_width=1568)         # verify the effect
```

Rules that save you a lot of confusion:

1. **Scale back after `--max-width`.** `shot.max_width=1568` on a 2560-wide
   stream gives a 1568×980 image; a click at image (x, y) must be sent as
   `x * 2560 / 1568`.
2. **Use stream coordinates, not monitor geometry.** `gwa monitors` reports
   logical layout; on a scaled display those numbers are not stream pixels.
   If you only have one screen at position 0,0 they coincide — otherwise use
   `stream.position` and `stream.size`.
3. **Motion first, then click.** `NotifyPointerButton` uses whatever position
   the compositor last accepted, so always `move` before `click`.
4. **Verify with a screenshot, not with the command's exit code.** `ok` means
   the compositor accepted the event, not that the app did something.

## Typing

```python
call(cmd="type", text="https://example.com")   # literal text, per-character keysyms
call(cmd="key", combo="Return")                # named keys and combos
call(cmd="key", combo="ctrl+shift+t")
```

Type into a field you can see and verify, never into a password prompt.

## Recording

```bash
gwa record -o /tmp/demo.webm --duration 10
```

The recording is a separate ScreenCast session (its own consent, its own
indicator) — do not record and control at the same time unless you want two
sessions and two indicators.

## Cleanup

```bash
gwa ctl quit        # end the control session; the orange indicator goes away
```

If you installed the systemd unit (`./install.sh --service`), the daemon
restarts at login and asks for consent once per login; otherwise start it when
the task starts and quit when it ends.
