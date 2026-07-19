#!/usr/bin/env bash
# Dispatch between the two container modes:
#   server  (default) — run the FastAPI server headless
#   login             — headful Chromium behind noVNC for a one-time manual login
set -euo pipefail

mode="${1:-server}"

case "$mode" in
  server)
    exec python -m src.main
    ;;

  login)
    export DISPLAY="${DISPLAY:-:99}"
    res="${VNC_RESOLUTION:-1360x1020x24}"

    # Virtual framebuffer for the (otherwise invisible) Chromium window.
    Xvfb "$DISPLAY" -screen 0 "$res" -nolisten tcp &
    for _ in $(seq 1 50); do
      [ -e "/tmp/.X11-unix/X${DISPLAY#:}" ] && break
      sleep 0.1
    done

    # Export that framebuffer over VNC, then bridge VNC -> WebSocket for noVNC.
    x11vnc -display "$DISPLAY" -forever -shared -nopw -quiet -rfbport 5900 &
    websockify --web=/usr/share/novnc 6080 localhost:5900 &

    echo "noVNC ready — open http://<host>:6080/vnc.html and click Connect."
    exec python -m src.login
    ;;

  *)
    # Fall through: run whatever was passed (e.g. an interactive shell).
    exec "$@"
    ;;
esac
