#!/usr/bin/env bash
# Glovebox — nested X11 "automation sandbox" for the Glovebox MCP.
#
# Opens a Xephyr window on DISPLAY :1 (a real X server, so xdotool input works even though the
# host is Wayland) with a window manager + a browser. The MCP server runs with DISPLAY=:1,
# so the agent's mouse/keyboard/screenshots are CONFINED TO THIS WINDOW —
# it cannot touch your real desktop, files, or other apps.
#
# Usage:   ./start-display.sh        (run once; leave it open)
# Stop:    pkill Xephyr              (closes the whole sandbox)
set -e
DISP=:1
RES="${RES:-1440x900}"
DIR="$(cd "$(dirname "$0")" && pwd)"

if ! xdpyinfo -display "$DISP" >/dev/null 2>&1; then
  echo "▶ Starting Xephyr on $DISP ($RES) …"
  Xephyr "$DISP" -screen "$RES" -title "Glovebox :1 — automation sandbox" -resizeable -ac \
    >/tmp/xephyr.log 2>&1 &
  for _ in $(seq 1 25); do xdpyinfo -display "$DISP" >/dev/null 2>&1 && break; sleep 0.3; done
else
  echo "▶ Display $DISP already running."
fi

export DISPLAY="$DISP"
# CRITICAL: without this, snap Chromium picks the Wayland backend and renders to your REAL screen,
# leaving the X11 :1 sandbox black. Force X11/Xephyr.
unset WAYLAND_DISPLAY
export GDK_BACKEND=x11

# minimal window manager (movable/decorated windows)
pgrep -x openbox >/dev/null 2>&1 || { openbox >/tmp/openbox-x1.log 2>&1 & sleep 0.6; }

# browser with its OWN profile in this folder (logins live here, separate from your real browser).
# --ozone-platform=x11 → render to :1 (Xephyr); --disable-gpu → reliable in a nested X server.
chromium --ozone-platform=x11 --disable-gpu \
  --user-data-dir="$DIR/chrome-profile" --no-first-run --no-default-browser-check \
  --remote-debugging-port=9223 --remote-allow-origins=* \
  --new-window "about:blank" >/tmp/chromium-x1.log 2>&1 &

echo "✓ Sandbox ready on $DISP. Log into sites in THIS window."
echo "  The Glovebox MCP (DISPLAY=:1) drives only this display."
echo "  If the browser window doesn't appear: try  DISPLAY=:1 chromium-browser  or install firefox."
