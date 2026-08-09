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

# Start an off-screen X server on $DISPLAY and wait until it is really serving.
start_xvfb() {
  export DISPLAY="${DISPLAY:-:99}"
  local res="${VNC_RESOLUTION:-1360x1020x24}"
  local n="${DISPLAY#:}"
  # A restart-in-place keeps /tmp, so a lock/socket left by the previous run
  # makes Xvfb refuse to start ("Server is already active for display N") while
  # the leftover socket file fools a naive existence check into reporting ready.
  # Clear both before launching.
  rm -f "/tmp/.X${n}-lock" "/tmp/.X11-unix/X${n}"
  Xvfb "$DISPLAY" -screen 0 "$res" -nolisten tcp &
  local xvfb_pid=$!
  for _ in $(seq 1 50); do
    # If Xvfb died (e.g. lock conflict) the socket alone means nothing.
    if ! kill -0 "$xvfb_pid" 2>/dev/null; then
      echo "Xvfb exited during startup on $DISPLAY" >&2
      return 1
    fi
    [ -e "/tmp/.X11-unix/X${n}" ] && return 0
    sleep 0.1
  done
  echo "Xvfb did not become ready on $DISPLAY" >&2
  return 1
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

# Chromium refuses to reuse a profile that still holds a Singleton* lock from a
# previous, uncleanly-killed run (SIGKILL / OOM skip a graceful close). Nothing
# is using the profile yet at container start, so clearing these is safe and
# lets the browser launch instead of erroring out "profile already in use".
profile_dir="${CHAT2API_USER_DATA_DIR:-/app/.browser_profile}"
rm -f "$profile_dir/SingletonLock" "$profile_dir/SingletonCookie" "$profile_dir/SingletonSocket"

if ! is_true "${CHAT2API_HEADLESS:-true}"; then
  start_xvfb
  echo "Running headful under Xvfb on $DISPLAY (needed for Cloudflare-gated sites)."
  if is_true "${CHAT2API_VNC:-}"; then
    start_vnc   # live browser: log in manually here, or watch for debugging
  fi
fi

exec python -m src.main
