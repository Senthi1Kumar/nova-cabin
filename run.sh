#!/usr/bin/env bash
# Start Nova Cabin full screen on the Pi's Wayland desktop. Extra arguments go to main.py.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-wayland}"
exec .venv/bin/python main.py --fullscreen "$@"
