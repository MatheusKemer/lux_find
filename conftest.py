"""Make the package importable from a plain checkout (no install required)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for extra in (ROOT / "src", ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))
