"""Legacy CLI shim for the Orchestrator Agent (PR6).

Kept so ``python agents/orchestrator.py ...`` keeps working unchanged.
All real code lives in the :mod:`agents.orchestrator` package.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.orchestrator.cli import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main())
