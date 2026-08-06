"""Versioned scientific protocol for Prediction (Engineering Standard section 8 / Roadmap PR7).

Scientific parameters no longer live as Magic Numbers in execution handlers;
they are read from ``protocols/prediction_v1.json`` so results stay
reproducible and a parameter change forces a protocol version bump.

The task-level ``predictor_protocol`` name and its registered set are derived
from the same file, giving contracts and planners a single source of truth.
The loader and version/sha256 contract are shared with Design via
``core.protocol.load_protocol``.
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.protocol import load_protocol  # noqa: E402

PREDICTION_PROTOCOL_PATH = ROOT / "protocols" / "prediction_v1.json"
PREDICTION_PROTOCOL, PREDICTION_PROTOCOL_SHA256 = load_protocol(
    PREDICTION_PROTOCOL_PATH
)

# Single source of truth for the predictor protocol referenced by Planner tasks
# and validated by Execution contracts.
PREDICTOR_PROTOCOL = PREDICTION_PROTOCOL["protocol_name"]
PREDICTOR_PROTOCOLS = frozenset({PREDICTOR_PROTOCOL})
