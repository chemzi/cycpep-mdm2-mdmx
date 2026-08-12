"""Immutable contracts shared by Design, Prediction, and downstream agents."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from project_config import required_target_ids
from core.integrity import canonical_json, file_sha256, object_sha256
from peptide_contract import (
    MAX_CYCLIC_PEPTIDE_LENGTH,
    MIN_CYCLIC_PEPTIDE_LENGTH,
)


SCHEMA_VERSION = 1
PREDICTION_PIPELINE_VERSION = "1.5.1"
PREDICTION_SCORING_IMPLEMENTATION = "prediction_pipeline"
CANDIDATE_ID_RE = re.compile(r"^C\d{4,}$")
SEQUENCE_RE = re.compile(r"^[ACDEFGHIKLMNPQRSTVWY]+$")
SUPPORTED_CYCLIZATION = frozenset(
    {"head_to_tail_amide", "head-to-tail_amide", "head-to-tail-amide"}
)
SUPPORTED_DESIGN_REFERENCE_ROLES = frozenset({
    "rfdiffusion_target_bound_backbone",
    "experimental_cyclic_peptide_structure",
    "legacy_backbone_pdb",
})
PREDICTION_RECORD_STATUSES = (
    "finalized",
    "awaiting_threshold_calibration",
    "prediction_pending",
    "needs_optimization",
    "invalid",
)
CRITIC_READY_STATUSES = (
    "finalized",
    "awaiting_threshold_calibration",
    "needs_optimization",
)
UNEVALUATED_NON_READY_STATUSES = ("invalid",)


class ContractError(ValueError):
    """An input cannot safely enter Prediction."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def scoring_implementation_identity() -> dict:
    return {
        "name": PREDICTION_SCORING_IMPLEMENTATION,
        "version": PREDICTION_PIPELINE_VERSION,
    }


def prediction_status_from_battery(battery: dict) -> str:
    """Return the Prediction-owned scientific status for one evidence battery."""
    if not isinstance(battery, dict):
        raise ContractError("prediction_battery_invalid", "battery must be an object")
    required = (
        "competition_clearance",
        "metric_clearance",
        "triage_status",
        "missing_evidence",
        "missing_thresholds",
    )
    missing = [key for key in required if key not in battery]
    if missing:
        raise ContractError(
            "prediction_battery_invalid",
            f"battery is missing readiness fields: {missing}",
        )
    if battery["competition_clearance"]:
        return "finalized"
    if battery["metric_clearance"]:
        return "awaiting_threshold_calibration"
    if battery["triage_status"] == "invalid":
        return "invalid"
    if battery["missing_evidence"] or battery["missing_thresholds"]:
        return "prediction_pending"
    return "needs_optimization"


def _resolve_path(raw: str | Path, base: Path) -> Path:
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _read_json_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"{label}_missing", f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(
            f"{label}_malformed", f"{label} is not valid JSON: {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ContractError(f"{label}_type", f"{label} must contain a JSON object: {path}")
    return value


def _verify_declared_hash(path: Path, declared: str | None, label: str) -> str:
    if not path.is_file():
        raise ContractError(f"{label}_missing", f"{label} not found: {path}")
    actual = file_sha256(path)
    declared = str(declared or "").strip().lower()
    if declared and not actual.startswith(declared):
        raise ContractError(
            f"{label}_hash_mismatch",
            f"{label} hash mismatch for {path}: declared={declared}, actual={actual}",
        )
    return actual


@dataclass(frozen=True)
class PredictionConfig:
    """Method parameters and operational settings.

    These values define how a metric is calculated.  Selection thresholds stay
    in Research/State and are never substituted here.
    """

    mode: str = "production"
    ipsae_pae_cutoff: float = 10.0
    interface_distance_angstrom: float = 4.5
    seed_cluster_rmsd_angstrom: float = 2.0
    minimum_predictions_per_target: int = 3
    minimum_predictors_per_target: int = 2
    colabdesign_commit: str = "094e2cb3603dee7d99846e0977736bd943c830c2"

    def __post_init__(self):
        if self.mode != "production":
            raise ContractError("unsupported_mode", "Prediction supports production mode only")
        for name in (
            "ipsae_pae_cutoff",
            "interface_distance_angstrom",
            "seed_cluster_rmsd_angstrom",
        ):
            if float(getattr(self, name)) <= 0:
                raise ContractError("invalid_config", f"{name} must be positive")
        if self.minimum_predictions_per_target < 2:
            raise ContractError(
                "invalid_config", "minimum_predictions_per_target must be at least 2"
            )
        if self.minimum_predictors_per_target < 2:
            raise ContractError(
                "invalid_config", "minimum_predictors_per_target must be at least 2"
            )
        if not re.fullmatch(r"[0-9a-f]{40}", self.colabdesign_commit):
            raise ContractError(
                "invalid_config", "colabdesign_commit must be a full 40-character git SHA"
            )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict | None) -> "PredictionConfig":
        raw = dict(raw or {})
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ContractError(
                "unknown_config_keys", f"unknown Prediction config keys: {unknown}"
            )
        return cls(**raw)


