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
[[ "$MODE" == "claude" ]] && MODE="none"   # legacy alias
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

# ---- system packages (sudo; printed, not run) ----
APT_COMMON="xserver-xephyr openbox scrot x11-utils xdotool wmctrl xclip"
APT_MODE=""; [[ "$MODE" == "basic" ]] && APT_MODE="tesseract-ocr tesseract-ocr-slv"
echo "▶ System packages for mode '$MODE' (run if missing):"
echo "    sudo apt-get install -y $APT_COMMON $APT_MODE"
MISS=""
for b in Xephyr xdotool; do command -v "$b" >/dev/null 2>&1 || MISS="$MISS $b"; done
[[ "$MODE" == "basic" ]] && { command -v tesseract >/dev/null 2>&1 || MISS="$MISS tesseract"; }
[[ -n "$MISS" ]] && echo "  ⚠ missing:$MISS — run the apt line above, then re-run this script."

# ---- python env ----
[[ -d .venv ]] || uv venv --python 3.12 .venv
PY="$DIR/.venv/bin/python"

echo "▶ Installing Python deps (mode: $MODE) …"
uv pip install --python "$PY" "mcp[cli]" mss Pillow websocket-client

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
  [[ -d OmniParser ]] || git clone --depth 1 https://github.com/microsoft/OmniParser.git
  ( cd OmniParser && mkdir -p weights
    for f in icon_detect/train_args.yaml icon_detect/model.pt icon_detect/model.yaml \
             icon_caption/config.json icon_caption/generation_config.json icon_caption/model.safetensors; do
      "$PY" -c "from huggingface_hub import hf_hub_download as d; d(repo_id='microsoft/OmniParser-v2.0', filename='$f', local_dir='weights')"
    done
    [[ -d weights/icon_caption ]] && mv weights/icon_caption weights/icon_caption_florence || true )
  # make PaddleOCR optional in OmniParser (we use easyocr) — idempotent
  U="OmniParser/util/utils.py"
  if grep -qx "from paddleocr import PaddleOCR" "$U" 2>/dev/null; then
    "$PY" - "$U" <<'PYP'
import sys, re
p = sys.argv[1]; s = open(p).read()
s = s.replace("from paddleocr import PaddleOCR\n",
              "try:\n    from paddleocr import PaddleOCR\nexcept Exception:\n    PaddleOCR = None\n")
s = re.sub(r"(rec_batch_num=1024\))\n", r"\1 if PaddleOCR is not None else None\n", s, count=1)
open(p, "w").write(s); print("patched OmniParser util.utils.py")
PYP
  fi
  # verify torch actually sees CUDA at runtime
  "$PY" -c "import torch; assert torch.cuda.is_available(); print('✓ torch CUDA OK:', torch.cuda.get_device_name(0))" 2>/dev/null \
    || echo "  ⚠ torch installed but CUDA not visible at runtime — check the driver."
fi

echo "$MODE" > .vision-mode
echo "✓ Done. Vision mode = $MODE (.vision-mode). Restart your MCP client to apply."
