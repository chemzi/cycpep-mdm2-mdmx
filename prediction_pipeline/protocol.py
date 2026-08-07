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
from dataclasses import dataclass
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

AF2_MODEL_NUMBER_RANGE = range(0, 5)


@dataclass(frozen=True)
class PredictionProtocol:
    """Typed view of a prediction protocol (Engineering Standard section 8).

    Loading a protocol through :meth:`from_data` enforces the full scientific
    contract -- seeds/models presence and ranges, ensemble pairing, recycles
    and repeat counts -- so a typo in ``protocols/*.json`` fails at import
    time instead of surfacing as a bare KeyError or a bad subprocess later.
    """

    version: str
    protocol_name: str
    af2_seeds: tuple[int, ...]
    af2_model_numbers: tuple[int, ...]
    num_recycles: int
    enrichment_seed_base: int
    post_relax_seed_base: int
    post_relax_repeats: int
    post_relax_coordinate_stdev: float
    boltz_diffusion_samples: int

    @classmethod
    def from_data(cls, data: dict) -> "PredictionProtocol":
        version = str(data.get("version") or "")
        protocol_name = str(data.get("protocol_name") or "")
        if not version or not protocol_name:
            raise ProtocolError(
                "prediction protocol must declare non-empty version and protocol_name"
            )
        af2 = data.get("af2_prodigy")
        if not isinstance(af2, dict):
            raise ProtocolError("prediction protocol section 'af2_prodigy' must be an object")
        seeds = _int_list(af2.get("seeds"), "af2_prodigy.seeds")
        models = _int_list(af2.get("model_numbers"), "af2_prodigy.model_numbers")
        if not seeds:
            raise ProtocolError("af2_prodigy.seeds must not be empty")
        if not models:
            raise ProtocolError("af2_prodigy.model_numbers must not be empty")
        if len(seeds) != len(models):
            raise ProtocolError(
                "af2_prodigy.seeds and af2_prodigy.model_numbers must have equal length"
            )
        if len(set(seeds)) != len(seeds):
            raise ProtocolError("af2_prodigy.seeds must not contain duplicates")
        if len(set(models)) != len(models):
            raise ProtocolError("af2_prodigy.model_numbers must not contain duplicates")
        if any(model not in AF2_MODEL_NUMBER_RANGE for model in models):
            raise ProtocolError(
                "af2_prodigy.model_numbers must be within "
                f"{min(AF2_MODEL_NUMBER_RANGE)}-{max(AF2_MODEL_NUMBER_RANGE)}"
            )
        recycles = af2.get("num_recycles")
        if not isinstance(recycles, int) or isinstance(recycles, bool) or recycles <= 0:
            raise ProtocolError("af2_prodigy.num_recycles must be a positive integer")
        enrichment = data.get("enrichment")
        if not isinstance(enrichment, dict):
            raise ProtocolError("prediction protocol section 'enrichment' must be an object")
        seed_base = _int(enrichment.get("seed_base"), "enrichment.seed_base")
        post_relax_seed_base = _int(
            enrichment.get("post_relax_seed_base"), "enrichment.post_relax_seed_base"
        )
        post_relax_repeats = _int(
            enrichment.get("post_relax_repeats"), "enrichment.post_relax_repeats"
        )
        if seed_base < 0:
            raise ProtocolError("enrichment.seed_base must be non-negative")
        if post_relax_seed_base < 0:
            raise ProtocolError("enrichment.post_relax_seed_base must be non-negative")
        if post_relax_repeats <= 0:
            raise ProtocolError("enrichment.post_relax_repeats must be a positive integer")
        coordinate_stdev = enrichment.get("post_relax_coordinate_stdev")
        if (
            not isinstance(coordinate_stdev, (int, float))
            or isinstance(coordinate_stdev, bool)
            or coordinate_stdev <= 0
        ):
            raise ProtocolError(
                "enrichment.post_relax_coordinate_stdev must be a positive number"
            )
        boltz = data.get("boltz")
        if not isinstance(boltz, dict):
            raise ProtocolError("prediction protocol section 'boltz' must be an object")
        diffusion_samples = _int(
            boltz.get("diffusion_samples"), "boltz.diffusion_samples"
        )
        if diffusion_samples <= 0:
            raise ProtocolError(
                "boltz.diffusion_samples must be a positive integer"
            )
        return cls(
            version=version,
            protocol_name=protocol_name,
            af2_seeds=tuple(seeds),
            af2_model_numbers=tuple(models),
            num_recycles=recycles,
            enrichment_seed_base=seed_base,
            post_relax_seed_base=post_relax_seed_base,
            post_relax_repeats=post_relax_repeats,
            post_relax_coordinate_stdev=float(coordinate_stdev),
            boltz_diffusion_samples=diffusion_samples,
        )


