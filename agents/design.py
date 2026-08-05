"""Legacy CLI shim for the Design Agent (于嘉乐).

Kept so Execution v1 can continue to invoke ``python agents/design.py --route ...``
unchanged.  All real code lives in the :mod:`agents.design` package.
"""

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.design.cli import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main())
