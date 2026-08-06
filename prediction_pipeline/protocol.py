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

# ROOT must be on sys.path before core.protocol can be imported when this
# package is loaded outside the repo root (scripts/tests add it themselves,
# but direct imports should not depend on the caller).
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.protocol import ProtocolError, load_protocol  # noqa: E402

PREDICTION_PROTOCOL_PATH = ROOT / "protocols" / "prediction_v1.json"
PREDICTION_PROTOCOL, PREDICTION_PROTOCOL_SHA256 = load_protocol(
    PREDICTION_PROTOCOL_PATH,
    required_sections={
        "protocol_name": str,
        "af2_prodigy": dict,
        "enrichment": dict,
    },
)

# Single source of truth for the predictor protocol referenced by Planner tasks
# and validated by Execution contracts.
PREDICTOR_PROTOCOL = PREDICTION_PROTOCOL["protocol_name"]
PREDICTOR_PROTOCOLS = frozenset({PREDICTOR_PROTOCOL})


def protocol_binding() -> dict:
    """Return the ``{name, version, sha256}`` binding recorded in artifacts."""
    return {
        "name": PREDICTOR_PROTOCOL,
        "version": PREDICTION_PROTOCOL["version"],
        "sha256": PREDICTION_PROTOCOL_SHA256,
    }


def reconcile_bundle_protocol(bundle: dict) -> None:
    """Bind the current protocol to an artifact bundle without relabeling history.

    A bundle produced before protocol recording has no ``protocol`` key and
    receives the current binding.  A bundle that already carries a protocol is
    preserved verbatim; a mismatch with the current protocol raises
    :class:`ProtocolError` instead of silently rewriting historical evidence.
    """
    existing = bundle.get("protocol")
    if existing is None:
        bundle["protocol"] = protocol_binding()
        return
    if existing != protocol_binding():
        raise ProtocolError(
            "artifact bundle protocol does not match the current prediction "
            "protocol; refusing to relabel historical evidence"
        )
