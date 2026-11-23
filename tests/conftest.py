import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# import-time default for the smoke tests; the e2e server subprocess gets its own env
os.environ.setdefault("GLOVEBOX_VISION", "none")


import pytest


@pytest.fixture(autouse=True)
def _no_real_accessibility(monkeypatch):
    """Unit tests use made-up instance numbers; never let them reach a real browser or AT-SPI bus
    that happens to run on that display (parse_screen asks the app before vision). Tests of the
    accessibility path install their own; the e2e tests run the server in a subprocess."""
    import server
    monkeypatch.setattr(server, "_a11y_parse", lambda n: (None, "not asked in unit tests"))
