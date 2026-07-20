#!/usr/bin/env bash
# Launch the FastAPI server. Browser mode is driven entirely by env vars:
#
#   CHAT2API_HEADLESS=true  (default) — headless; no display, no VNC.
#   CHAT2API_HEADLESS=false           — headful under a virtual display (Xvfb).
#                                       Required for sites behind a bot wall like
#                                       Cloudflare (e.g. Perplexity), which never
#                                       clears for a headless browser.
#   CHAT2API_VNC=true                 — when headful, also expose the live
#                                       browser over noVNC (:6080). Connect there
#                                       to log in manually (cookies persist to the
#                                       profile volume) or to watch/debug.
set -euo pipefail

# Truthy check for env flags: true/1/yes (case-insensitive).
is_true() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    true | 1 | yes) return 0 ;;
    *) return 1 ;;
  esac
}

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

# Expose the current $DISPLAY over noVNC (:6080). NOTE: no VNC password — reach
# it over a private network / SSH tunnel, don't publish 6080 to the internet.
start_vnc() {
  x11vnc -display "$DISPLAY" -forever -shared -nopw -quiet -rfbport 5900 &
  websockify --web=/usr/share/novnc 6080 localhost:5900 &
  echo "noVNC ready — open http://<host>:6080/vnc.html and click Connect."
}

# Any non-server command (e.g. an interactive shell) runs verbatim.
if [ "$#" -gt 0 ] && [ "$1" != "server" ]; then
  exec "$@"
fi

if ! is_true "${CHAT2API_HEADLESS:-true}"; then
  start_xvfb
  echo "Running headful under Xvfb on $DISPLAY (needed for Cloudflare-gated sites)."
  if is_true "${CHAT2API_VNC:-}"; then
    start_vnc   # live browser: log in manually here, or watch for debugging
  fi
fi

exec python -m src.main
