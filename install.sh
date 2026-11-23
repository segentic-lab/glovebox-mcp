#!/usr/bin/env bash
# Glovebox installer — system tools, the Python env, the vision backend, the Claude Code skill.
#
#   ./install.sh            auto: probe the GPU and install the best backend this machine can run
#   ./install.sh auto       same (NVIDIA GPU with >= 6 GB VRAM -> local, else basic, else none)
#   ./install.sh none       lightest: no local models — the agent reads screenshots itself
#   ./install.sh basic      Tesseract OCR (text + coords), no GPU/torch
#   ./install.sh local      OmniParser + ScreenParser grounding on the GPU (typed elements), heaviest/best
#   ./install.sh container  run Glovebox in a Docker container instead: builds the image, starts the container,
#                           writes the MCP config — needs only Docker (works on macOS/Windows hosts too)
#   ./install.sh skill      only install the Claude Code skill (skills/glovebox/SKILL.md) into ~/.claude/skills
#
# Flags:  --apps SET          container: browser (default) | basic (+ editor, files, viewers, terminal)
#                             | full (+ LibreOffice, GIMP, Inkscape, Firefox, VLC)
#         --vision host       container: share this machine's GPU vision service (Linux + ./install.sh local)
#         --no-skill          do not install the Claude Code skill (auto-installed when ~/.claude exists)
#         --no-screenparser   local mode: OmniParser's own detector only (no 153 MB ScreenParser download)
#         --dev               also install the test dependencies (pytest)
#
# System packages need sudo — on Debian/Ubuntu the script runs apt itself; elsewhere it prints the line.
# Idempotent: re-run it any time (it only downloads what is missing).
set -e
MODE=""; SKILL=1; SCREENPARSER=1; DEV=0; APPS=browser; CVISION=""; NEXT=""
for a in "$@"; do
  if [[ -n "$NEXT" ]]; then printf -v "$NEXT" '%s' "$a"; NEXT=""; continue; fi
  case "$a" in
    --apps) NEXT=APPS ;;
    --apps=*) APPS="${a#*=}" ;;
    --vision) NEXT=CVISION ;;
    --vision=*) CVISION="${a#*=}" ;;
    --no-skill) SKILL=0 ;;
    --no-screenparser) SCREENPARSER=0 ;;
    --dev) DEV=1 ;;
    -h|--help) sed -n 2,21p "$0"; exit 0 ;;
    *) MODE="$a" ;;
  esac
done
MODE="${MODE:-auto}"
REQUESTED="$MODE"   # what the user asked for (auto = let the server choose at run time)
cd "$(dirname "$0")"
DIR="$(pwd)"
export PATH="$HOME/.local/bin:$PATH"

install_skill() {
  DEST="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}/glovebox"
  mkdir -p "$DEST"
  cp "$DIR/skills/glovebox/SKILL.md" "$DEST/SKILL.md"
  echo "✓ Claude Code skill installed: $DEST/SKILL.md (picked up on the next session; /glovebox invokes it)"
}

if [[ "$MODE" == "skill" ]]; then install_skill; exit 0; fi

# ---- container mode: nothing is installed on this machine except the image ----
if [[ "$MODE" == "container" ]]; then
  command -v docker >/dev/null 2>&1 || {
    echo "✗ Docker not found — install Docker Desktop (macOS/Windows) or docker.io (Linux), then re-run."; exit 1; }
  docker info >/dev/null 2>&1 || { echo "✗ Docker is installed but its daemon is not reachable — start Docker, then re-run."; exit 1; }
  case "$APPS" in browser|basic|full) ;; *) echo "✗ --apps must be browser, basic or full (got '$APPS')"; exit 1 ;; esac
  case "$CVISION" in ""|host) ;; *) echo "✗ --vision must be 'host' (or omitted: OCR in the container)"; exit 1 ;; esac
  echo "▶ Building the image glovebox-mcp:local, apps: $APPS (several minutes the first time; browser ~1.8 GB, full ~4 GB) …"
  docker build -t glovebox-mcp:local --build-arg APPS="$APPS" "$DIR"
  PYBIN="$(command -v python3 || command -v python || true)"
  if [[ -n "$PYBIN" ]]; then
    # an existing container keeps its old image and mounts: recreate it so the new ones apply
    "$PYBIN" "$DIR/container.py" down >/dev/null
    GLOVEBOX_CONTAINER_VISION="$CVISION" "$PYBIN" "$DIR/container.py" up
    # the launcher starts the container when it is not running, so the config survives a reboot
    CMD="\"$PYBIN\""; ARGS="[\"$DIR/container.py\"]"
  else
    echo "▶ No python3 on this machine: starting the container with docker compose."
    [[ -n "$CVISION" ]] && echo "  (--vision host needs the launcher, i.e. python3 on the host — skipped)"
    (cd "$DIR" && docker compose up -d --force-recreate)
    CMD="\"docker\""; ARGS="[\"exec\", \"-i\", \"glovebox\", \"glovebox-server\"]"
  fi
  cat > mcp-config.json <<JSON
{
  "mcpServers": {
    "glovebox": {
      "command": $CMD,
      "args": $ARGS
    }
  }
}
JSON
  if [[ ! -f .mcp.json ]]; then cp mcp-config.json .mcp.json; echo "▶ wrote .mcp.json (Claude Code picks it up in this folder; copy it to other projects)"; fi
  [[ "$SKILL" == 1 && -d "$HOME/.claude" ]] && install_skill
  echo "✓ Done. Glovebox runs in the container 'glovebox'. Wrote mcp-config.json."
  echo "  Watch and use the sandbox desktop at http://localhost:6080/vnc.html?autoconnect=1&resize=scale"
  echo "  Files you stage and browser logins live in $DIR/container-data/ (kept when the container is rebuilt)."
  exit 0
