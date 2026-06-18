"""Put the in-repo trellis + trellis-keymap sources on the path for the
spike's tests, so `pytest` works from a checkout with nothing installed
but pytest + dearpygui."""

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for _src in (_REPO / "src", _REPO / "packages" / "trellis-keymap" / "src"):
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))
