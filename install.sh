#!/usr/bin/env bash
# Glovebox installer — pick the vision/grounding backend.
#
#   ./install.sh none     lightest: no local models — the agent reads screenshots itself
#   ./install.sh basic     Tesseract OCR (text + coords), no GPU/torch
#   ./install.sh local     OmniParser grounding on the GPU (text+icons), heaviest/best
#
# System packages need sudo — the script prints the apt line for you to run.
set -e
MODE="${1:-}"
cd "$(dirname "$0")"
DIR="$(pwd)"
export PATH="$HOME/.local/bin:$PATH"

if [[ "$MODE" != "none" && "$MODE" != "basic" && "$MODE" != "local" ]]; then
  echo "Usage: ./install.sh <none|basic|local>"
  echo "  none   — no local grounding model (the agent's own vision); lightest, instant install"
  echo "  basic  — Tesseract OCR, no GPU"
  echo "  local  — OmniParser on GPU (best grounding; ~4GB downloads)"
  exit 1
fi

# ---- system packages — auto via apt (Debian/Ubuntu); guided elsewhere (the server is distro-agnostic) ----
APT_PKGS="xserver-xephyr openbox scrot x11-utils xdotool wmctrl xclip"
[[ "$MODE" == "basic" ]] && APT_PKGS="$APT_PKGS tesseract-ocr tesseract-ocr-slv"
TESS=""; [[ "$MODE" == "basic" ]] && TESS=" tesseract"
MISS=""
for b in Xephyr openbox xdotool wmctrl xclip xdpyinfo; do command -v "$b" >/dev/null 2>&1 || MISS="$MISS $b"; done
[[ "$MODE" == "basic" ]] && { command -v tesseract >/dev/null 2>&1 || MISS="$MISS tesseract"; }
if [[ -z "$MISS" ]]; then
  echo "▶ System tools present (Xephyr, openbox, xdotool, wmctrl, xclip) ✓"
elif command -v apt-get >/dev/null 2>&1; then
  echo "▶ Missing:$MISS — installing via apt (needs sudo) …"
  sudo apt-get install -y $APT_PKGS || {
    echo "✗ apt failed — run it yourself, then re-run: sudo apt-get install -y $APT_PKGS"; exit 1; }
elif command -v dnf >/dev/null 2>&1; then
  echo "▶ Missing:$MISS — install with dnf, then re-run this script:"
  echo "    sudo dnf install -y xorg-x11-server-Xephyr openbox scrot xorg-x11-utils xdotool wmctrl xclip$TESS"; exit 1
elif command -v pacman >/dev/null 2>&1; then
  echo "▶ Missing:$MISS — install with pacman, then re-run this script:"
  echo "    sudo pacman -S --needed xorg-server-xephyr openbox scrot xorg-xdpyinfo xdotool wmctrl xclip$TESS"; exit 1
else
  echo "▶ Missing:$MISS — install these tools with your package manager, then re-run:"
  echo "    Xephyr, openbox, scrot, xdpyinfo (x11-utils), xdotool, wmctrl, xclip$TESS"; exit 1
fi

# ---- python env ----
command -v uv >/dev/null 2>&1 || {
  echo "✗ uv not found — install it first: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
[[ -d .venv ]] || uv venv --python 3.12 .venv
PY="$DIR/.venv/bin/python"

echo "▶ Installing Python deps (mode: $MODE) …"
uv pip install --python "$PY" "mcp[cli]>=1.9,<2" mss Pillow websocket-client

if [[ "$MODE" == "basic" ]]; then
  uv pip install --python "$PY" pytesseract

elif [[ "$MODE" == "local" ]]; then
  # ---- CUDA + VRAM preflight (OmniParser = Florence-2 + YOLO) ----
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "✗ 'local' needs an NVIDIA GPU, but nvidia-smi was not found."
    echo "  Use './install.sh basic' (Tesseract, CPU) or './install.sh none' instead."; exit 1
  fi
  GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
  VRAM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -dc '0-9')
  echo "▶ GPU: ${GPU_NAME:-unknown} — ${VRAM:-?} MiB VRAM"
  if [[ -z "$VRAM" || "$VRAM" -lt 6000 ]]; then
    echo "✗ OmniParser needs ~6 GB VRAM; found ${VRAM:-0} MiB."
    echo "  Use './install.sh basic' or './install.sh none' instead."; exit 1
  fi
  [[ "$VRAM" -lt 8000 ]] && echo "  ⚠ ${VRAM} MiB is tight — should work, but close other GPU apps before parsing."
  uv pip install --python "$PY" --index-url https://download.pytorch.org/whl/cu124 torch torchvision
  # openai + matplotlib are imported by OmniParser's util.utils at import time (not used for inference here).
  uv pip install --python "$PY" ultralytics==8.3.70 "transformers==4.49.0" accelerate timm einops==0.8.0 \
    easyocr supervision==0.18.0 opencv-python-headless "numpy==1.26.4" huggingface_hub openai matplotlib
  # the server owns the OmniParser lifecycle (clone + weights + PaddleOCR patch) — see vision.py
  "$PY" vision.py install-omniparser
  # verify torch actually sees CUDA at runtime
  "$PY" -c "import torch; assert torch.cuda.is_available(); print('✓ torch CUDA OK:', torch.cuda.get_device_name(0))" 2>/dev/null \
    || echo "  ⚠ torch installed but CUDA not visible at runtime — check the driver."
fi

echo "$MODE" > .vision-mode
echo "▶ Vision diagnosis (same as the status() tool):"
"$PY" vision.py check | "$PY" -c "import json,sys; r=json.load(sys.stdin)['resolution']; print(f\"  requested={r['requested']} effective={r['effective']} ready={r['ready']} — {r['reason']}\")"

# ---- write a ready-to-use MCP client config with the real install path ----
cat > mcp-config.json <<EOF
{
  "mcpServers": {
    "glovebox": {
      "command": "$DIR/.venv/bin/python",
      "args": ["$DIR/server.py"],
      "env": { "DISPLAY": ":1" }
    }
  }
}
EOF

echo "✓ Done. Vision mode = $MODE. Wrote mcp-config.json (paths → $DIR)."
echo "  Add its \"glovebox\" block to your MCP client config, then restart the client."