fi

if [[ "$MODE" != "auto" && "$MODE" != "none" && "$MODE" != "basic" && "$MODE" != "local" ]]; then
  echo "Usage: ./install.sh [auto|none|basic|local|container|skill] [--no-skill] [--no-screenparser] [--dev]"
  echo "  auto   — probe the GPU, install the best backend this machine can run (default)"
  echo "  none   — no local grounding model (the agent's own vision); lightest, instant install"
  echo "  basic  — Tesseract OCR, no GPU"
  echo "  local  — OmniParser + ScreenParser on the GPU (best grounding; ~4 GB downloads)"
  echo "  container — run it in a Docker container (only Docker needed; macOS/Windows/Linux)"
  echo "  skill  — only the Claude Code skill"
  exit 1
fi

# ---- GPU probe (the same thresholds vision.py uses: >= 6 GB VRAM for OmniParser) ----
GPU_NAME=""; VRAM=""
if command -v nvidia-smi >/dev/null 2>&1; then
  GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
  VRAM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -dc '0-9')
fi
if [[ -n "$VRAM" ]]; then echo "▶ GPU: ${GPU_NAME:-unknown} — ${VRAM} MiB VRAM"; else echo "▶ GPU: none visible (nvidia-smi not found or no NVIDIA device)"; fi
if [[ "$MODE" == "auto" ]]; then
  if [[ -n "$VRAM" && "$VRAM" -ge 6000 ]]; then MODE=local
  else MODE=basic; fi
  echo "▶ auto → $MODE$( [[ "$MODE" == basic ]] && echo " (no NVIDIA GPU with >= 6 GB VRAM; OCR grounding on the CPU)" )"
fi

# ---- system packages — auto via apt (Debian/Ubuntu); guided elsewhere (the server is distro-agnostic) ----
# tesseract is installed for every mode: it is small and the server's `auto` backend selection
# falls back to it when the GPU is busy or OmniParser is not usable.
APT_PKGS="xserver-xephyr openbox scrot x11-utils x11-xkb-utils x11-xserver-utils xdotool wmctrl xclip tesseract-ocr tesseract-ocr-slv at-spi2-core gir1.2-atspi-2.0 python3-gi"
MISS=""
for b in Xephyr openbox xdotool wmctrl xclip xdpyinfo xkbcomp xmodmap tesseract; do command -v "$b" >/dev/null 2>&1 || MISS="$MISS $b"; done
# AT-SPI (optional, apt only): lets fill() check which native widget has focus before it types
if command -v apt-get >/dev/null 2>&1 && ! /usr/bin/python3 -c "import gi; gi.require_version('Atspi','2.0')" >/dev/null 2>&1; then
  MISS="$MISS python3-gi/gir1.2-atspi-2.0"
fi
if [[ -z "$MISS" ]]; then
  echo "▶ System tools present (Xephyr, openbox, xdotool, wmctrl, xclip, xkbcomp, tesseract) ✓"
elif command -v apt-get >/dev/null 2>&1; then
  echo "▶ Missing:$MISS — installing via apt (needs sudo) …"
  sudo apt-get install -y $APT_PKGS || {
    echo "✗ apt failed — run it yourself, then re-run: sudo apt-get install -y $APT_PKGS"; exit 1; }
