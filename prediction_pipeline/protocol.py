"""Versioned scientific protocol for Prediction (Engineering Standard section 8 / Roadmap PR7).

Scientific parameters no longer live as Magic Numbers in execution handlers;
they are read from ``protocols/prediction_v1.json`` so results stay
reproducible and a parameter change forces a protocol version bump.

The task-level ``predictor_protocol`` name and its registered set are derived
from the same file, giving contracts and planners a single source of truth.
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

PREDICTION_PROTOCOL_PATH = ROOT / "protocols" / "prediction_v1.json"


def _load_prediction_protocol() -> dict:
    """Load the versioned prediction protocol from protocols/prediction_v1.json."""
    if not PREDICTION_PROTOCOL_PATH.is_file():
        raise FileNotFoundError(
            f"versioned prediction protocol missing: {PREDICTION_PROTOCOL_PATH}"
        )
    with open(PREDICTION_PROTOCOL_PATH, encoding="utf-8") as handle:
        return json.load(handle)


PREDICTION_PROTOCOL = _load_prediction_protocol()

# Single source of truth for the predictor protocol referenced by Planner tasks
# and validated by Execution contracts.
PREDICTOR_PROTOCOL = PREDICTION_PROTOCOL["protocol_name"]
PREDICTOR_PROTOCOLS = frozenset({PREDICTOR_PROTOCOL})
