#!/bin/sh
# Brings up the virtual desktop the instances appear on, then waits. The MCP server is started
# per agent session with `docker exec -i <container> glovebox-server`, never here.
set -e
SIZE="${GLOVEBOX_DESKTOP_SIZE:-1920x1080}"

# a restarted container keeps /tmp: stale X locks would make every display look taken
rm -f /tmp/.X*-lock /tmp/.X11-unix/X* 2>/dev/null || true

Xvfb :0 -screen 0 "${SIZE}x24" -nolisten tcp >/tmp/xvfb.log 2>&1 &
XVFB=$!
i=0
until xdpyinfo -display :0 >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -gt 50 ] && { echo "glovebox: Xvfb did not start" >&2; cat /tmp/xvfb.log >&2; exit 1; }
  sleep 0.2
done
export DISPLAY=:0
xsetroot -solid "#2b2f36" 2>/dev/null || true
openbox >/tmp/openbox.log 2>&1 &

# The viewer: VNC stays on the container's loopback; only the noVNC web port is exposed, and the
# launcher/compose file publish that on the HOST's loopback only.
if [ -n "$GLOVEBOX_VNC_PASSWORD" ]; then
  x11vnc -storepasswd "$GLOVEBOX_VNC_PASSWORD" /tmp/.vncpass >/dev/null 2>&1
  AUTH="-rfbauth /tmp/.vncpass"
else
  AUTH="-nopw"
fi
x11vnc -display :0 -forever -shared -localhost -rfbport 5900 $AUTH -noxdamage -quiet >/tmp/x11vnc.log 2>&1 &
websockify --web /usr/share/novnc 6080 localhost:5900 >/tmp/novnc.log 2>&1 &

echo "glovebox: desktop ${SIZE} ready — watch it at ${GLOVEBOX_VIEW_URL:-http://localhost:6080/vnc.html}" >&2
wait "$XVFB"
