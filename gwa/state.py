"""Small persistent state: portal restore tokens + defaults.

The portal's restore token is what turns the consent dialog into a one-time
thing.  It is issued only if the user ticks "Remember this selection", is
bound to this app id and this machine, and can be revoked at any time from
Settings → Apps → (app) → Screen sharing.
"""

from __future__ import annotations

import json
import os
import time

APP = "gwa"


def state_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    path = os.path.join(base, APP)
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def state_path() -> str:
    return os.path.join(state_dir(), "state.json")


def load() -> dict:
    try:
        with open(state_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save(data: dict) -> None:
    path = state_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def set_token(kind: str, token: str | None) -> None:
    data = load()
    if token:
        data[kind] = {"token": token, "created": int(time.time())}
    else:
        data.pop(kind, None)
    save(data)


def get_token(kind: str) -> str | None:
    entry = load().get(kind)
    if isinstance(entry, dict):  # tolerate older flat format
        return entry.get("token")
    if isinstance(entry, str):
        return entry
    return None


def runtime_dir() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/gwa-{os.getuid()}"
    path = os.path.join(base, APP)
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def socket_path() -> str:
    return os.path.join(runtime_dir(), "control.sock")