elif command -v dnf >/dev/null 2>&1; then
  echo "▶ Missing:$MISS — install with dnf, then re-run this script:"
  echo "    sudo dnf install -y xorg-x11-server-Xephyr openbox scrot xorg-x11-utils xorg-x11-xkb-utils xorg-x11-server-utils xdotool wmctrl xclip tesseract tesseract-langpack-slv at-spi2-core python3-gobject"; exit 1
elif command -v pacman >/dev/null 2>&1; then
  echo "▶ Missing:$MISS — install with pacman, then re-run this script:"
  echo "    sudo pacman -S --needed xorg-server-xephyr openbox scrot xorg-xdpyinfo xorg-xkbcomp xorg-setxkbmap xorg-xmodmap xdotool wmctrl xclip tesseract tesseract-data-slv tesseract-data-eng at-spi2-core python-gobject"; exit 1
else
  echo "▶ Missing:$MISS — install these tools with your package manager, then re-run:"
  echo "    Xephyr, openbox, scrot, xdpyinfo (x11-utils), xkbcomp + setxkbmap (x11-xkb-utils), xmodmap, xdotool, wmctrl, xclip, tesseract (+ slv data); optional: at-spi2-core + PyGObject for the system python3 (fill's focus check)"; exit 1
fi

# ---- python env (deps come from pyproject's extras: one source of truth) ----
command -v uv >/dev/null 2>&1 || {
  echo "✗ uv not found — install it first: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
[[ -d .venv ]] || uv venv --python 3.12 .venv
PY="$DIR/.venv/bin/python"
EXTRAS="basic"
[[ "$MODE" == "local" ]] && EXTRAS="$EXTRAS,local"
[[ "$DEV" == 1 ]] && EXTRAS="$EXTRAS,dev"
echo "▶ Installing Python deps (mode: $MODE, extras: $EXTRAS) …"
uv pip install --python "$PY" -q -e ".[$EXTRAS]"

if [[ "$MODE" == "local" ]]; then
  if [[ -z "$VRAM" ]]; then
    echo "✗ 'local' needs an NVIDIA GPU, but none is visible. Use './install.sh basic' instead."; exit 1
  fi
  if [[ "$VRAM" -lt 6000 ]]; then
    echo "✗ OmniParser needs ~6 GB VRAM; found ${VRAM} MiB. Use './install.sh basic' instead."; exit 1
  fi
  [[ "$VRAM" -lt 8000 ]] && echo "  ⚠ ${VRAM} MiB is tight — should work, but close other GPU apps before parsing."
  # the server owns the model lifecycle (clone + weights + import patches) — see vision.py
  "$PY" vision.py install-omniparser
  if [[ "$SCREENPARSER" == 1 ]]; then "$PY" vision.py install-screenparser; fi
  "$PY" -c "import torch; assert torch.cuda.is_available(); print('✓ torch sees CUDA:', torch.cuda.get_device_name(0))" 2>/dev/null \
    || echo "  ⚠ torch installed but CUDA not visible at runtime — check the NVIDIA driver (nvidia-smi works, torch does not)."
fi

# 'auto' in .vision-mode: the server picks the best READY backend at run time (local > basic > none)
# and says why; an explicit mode is written only when the user asked for one.
echo "$REQUESTED" > .vision-mode
echo "▶ Vision diagnosis (same as the status() tool):"
"$PY" vision.py check | "$PY" -c "import json,sys; d=json.load(sys.stdin); r=d['resolution']; det=d['omniparser']['detector']; print(f\"  requested={r['requested']} effective={r['effective']} ready={r['ready']} — {r['reason']}\"); print(f\"  detector: {det['effective']} — {det['reason']}\") if r['effective']=='local' else None"

# ---- MCP client config with the real install path (generic + Claude Code's .mcp.json) ----
cat > mcp-config.json <<JSON
{
  "mcpServers": {
    "glovebox": {
      "command": "$DIR/.venv/bin/python",
      "args": ["$DIR/server.py"],
      "env": { "DISPLAY": ":1" }
    }
  }
}
JSON
if [[ ! -f .mcp.json ]]; then cp mcp-config.json .mcp.json; echo "▶ wrote .mcp.json (Claude Code picks it up in this folder; copy it to other projects)"; fi

[[ "$SKILL" == 1 && -d "$HOME/.claude" ]] && install_skill

echo "✓ Done. Vision mode = $(cat .vision-mode) (effective backend shown above). Wrote mcp-config.json (paths → $DIR)."
echo "  Add its \"glovebox\" block to your MCP client config (Claude Code: already in .mcp.json), then restart the client."
