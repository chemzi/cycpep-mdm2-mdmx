"""records - split from agents/critic/report.py (PR6)."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from prediction_pipeline.contracts import file_sha256
from .config import ALLOWED_STATUSES
from .errors import CriticContractError
from .io import _json_object, _resolve_path

def _validate_handoff_envelope(handoff_path: Path) -> dict:
    """Load and validate the immutable handoff envelope metadata."""
    handoff = _json_object(handoff_path, "prediction_handoff")
    if not str(handoff.get("run_id") or "").strip():
        raise CriticContractError("handoff_run_id_missing", "handoff has no run_id")
    if not str(handoff.get("pipeline_version") or "").strip():
        raise CriticContractError(
            "handoff_pipeline_version_missing", "handoff has no pipeline_version"
        )
    required_targets = handoff.get("required_targets")
    if (
        not isinstance(required_targets, list)
        or not required_targets
        or any(not str(value).strip() for value in required_targets)
    ):
        raise CriticContractError(
            "handoff_targets_invalid", "handoff required_targets must be non-empty"
        )
    categories = handoff.get("categories")
    if not isinstance(categories, dict):
        raise CriticContractError(
            "handoff_categories_invalid", "handoff categories must be an object"
        )
    unknown = sorted(set(categories) - ALLOWED_STATUSES)
    if unknown:
        raise CriticContractError(
            "handoff_status_unknown", f"unknown handoff categories: {unknown}"
        )
    return handoff


def _load_handoff_records(handoff: dict, handoff_path: Path) -> list[dict]:
    """Load every handoff record, verifying its declared hash and identity."""
    categories = handoff.get("categories")
    records: list[dict] = []
    seen: set[str] = set()
    for status in sorted(categories):
        entries = categories[status]
        if not isinstance(entries, list):
            raise CriticContractError(
                "handoff_category_type", f"handoff category {status} must be a list"
            )
        for entry in entries:
            if not isinstance(entry, dict):
                raise CriticContractError(
                    "handoff_entry_type", f"handoff category {status} has a non-object"
                )
            candidate_id = str(entry.get("candidate_id") or "").strip()
            if not candidate_id or candidate_id in seen:
                raise CriticContractError(
                    "handoff_candidate_duplicate",
                    f"missing or duplicate candidate in handoff: {candidate_id!r}",
                )
            seen.add(candidate_id)
            record_path = _resolve_path(
                entry.get("record_path"), handoff_path.parent, "record"
            )
            declared_sha = str(entry.get("record_sha256") or "").strip().lower()
            if len(declared_sha) != 64:
                raise CriticContractError(
                    "record_hash_missing", f"{candidate_id} has no full record SHA-256"
                )
            observed_sha = file_sha256(record_path)
            if observed_sha != declared_sha:
                raise CriticContractError(
                    "record_hash_mismatch",
                    f"{candidate_id} record SHA-256 differs from handoff",
                )
            record = _json_object(record_path, "prediction_record")
            record_candidate = str(
                (record.get("candidate") or {}).get("candidate_id") or ""
            ).strip()
            if record_candidate != candidate_id:
                raise CriticContractError(
                    "record_candidate_mismatch",
                    f"handoff {candidate_id} points to record {record_candidate!r}",
                )
            if record.get("status") != status:
                raise CriticContractError(
                    "record_status_mismatch",
                    f"{candidate_id} handoff status {status!r} differs from record",
                )
            if record.get("run_id") != handoff["run_id"]:
                raise CriticContractError(
                    "record_run_mismatch", f"{candidate_id} belongs to another run"
                )
            if record.get("pipeline_version") != handoff["pipeline_version"]:
                raise CriticContractError(
                    "record_pipeline_mismatch",
                    f"{candidate_id} belongs to another pipeline version",
                )
            records.append({
                "candidate_id": candidate_id,
                "status": status,
                "path": record_path,
                "sha256": observed_sha,
                "record": record,
            })
    if not records:
        raise CriticContractError("handoff_empty", "handoff contains no candidates")
    return records


def _load_records(handoff_path: Path) -> tuple[dict, list[dict]]:
    handoff = _validate_handoff_envelope(handoff_path)
    records = _load_handoff_records(handoff, handoff_path)
    return handoff, records

def _route_summary(records: list[dict], rows_by_id: dict[str, dict]) -> dict:
    values: dict[str, dict] = {}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in records:
        row = rows_by_id.get(item["candidate_id"]) or {}
        grouped[str(row.get("source_route") or "unknown")].append(item)
    for route, items in sorted(grouped.items()):
        failed_layers = Counter()
        for item in items:
            battery = item["record"].get("battery") or {}
            failed_layers.update(battery.get("failed_layers") or [])
        values[route] = {
            "candidate_count": len(items),
            "status_counts": dict(sorted(Counter(
                item["status"] for item in items
            ).items())),
            "failed_layer_counts": dict(sorted(failed_layers.items())),
        }
    return values

def _row_snapshot(records: list[dict], rows_by_id: dict[str, dict]) -> list[dict]:
    """Snapshot the CandidateIndex row each record was reviewed against."""
    return [
        {
            "candidate_id": item["candidate_id"],
            "record_sequence": (
                item["record"].get("candidate") or {}
            ).get("sequence"),
            "candidate_index_present": item["candidate_id"] in rows_by_id,
            "candidate_index_sequence": (
                rows_by_id.get(item["candidate_id"]) or {}
            ).get("sequence"),
            "source_route": (rows_by_id.get(item["candidate_id"]) or {}).get(
                "source_route"
            ),
        }
        for item in sorted(records, key=lambda value: value["candidate_id"])
    ]