def _int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError(f"prediction protocol {label} must be an integer")
    return value


def _int_list(value: object, label: str) -> list[int]:
    if not isinstance(value, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in value
    ):
        raise ProtocolError(f"prediction protocol {label} must be a list of integers")
    return value


# Single source of truth for the predictor protocol referenced by Planner tasks
# and validated by Execution contracts.  ``PREDICTOR_PROTOCOLS`` is derived
# from the registry, so adding a protocol file extends the closed world
# instead of hard-coding the set.
PROTOCOL_REGISTRY: dict[str, PredictionProtocol] = {
    str(PREDICTION_PROTOCOL["protocol_name"]): PredictionProtocol.from_data(
        PREDICTION_PROTOCOL
    ),
}
ACTIVE_PREDICTOR_PROTOCOL = str(PREDICTION_PROTOCOL["protocol_name"])
PREDICTOR_PROTOCOL = ACTIVE_PREDICTOR_PROTOCOL
PREDICTOR_PROTOCOLS = frozenset(PROTOCOL_REGISTRY)


def protocol_binding() -> dict:
    """Return the ``{name, version, sha256}`` binding recorded in artifacts."""
    return {
        "name": ACTIVE_PREDICTOR_PROTOCOL,
        "version": PREDICTION_PROTOCOL["version"],
        "sha256": PREDICTION_PROTOCOL_SHA256,
    }


def validate_bundle_protocol(bundle: dict) -> None:
    """Refuse to relabel a bundle whose recorded protocol is unknown or stale.

    Runtime code must never guess a protocol for legacy evidence: a bundle
    without a ``protocol`` binding is rejected here instead of being silently
    stamped with the current protocol.  Legacy bundles must be bound
    explicitly by an operator via
    ``scripts/migrate_legacy_prediction_protocol.py``; a bundle that already
    carries a different binding is rejected verbatim so history is never
    rewritten.
    """
    existing = bundle.get("protocol")
    if existing is None:
        raise ProtocolError(
            "artifact bundle has no recorded prediction protocol; run "
            "scripts/migrate_legacy_prediction_protocol.py to bind it "
            "explicitly, or regenerate the evidence"
        )
    if existing != protocol_binding():
        raise ProtocolError(
            "artifact bundle protocol does not match the current prediction "
            "protocol; refusing to relabel historical evidence"
        )


def validate_execution_compatibility(bundle: dict) -> None:
    """Execution-side check: a bundle must match the protocol being run.

    :func:`load_artifact_bundle` only verifies a bundle is a *valid history*
    (a well-formed protocol binding), so an older-protocol bundle stays
    readable.  This function is the separate execution gate: evidence bound to
    a different protocol must not be mixed into a run that executes under
    another protocol, or provenance would be silently corrupted.  Execution
    can only run the active protocol, so the recorded binding must equal
    ``protocol_binding()`` exactly.
    """
    existing = bundle.get("protocol")
    if existing is None:
        raise ProtocolError(
            "artifact bundle has no recorded prediction protocol; run "
            "scripts/migrate_legacy_prediction_protocol.py to bind it "
            "explicitly, or regenerate the evidence"
        )
    if not isinstance(existing, dict) or (
        existing.get("name") != ACTIVE_PREDICTOR_PROTOCOL
    ):
        raise ProtocolError(
            f"artifact bundle protocol is not executable under "
            f"{ACTIVE_PREDICTOR_PROTOCOL!r}; refusing to mix historical evidence"
        )
    if existing != protocol_binding():
        raise ProtocolError(
            "artifact bundle protocol binding does not match the current "
            "prediction protocol; refusing to execute against stale evidence"
        )
