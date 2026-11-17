#!/usr/bin/env bash
# Installer for gwa — copy or symlink into ~/.local, no root, no pip.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"
BIN="$PREFIX/bin"
APPS="$PREFIX/share/applications"
UNITS="$PREFIX/share/systemd/user"
SERVICE=0

usage() {
  cat <<'EOF'
usage: ./install.sh [--service] [--uninstall]

  --service     also install + enable gwa-daemon.service (asks for screen
                access at every login — only do this on a dedicated machine)
  --uninstall   remove the launcher, desktop entry and unit
EOF
}

for arg in "$@"; do
  case "$arg" in
    --service) SERVICE=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $arg" >&2; usage; exit 2 ;;
  esac
done

if [[ "${UNINSTALL:-0}" == 1 ]]; then
  systemctl --user disable --now gwa-daemon.service 2>/dev/null || true
  rm -f "$BIN/gwa" "$APPS/gwa.desktop" "$UNITS/gwa-daemon.service"
  systemctl --user daemon-reload 2>/dev/null || true
  echo "gwa: uninstalled (state in ${XDG_STATE_HOME:-$HOME/.local/state}/gwa left alone)"
  exit 0
fi

missing=0
note() { printf '  %-24s %s\n' "$1" "$2"; }

echo "checking dependencies"
if /usr/bin/python3 -c 'import gi; gi.require_version("Gio","2.0")' 2>/dev/null; then
  note "PyGObject" "ok"
else
  note "PyGObject" "MISSING — install python3-gobject / python-gobject"; missing=1
fi
if command -v gst-launch-1.0 >/dev/null; then
  note "GStreamer" "ok"
else
  note "GStreamer" "MISSING — install gstreamer1-plugins-base / gstreamer1.0-tools"; missing=1
fi
if gst-inspect-1.0 pipewiresrc >/dev/null 2>&1; then
  note "pipewiresrc" "ok"
else
  note "pipewiresrc" "MISSING — install gst-plugin-pipewire / gstreamer1.0-pipewire"; missing=1
fi
if gdbus introspect --session --dest org.freedesktop.portal.Desktop \
     --object-path /org/freedesktop/portal/desktop >/dev/null 2>&1; then
  note "desktop portal" "ok"
else
  note "desktop portal" "not running — are you inside a graphical session?"; missing=1
fi

if [[ $missing -eq 1 ]]; then
  cat <<'EOF'

install the missing pieces first:
  Arch:   sudo pacman -S python-gobject gstreamer gst-plugins-base gst-plugin-pipewire
  Debian: sudo apt install python3-gi gstreamer1.0-tools gstreamer1.0-plugins-base \
                            gstreamer1.0-pipewire xdg-desktop-portal-gnome
  Fedora: sudo dnf install python3-gobject gstreamer1-plugins-base gstreamer1-plugin-pipewire
EOF
  exit 1
fi

mkdir -p "$BIN" "$APPS"
ln -sf "$REPO/bin/gwa" "$BIN/gwa"
chmod +x "$REPO/bin/gwa"
sed "s|^Exec=.*|Exec=$BIN/gwa daemon --quiet|" \
  "$REPO/packaging/gwa.desktop" > "$APPS/gwa.desktop"

echo
echo "installed:"
echo "  $BIN/gwa -> $REPO/bin/gwa"
echo "  $APPS/gwa.desktop"

if [[ $SERVICE -eq 1 ]]; then
  mkdir -p "$UNITS"
  sed "s|^ExecStart=.*|ExecStart=$BIN/gwa daemon --quiet|" \
    "$REPO/packaging/gwa-daemon.service" > "$UNITS/gwa-daemon.service"
  systemctl --user daemon-reload
  systemctl --user enable --now gwa-daemon.service
  echo "  $UNITS/gwa-daemon.service (enabled)"
fi

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo; echo "note: add $BIN to PATH to run 'gwa' from anywhere" ;;
esac

echo
echo "try: gwa doctor && gwa shot -o /tmp/shot.png"
