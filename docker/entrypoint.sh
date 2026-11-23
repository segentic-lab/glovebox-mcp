#!/bin/sh
# PID 1 of the container (under tini). Runs as root only long enough to make the mounted data
# folders writable by the unprivileged user, then becomes that user and starts the desktop.
# The MCP server is started per agent session with `docker exec -i <container> glovebox-server`.
set -e
if [ "$(id -u)" = "0" ]; then
  # The files/profiles folders are bind mounts from the host. On Linux they keep the host
  # owner's numeric id, which is not necessarily 1000 (a CI runner is 1001): without this the
  # sandbox user could not write its own staging folder. Give the user the id of the folder's
  # owner; a folder Docker created itself (owned by root) is handed to the user instead.
  WANT_UID="${GLOVEBOX_UID:-$(stat -c %u /app/files)}"
  WANT_GID="${GLOVEBOX_GID:-$(stat -c %g /app/files)}"
  if [ "$WANT_UID" != "0" ] && [ "$WANT_UID" != "$(id -u glovebox)" ]; then
    [ "$WANT_GID" != "0" ] && [ "$WANT_GID" != "$(id -g glovebox)" ] && groupmod -o -g "$WANT_GID" glovebox
    usermod -o -u "$WANT_UID" glovebox
    chown -R glovebox:glovebox /home/glovebox
    find /app -xdev -maxdepth 1 ! -name files ! -name profiles -exec chown glovebox:glovebox {} +
  fi
  for d in /app/files /app/profiles; do
    [ "$(stat -c %u "$d")" = "0" ] && chown glovebox:glovebox "$d"
  done
  exec setpriv --reuid=glovebox --regid=glovebox --init-groups env HOME=/home/glovebox "$0" "$@"
fi

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

echo "glovebox: desktop ${SIZE} ready (user $(id -u)) — watch it at ${GLOVEBOX_VIEW_URL:-http://localhost:6080/vnc.html}" >&2
wait "$XVFB"