@dataclass(frozen=True)
class CandidateInput:
    candidate_id: str
    sequence: str
    cyclization_type: str
    manifest_path: Path
    manifest_sha256: str
    legacy_refold_pdb: Path
    legacy_refold_sha256: str
    design_reference_pdb: Path | None
    design_reference_sha256: str | None
    design_reference_role: str | None
    source_route: str
    source_batch: str
    input_digest: str

    def snapshot(self) -> dict:
        result = {
            "candidate_id": self.candidate_id,
            "sequence": self.sequence,
            "cyclization_type": self.cyclization_type,
            "manifest_path": str(self.manifest_path),
            "manifest_sha256": self.manifest_sha256,
            "legacy_refold_pdb": str(self.legacy_refold_pdb),
            "legacy_refold_sha256": self.legacy_refold_sha256,
            "design_reference_pdb": (
                str(self.design_reference_pdb) if self.design_reference_pdb else None
            ),
            "design_reference_sha256": self.design_reference_sha256,
            "design_reference_role": self.design_reference_role,
            "source_route": self.source_route,
            "source_batch": self.source_batch,
            "input_digest": self.input_digest,
        }
        return result


def _validate_candidate_row(row: dict) -> tuple[str, str]:
    candidate_id = str(row.get("candidate_id") or "").strip()
    sequence = str(row.get("sequence") or "").strip().upper()
    if not CANDIDATE_ID_RE.fullmatch(candidate_id):
        raise ContractError(
            "candidate_id_invalid",
            f"candidate_id must match C followed by at least four digits: {candidate_id!r}",
        )
    if not SEQUENCE_RE.fullmatch(sequence):
        raise ContractError(
            "sequence_invalid",
            f"{candidate_id} sequence contains a non-standard amino acid",
        )
    if not MIN_CYCLIC_PEPTIDE_LENGTH <= len(sequence) <= MAX_CYCLIC_PEPTIDE_LENGTH:
        raise ContractError(
            "sequence_length_invalid",
            f"{candidate_id} sequence length {len(sequence)} is outside "
            f"{MIN_CYCLIC_PEPTIDE_LENGTH}-{MAX_CYCLIC_PEPTIDE_LENGTH}",
        )
    return candidate_id, sequence


