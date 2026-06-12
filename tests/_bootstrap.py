"""Path shim imported first by every test. The tests are standalone scripts run
as `python tests/test_x.py`, so sys.path[0] is tests/ — but the engine package,
the root modules (client, main, win_ui, ...), and the shareware data all live
at the repo root, and the tests open `quake-shareware/...` by relative path.
Put the root on sys.path and chdir there so tests run from any cwd."""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)
