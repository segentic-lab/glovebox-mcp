import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# import-time default for the smoke tests; the e2e server subprocess gets its own env
os.environ.setdefault("GLOVEBOX_VISION", "none")