def candidate_from_row(row: dict) -> CandidateInput:
    candidate_id, sequence = _validate_candidate_row(row)
    raw_manifest = str(row.get("manifest_path") or "").strip()
    if not raw_manifest:
        raise ContractError(
            "manifest_missing", f"{candidate_id} has no Design manifest_path"
        )
    manifest_path = Path(raw_manifest).expanduser().resolve()
    manifest = _read_json_object(manifest_path, "manifest")
    manifest_sha = file_sha256(manifest_path)

    manifest_id = str(manifest.get("candidate_id") or "").strip()
    manifest_sequence = str(manifest.get("sequence") or "").strip().upper()
    if manifest_id != candidate_id:
        raise ContractError(
            "manifest_candidate_mismatch",
            f"CandidateIndex has {candidate_id}, manifest has {manifest_id!r}",
        )
    if manifest_sequence != sequence:
        raise ContractError(
            "manifest_sequence_mismatch",
            f"{candidate_id} CandidateIndex/manifest sequences differ",
        )
    try:
        manifest_length = int(manifest.get("length"))
    except (TypeError, ValueError) as exc:
        raise ContractError(
            "manifest_length_invalid", f"{candidate_id} manifest length is invalid"
        ) from exc
    if manifest_length != len(sequence):
        raise ContractError(
            "manifest_length_mismatch",
            f"{candidate_id} manifest length does not match sequence",
        )

    cyclization = str(manifest.get("cyclization_type") or row.get("cyclization_type") or "")
    if cyclization not in SUPPORTED_CYCLIZATION:
        raise ContractError(
            "unsupported_cyclization",
            f"{candidate_id} cyclization {cyclization!r} is unsupported by L4",
        )
    cyclization = "head_to_tail_amide"

    base = manifest_path.parent
    raw_refold = manifest.get("refold_pdb") or row.get("design_pdb_path")
    if not raw_refold:
        raise ContractError(
            "legacy_refold_missing", f"{candidate_id} manifest has no refold_pdb"
        )
    refold = _resolve_path(raw_refold, base)
    refold_hash = _verify_declared_hash(
        refold,
        manifest.get("refold_pdb_hash") or row.get("design_pdb_hash"),
        "legacy_refold",
    )

    explicit_reference = str(manifest.get("design_reference_pdb") or "").strip()
    legacy_reference = str(manifest.get("backbone_pdb") or "").strip()
    if explicit_reference and legacy_reference:
        explicit_path = _resolve_path(explicit_reference, base)
        legacy_path = _resolve_path(legacy_reference, base)
        if explicit_path != legacy_path:
            raise ContractError(
                "design_reference_conflict",
                f"{candidate_id} explicit and compatibility Design references differ",
            )
    raw_reference = explicit_reference or legacy_reference
    reference = _resolve_path(raw_reference, base) if raw_reference else None
    reference_hash = None
    reference_role = None
    if reference:
        declared_role = str(manifest.get("design_reference_role") or "").strip()
        reference_role = declared_role or "legacy_backbone_pdb"
        if reference_role not in SUPPORTED_DESIGN_REFERENCE_ROLES:
            raise ContractError(
                "design_reference_role_invalid",
                f"{candidate_id} has unsupported Design reference role {reference_role!r}",
            )
        reference_hash = _verify_declared_hash(
            reference,
            manifest.get("design_reference_pdb_hash")
            or manifest.get("backbone_pdb_hash"),
            "design_reference",
        )
        if reference == refold or reference_hash == refold_hash:
            raise ContractError(
                "design_reference_not_independent",
                f"{candidate_id} fixed-sequence refold cannot be used as its L7 reference",
            )

    snapshot = {
        "candidate_id": candidate_id,
        "sequence": sequence,
        "cyclization_type": cyclization,
        "manifest_sha256": manifest_sha,
        "legacy_refold_sha256": refold_hash,
        "design_reference_sha256": reference_hash,
        "design_reference_role": reference_role,
        "source_route": str(manifest.get("source_route") or row.get("source_route") or ""),
        "source_batch": str(manifest.get("source_batch") or row.get("source_batch") or ""),
    }
    return CandidateInput(
        candidate_id=candidate_id,
        sequence=sequence,
        cyclization_type=cyclization,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha,
        legacy_refold_pdb=refold,
        legacy_refold_sha256=refold_hash,
        design_reference_pdb=reference,
        design_reference_sha256=reference_hash,
        design_reference_role=reference_role,
        source_route=snapshot["source_route"],
        source_batch=snapshot["source_batch"],
        input_digest=object_sha256(snapshot),
    )


def load_candidate_inputs(
    rows: Iterable[dict], candidate_ids: Iterable[str] | None = None
) -> list[CandidateInput]:
    requested = {str(value).strip() for value in (candidate_ids or []) if str(value).strip()}
    selected_rows = [
        row for row in rows
        if not requested or str(row.get("candidate_id") or "").strip() in requested
    ]
    found = {str(row.get("candidate_id") or "").strip() for row in selected_rows}
    missing = sorted(requested - found)
    if missing:
        raise ContractError(
            "candidate_not_found", f"CandidateIndex does not contain: {missing}"
        )
    if not selected_rows:
        raise ContractError("no_candidates", "Prediction received no Design candidates")

    seen: set[str] = set()
    candidates = []
    for row in selected_rows:
        candidate_id = str(row.get("candidate_id") or "").strip()
        if candidate_id in seen:
            raise ContractError(
                "duplicate_candidate", f"duplicate CandidateIndex row: {candidate_id}"
            )
        seen.add(candidate_id)
        candidates.append(candidate_from_row(row))
    return candidates


def validate_project(project: dict) -> tuple[str, ...]:
    review = project.get("review") or {}
    if review.get("status") != "approved":
        raise ContractError(
            "project_not_approved",
            f"project {project.get('project_id')} is not approved",
        )
    approved_digest = str(review.get("approved_digest") or "")
    content = json.loads(json.dumps(project))
    content.pop("review", None)
    current_digest = object_sha256(content)
    if not approved_digest or approved_digest != current_digest:
        raise ContractError(
            "project_approval_stale",
            "project content does not match its approved_digest",
        )
    targets = required_target_ids(project)
    if not targets:
        raise ContractError("no_required_targets", "project has no required target")
    return targets
