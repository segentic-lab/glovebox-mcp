# Glovebox MCP in a container — the same server, with a virtual desktop instead of yours.
#
#   Xvfb :0 (a desktop nobody sits at) + openbox + noVNC   <- you watch/use it at http://localhost:6080
#     └─ Xephyr :2, :3, …  one window per instance          <- exactly what launch_app does on a real desktop
#
# The MCP server is NOT the container's main process: agents attach with
#   docker exec -i glovebox glovebox-server
# so instances outlive a session and several agents share one desktop (see container.py).
#
# Build:  docker build -t glovebox-mcp:local .                          a browser (Chromium)
#         docker build -t glovebox-mcp:local --build-arg APPS=basic .   + editor, files, PDF/image viewer, terminal
#         docker build -t glovebox-mcp:local --build-arg APPS=full .    + LibreOffice, GIMP, Inkscape, Firefox, VLC
#         … --build-arg EXTRA_APT="thunderbird kate"                    any other Debian packages, on top
# (`GLOVEBOX_APPS=full python3 container.py build` and `./install.sh container --apps full` do the same.)
FROM debian:trixie-slim

ARG APPS=browser
ARG EXTRA_APT=""
ARG UID=1000
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8

# X stack + what server.py shells out to (procps: pkill; x11-utils: xprop/xdpyinfo), the AT-SPI
# bindings for the SYSTEM python (a11y_focus.py / a11y_tree.py), OCR, a browser, and the viewer.
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb xserver-xephyr openbox xdotool wmctrl xclip scrot \
      x11-utils x11-xkb-utils x11-xserver-utils xkb-data xfonts-base \
      dbus dbus-x11 at-spi2-core gir1.2-atspi-2.0 python3-gi \
      python3 python3-venv procps util-linux passwd tini ca-certificates xdg-utils \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-slv \
      chromium fonts-liberation fonts-dejavu-core fonts-noto-color-emoji \
      x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/*

# The app set — its own layer, so changing it does not rebuild the stack above. Everything here
# speaks AT-SPI (GTK/Qt/LibreOffice), so find_elements/act work on it without vision.
RUN set -e; \
    BASIC="mousepad pcmanfm xterm evince eog galculator file-roller gir1.2-gtk-3.0"; \
    case "$APPS" in \
      browser|chrome) PKGS="" ;; \
      basic) PKGS="$BASIC" ;; \
      full)  PKGS="$BASIC libreoffice libreoffice-gtk3 gimp inkscape firefox-esr vlc" ;; \
      *) echo "APPS must be browser, basic or full (got '$APPS')" >&2; exit 1 ;; \
    esac; \
    if [ -n "$PKGS$EXTRA_APT" ]; then \
      apt-get update && apt-get install -y --no-install-recommends $PKGS $EXTRA_APT \
      && rm -rf /var/lib/apt/lists/*; \
    fi

RUN useradd -m -u ${UID} -s /bin/bash glovebox \
    && mkdir -p /app/files /app/profiles /tmp/.X11-unix \
    && chmod 1777 /tmp/.X11-unix \
    && chown -R glovebox:glovebox /app

WORKDIR /app
COPY --chown=glovebox:glovebox pyproject.toml README.md _version.py ./
COPY --chown=glovebox:glovebox server.py vision.py vision_service.py host_bridge.py a11y.py a11y_focus.py \
     a11y_tree.py _paths.py container.py AGENTS.md ./
COPY docker/entrypoint.sh /usr/local/bin/glovebox-entrypoint
COPY docker/glovebox-server /usr/local/bin/glovebox-server
RUN chmod 755 /usr/local/bin/glovebox-entrypoint /usr/local/bin/glovebox-server \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir "/app[basic]"

# No USER line: the entrypoint starts as root to match the sandbox user to the owner of the
# mounted data folders, then drops to `glovebox`; glovebox-server drops the same way.
ENV GLOVEBOX_APPS=${APPS} \
    GLOVEBOX_IN_CONTAINER=1 \
    GLOVEBOX_HOST_DISPLAY=:0 \
    GLOVEBOX_VISION=auto \
    GLOVEBOX_VIEW_URL="http://localhost:6080/vnc.html?autoconnect=1&resize=scale"
VOLUME ["/app/files", "/app/profiles"]
EXPOSE 6080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD xdpyinfo -display :0 >/dev/null 2>&1 || exit 1
# the last step as root is the entrypoint's own; everything the agent starts runs as `glovebox`
ENTRYPOINT ["tini", "--", "glovebox-entrypoint"]
