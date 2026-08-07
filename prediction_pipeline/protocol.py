"""Versioned scientific protocol for Prediction (Engineering Standard section 8 / Roadmap PR7).

Scientific parameters no longer live as Magic Numbers in execution handlers;
they are read from ``protocols/prediction_v1.json`` so results stay
reproducible and a parameter change forces a protocol version bump.

The task-level ``predictor_protocol`` identity object ``{name, version,
sha256}`` and the registered set are derived from the same file, giving
contracts and planners a single source of truth.  The loader and the
parameters-only identity SHA-256 are shared with Design via
``core.protocol``.
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

from core.protocol import (  # noqa: E402
    ProtocolError,
    canonical_parameters_sha256,
    load_protocol,
)

PREDICTION_PROTOCOL_PATH = ROOT / "protocols" / "prediction_v1.json"
PREDICTION_PROTOCOL, PREDICTION_PROTOCOL_IDENTITY_SHA256 = load_protocol(
    PREDICTION_PROTOCOL_PATH,
    required_sections={
        "af2_prodigy": dict,
        "enrichment": dict,
        "boltz": dict,
    },
)
# Parameters-only digest (scientific semantics); the identity digest above
# additionally binds name/version, so two protocols with identical parameters
# but different name/version are still distinct identities.
PREDICTION_PROTOCOL_PARAMETERS_SHA256 = canonical_parameters_sha256(
    PREDICTION_PROTOCOL["parameters"]
)
# Backward-compatible name: bundle bindings use the full identity digest.
PREDICTION_PROTOCOL_SHA256 = PREDICTION_PROTOCOL_IDENTITY_SHA256

AF2_MODEL_NUMBER_RANGE = range(0, 5)

_AF2_ALLOWED_KEYS = frozenset({"seeds", "model_numbers", "num_recycles"})
_ENRICHMENT_ALLOWED_KEYS = frozenset({
    "seed_base",
    "post_relax_seed_base",
    "post_relax_repeats",
    "post_relax_coordinate_stdev",
})
_BOLTZ_ALLOWED_KEYS = frozenset({"diffusion_samples"})


def _require_known_keys(section: dict, allowed: frozenset[str], label: str) -> None:
    """Reject typos inside a parameter section (e.g. ``recycels``)."""
    unknown = sorted(set(section) - allowed)
    if unknown:
        raise ProtocolError(
            f"prediction protocol {label} has unsupported fields {unknown}; "
            f"allowed keys are {sorted(allowed)}"
        )


@dataclass(frozen=True)
class PredictionProtocol:
    """Typed view of a prediction protocol (Engineering Standard section 8).

    Loading a protocol through :meth:`from_data` enforces the full scientific
    contract -- name/version, seeds/models presence and ranges, ensemble
    pairing, recycles and repeat counts, and unknown-key rejection -- so a
    typo in ``protocols/*.json`` fails at import time instead of surfacing as
    a bare KeyError or a bad subprocess later.
    """

    name: str
    version: str
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
        name = str(data.get("name") or "")
        version = str(data.get("version") or "")
        if not name or not version:
            raise ProtocolError(
                "prediction protocol must declare non-empty name and version"
            )
        parameters = data.get("parameters")
        if not isinstance(parameters, dict):
            raise ProtocolError(
                "prediction protocol must declare a 'parameters' object"
            )
        af2 = parameters.get("af2_prodigy")
        if not isinstance(af2, dict):
            raise ProtocolError("prediction protocol section 'af2_prodigy' must be an object")
        _require_known_keys(af2, _AF2_ALLOWED_KEYS, "af2_prodigy")
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
        enrichment = parameters.get("enrichment")
        if not isinstance(enrichment, dict):
            raise ProtocolError("prediction protocol section 'enrichment' must be an object")
        _require_known_keys(enrichment, _ENRICHMENT_ALLOWED_KEYS, "enrichment")
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
        boltz = parameters.get("boltz")
        if not isinstance(boltz, dict):
            raise ProtocolError("prediction protocol section 'boltz' must be an object")
        _require_known_keys(boltz, _BOLTZ_ALLOWED_KEYS, "boltz")
        diffusion_samples = _int(
            boltz.get("diffusion_samples"), "boltz.diffusion_samples"
        )
        if diffusion_samples <= 0:
            raise ProtocolError(
                "boltz.diffusion_samples must be a positive integer"
            )
        return cls(
            name=name,
            version=version,
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


# Single source of truth for the predictor protocol referenced by Planner
# tasks and validated by Execution contracts.  The registry currently holds
# exactly one protocol (protocols/prediction_v1.json) loaded at import time;
# extending it to multiple protocols requires loading each protocol file and
# recording its per-file identity SHA-256 -- until then the registry is a
# closed world with one active member.
PROTOCOL_REGISTRY: dict[str, PredictionProtocol] = {
    str(PREDICTION_PROTOCOL["name"]): PredictionProtocol.from_data(
        PREDICTION_PROTOCOL
    ),
}
ACTIVE_PREDICTOR_PROTOCOL = str(PREDICTION_PROTOCOL["name"])
PREDICTOR_PROTOCOLS = frozenset(PROTOCOL_REGISTRY)


def protocol_binding() -> dict:
    """Return the ``{name, version, sha256}`` identity of the active protocol.

    ``sha256`` is the parameters-only canonical digest, so metadata edits to
    the protocol file do not change the binding.
    """
    return {
        "name": ACTIVE_PREDICTOR_PROTOCOL,
        "version": PREDICTION_PROTOCOL["version"],
        "sha256": PREDICTION_PROTOCOL_IDENTITY_SHA256,
    }


# The identity object carried by Planner tasks (Action Contract); Execution
# requires the task identity to equal the active binding exactly.
PREDICTOR_PROTOCOL = protocol_binding()


# Shared operator hint for legacy evidence that cannot be bound automatically.
MIGRATE_LEGACY_HINT = (
    "run scripts/migrate_legacy_prediction_protocol.py to bind it "
    "explicitly, or regenerate the evidence"
)


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
            "artifact bundle has no recorded prediction protocol; "
            + MIGRATE_LEGACY_HINT
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
            "artifact bundle has no recorded prediction protocol; "
            + MIGRATE_LEGACY_HINT
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
