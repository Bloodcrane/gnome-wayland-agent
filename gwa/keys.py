"""X11 keysyms — the currency of the RemoteDesktop portal's keyboard API.

Mutter (and KWin) translate a keysym to a keycode using the user's active
layout, so we never hard-code a keyboard layout here.
"""

from __future__ import annotations

# Modifier names -> keysyms
MODIFIERS = {
    "ctrl": 0xFFE3,
    "control": 0xFFE3,
    "shift": 0xFFE1,
    "alt": 0xFFE9,
    "super": 0xFFEB,
    "meta": 0xFFEB,
    "win": 0xFFEB,
    "altgr": 0xFFEA,
}

# Named keys -> keysyms
NAMED = {
    "return": 0xFF0D, "enter": 0xFF0D, "tab": 0xFF09,
    "esc": 0xFF1B, "escape": 0xFF1B, "space": 0x0020,
    "backspace": 0xFF08, "delete": 0xFFFF, "del": 0xFFFF,
    "insert": 0xFF63, "home": 0xFF50, "end": 0xFF57,
    "pageup": 0xFF55, "pgup": 0xFF55, "pagedown": 0xFF56, "pgdn": 0xFF56,
    "up": 0xFF52, "down": 0xFF54, "left": 0xFF51, "right": 0xFF53,
    "menu": 0xFF67, "print": 0xFF61, "pause": 0xFF13,
    "capslock": 0xFFE5, "numlock": 0xFF7F,
    **{f"f{i}": 0xFFBE + i - 1 for i in range(1, 13)},
}

# Characters whose keysym differs from their Unicode code point in a way that
# matters on a US layout: none for ASCII, so ordinary code points work.
_FORBIDDEN = {
    "\n": "return", "\t": "tab", "\r": "return",
    "\b": "backspace", "\x1b": "esc",
}


def keysym_for(name: str) -> int:
    """Resolve 'ctrl', 'Return', 'a', 'F5', '€' … to a keysym."""
    key = name.strip()
    low = key.lower()
    if low in MODIFIERS:
        return MODIFIERS[low]
    if low in NAMED:
        return NAMED[low]
    if len(key) == 1:
        return ord(key)
    if key.startswith("U+") or key.startswith("0x"):
        return int(key.replace("U+", ""), 16)
    raise ValueError(f"unknown key name: {name!r}")


def char_keysym(ch: str) -> int:
    """Keysym for typing one character."""
    special = _FORBIDDEN.get(ch)
    if special:
        return NAMED[special]
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp
    return 0x01000000 + cp  # Unicode keysym range


def parse_combo(combo: str) -> list[int]:
    """'ctrl+shift+t' -> [Control_L, Shift_L, t]."""
    parts = [p for p in combo.replace(" ", "").split("+") if p]
    if not parts:
        raise ValueError("empty key combination")
    return [keysym_for(p) for p in parts]
