#!/usr/bin/env bash
# Dispatch between the container modes:
#   server  (default) — run the FastAPI server. Headless by default; if
#                       CHAT2API_HEADLESS=false it runs *headful* under a
#                       virtual display (Xvfb). Headful is required for sites
#                       behind a bot wall like Cloudflare (e.g. Perplexity),
#                       which never clears for a headless browser.
#   login             — headful Chromium behind noVNC for a one-time manual login
set -euo pipefail

# Start an off-screen X server on $DISPLAY and wait for it to accept clients.
start_xvfb() {
  export DISPLAY="${DISPLAY:-:99}"
  local res="${VNC_RESOLUTION:-1360x1020x24}"
  Xvfb "$DISPLAY" -screen 0 "$res" -nolisten tcp &
  for _ in $(seq 1 50); do
    [ -e "/tmp/.X11-unix/X${DISPLAY#:}" ] && break
    sleep 0.1
  done
}

mode="${1:-server}"

case "$mode" in
  server)
    # A headful server needs a display; headless doesn't. Compare
    # case-insensitively so "false"/"False"/"FALSE" all count.
    if [ "$(printf '%s' "${CHAT2API_HEADLESS:-true}" | tr '[:upper:]' '[:lower:]')" = "false" ]; then
      start_xvfb
      echo "Starting server headful under Xvfb on $DISPLAY (needed for Cloudflare-gated sites)."
    fi
    exec python -m src.main
    ;;

  login)
    start_xvfb

    # Export the framebuffer over VNC, then bridge VNC -> WebSocket for noVNC.
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
