"""Read-only Candidate scientific projection from committed Store authority."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


ArtifactBytesReader = Callable[[str | Path], bytes]


@dataclass(frozen=True)
class CandidateScienceProjection:
    candidates: tuple[dict[str, Any], ...]
    artifact_candidates: Mapping[str, tuple[str, ...]]
    artifact_roles: Mapping[str, str]


def _limitation(code: str, summary: str) -> dict[str, str]:
    return {"code": code, "summary": summary}


def _safe_content_link(value: Mapping[str, Any]) -> str | None:
    link = value.get("content_link")
    if (
        isinstance(link, str)
        and link.startswith("/api/")
        and ".." not in link
        and "\\" not in link
    ):
        return link
    return None


def _browser_metrics(value: Mapping[str, Any]) -> dict[str, Any]:
    """Remove formally known internal locator fields without inspecting values."""
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key == "path" or key.endswith("_path"):
            continue
        if isinstance(item, Mapping):
            result[key] = _browser_metrics(item)
        elif isinstance(item, list):
            result[key] = [
                _browser_metrics(entry) if isinstance(entry, Mapping) else entry
                for entry in item
            ]
        else:
            result[key] = item
    return result


def _shortlist_index(
    evidence: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    allowed = (
        "candidate_id", "passed", "reason", "desirability", "pareto_front",
        "top_margin_metric",
    )
    for event in evidence:
        if event.get("event_type") != "exploration_shortlist":
            continue
        entries = event.get("shortlist")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            candidate_id = str(entry.get("candidate_id") or "")
            if not candidate_id:
                continue
            item = {
                key: entry[key]
                for key in allowed
                if key in entry
            }
            if event.get("event_id") is not None:
                item["event_id"] = event["event_id"]
            result[candidate_id].append(item)
    return result


class _ProjectionBuilder:
    def __init__(
        self,
        *,
        evidence: Sequence[Mapping[str, Any]],
        artifacts: Sequence[Mapping[str, Any]],
        transactions: Sequence[Mapping[str, Any]],
        current_run_id: str | None,
        artifact_bytes_reader: ArtifactBytesReader,
    ) -> None:
        self.evidence = evidence
        self.current_run_id = current_run_id
        self.artifact_bytes_reader = artifact_bytes_reader
        self.transactions = {
            str(item.get("transaction_id")): item
            for item in transactions
            if item.get("transaction_id")
        }
        self.artifacts = {
            str(item.get("artifact_id")): item
            for item in artifacts
            if item.get("artifact_id")
        }
        self.transaction_artifacts = {
            transaction_id: {
                str(artifact_id)
                for artifact_id in transaction.get("artifact_ids") or ()
            }
            for transaction_id, transaction in self.transactions.items()
        }
        self.evidence_by_candidate: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for item in evidence:
            candidate_id = str(item.get("candidate_id") or "")
            if candidate_id:
                self.evidence_by_candidate[candidate_id].append(item)
        self.shortlists = _shortlist_index(evidence)
        self.bytes_cache: dict[str, bytes | None] = {}
        self.digest_cache: dict[str, str | None] = {}
        self.artifact_candidates: dict[str, set[str]] = defaultdict(set)
        self.artifact_roles: dict[str, str] = {}

    def _artifact_bytes(self, artifact_id: str) -> bytes | None:
        if artifact_id in self.bytes_cache:
            return self.bytes_cache[artifact_id]
        artifact = self.artifacts.get(artifact_id)
        try:
            content = self.artifact_bytes_reader(artifact["path"]) if artifact else None
        except (KeyError, OSError, TypeError, ValueError):
            content = None
        self.bytes_cache[artifact_id] = content
        return content

    def _verified_artifact(
        self,
        artifact_id: str,
        *,
        transaction_id: str,
        declared_sha256: str | None = None,
        verify_bytes: bool = False,
    ) -> Mapping[str, Any] | None:
        artifact = self.artifacts.get(artifact_id)
        transaction = self.transactions.get(transaction_id)
        if artifact is None or transaction is None:
            return None
        if artifact_id not in self.transaction_artifacts.get(transaction_id, set()):
            return None
        if artifact.get("transaction_id") != transaction_id:
            return None
        stored_sha = str(artifact.get("sha256") or "")
        if not stored_sha or (declared_sha256 and declared_sha256 != stored_sha):
            return None
        if not verify_bytes:
            return artifact
        if artifact_id not in self.digest_cache:
            content = self._artifact_bytes(artifact_id)
            self.digest_cache[artifact_id] = (
                hashlib.sha256(content).hexdigest() if content is not None else None
            )
        return artifact if self.digest_cache[artifact_id] == stored_sha else None

    def _status_event(self, candidate_id: str) -> Mapping[str, Any] | None:
        prediction_events = [
            item for item in self.evidence_by_candidate.get(candidate_id, ())
            if item.get("event_type") == "prediction_recorded"
        ]
        for event in reversed(prediction_events):
            transaction = self.transactions.get(str(event.get("transaction_id") or ""))
            if transaction and transaction.get("status") == "COMMITTED":
                return event
        return None

    @staticmethod
    def _event_matches_transaction(
        event: Mapping[str, Any], transaction: Mapping[str, Any]
    ) -> bool:
        return all(
            event.get(key) in (None, transaction.get(key))
            for key in ("workflow_id", "run_id", "task_id", "attempt_id")
        )

    def _structures(
        self,
        inventory: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        structures = []
        for item in inventory:
            artifact_id = str(item.get("artifact_id") or "")
            artifact = self.artifacts.get(artifact_id) or {}
            role = str(item.get("role") or "")
            artifact_type = str(artifact.get("artifact_type") or "")
            is_structure = artifact_type in {
                "design_pdb",
                "prediction_input:global.post_relax_pdb",
                "prediction_input:global.design_reference_pdb",
            } or (
                artifact_type.startswith("prediction_input:")
                and role.endswith(".pdb")
            )
            if not is_structure:
                continue
            descriptor: dict[str, Any] = {
                "artifact_id": artifact_id,
                "artifact_type": artifact_type,
            }
            if role:
                descriptor["role"] = role
            content_link = _safe_content_link(artifact)
            if content_link:
                descriptor["content_link"] = content_link
            structures.append(descriptor)
        return structures

    def _record_associations(
        self,
        candidate_id: str,
        event: Mapping[str, Any],
        limitations: list[dict[str, str]],
    ) -> tuple[list[str], list[dict[str, Any]], bool]:
        transaction_id = str(event.get("transaction_id") or "")
        record_id = str(event.get("record_artifact_id") or "")
        transaction = self.transactions.get(transaction_id)
        if transaction is None or not self._event_matches_transaction(event, transaction):
            limitations.append(_limitation(
                "prediction_transaction_unverified",
                "The committed Prediction transaction could not be verified.",
            ))
            return [], [], False
        record_artifact = self._verified_artifact(
            record_id, transaction_id=transaction_id, verify_bytes=True
        ) if record_id else None
        if record_artifact is None:
            limitations.append(_limitation(
                "prediction_record_unverified",
                "The committed Prediction record could not be verified.",
            ))
            return [], [], False
        record_bytes = self._artifact_bytes(record_id)
        try:
            record = json.loads(record_bytes.decode("utf-8")) if record_bytes else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            record = None
        record_candidate = (
            record.get("candidate") if isinstance(record, Mapping) else None
        )
        if not isinstance(record_candidate, Mapping) or record_candidate.get("candidate_id") != candidate_id:
            limitations.append(_limitation(
                "prediction_record_candidate_mismatch",
                "The committed Prediction record does not match this Candidate.",
            ))
            return [], [], False
        inventory = record.get("artifact_inventory")
        if not isinstance(inventory, list) or any(
            not isinstance(item, Mapping) for item in inventory
        ):
            limitations.append(_limitation(
                "prediction_inventory_unverified",
                "The committed Prediction Artifact inventory could not be verified.",
            ))
            return [], [], False
        verified_inventory: list[Mapping[str, Any]] = []
        for item in inventory:
            artifact_id = str(item.get("artifact_id") or "")
            declared_sha = str(item.get("sha256") or "")
            artifact = self._verified_artifact(
                artifact_id,
                transaction_id=transaction_id,
                declared_sha256=declared_sha,
            ) if artifact_id and declared_sha else None
            if artifact is None:
                limitations.append(_limitation(
                    "prediction_inventory_unverified",
                    "The committed Prediction Artifact inventory could not be verified.",
                ))
                return [], [], False
            verified_inventory.append(item)
        artifact_ids = list(dict.fromkeys([
            record_id,
            *(str(item["artifact_id"]) for item in verified_inventory),
        ]))
        for item in verified_inventory:
            artifact_id = str(item["artifact_id"])
            role = str(item.get("role") or "")
            self.artifact_candidates[artifact_id].add(candidate_id)
            if role:
                self.artifact_roles.setdefault(artifact_id, role)
        self.artifact_candidates[record_id].add(candidate_id)
        return artifact_ids, self._structures(verified_inventory), True

    def candidate(self, value: Mapping[str, Any]) -> dict[str, Any]:
        candidate = dict(value)
        candidate_id = str(candidate.get("candidate_id") or "")
        limitations: list[dict[str, str]] = []
        metrics = candidate.get("metrics")
        if isinstance(metrics, Mapping):
            candidate["metrics"] = _browser_metrics(metrics)
        else:
            encoded = candidate.get("metrics_json")
            if isinstance(encoded, Mapping):
                candidate["metrics"] = _browser_metrics(encoded)
            elif isinstance(encoded, str) and encoded:
                try:
                    decoded = json.loads(encoded)
                except json.JSONDecodeError:
                    decoded = None
                if isinstance(decoded, Mapping):
                    candidate["metrics"] = _browser_metrics(decoded)
                else:
                    limitations.append(_limitation(
                        "candidate_metrics_malformed",
                        "Candidate metrics are present but could not be read.",
                    ))
        status = str(candidate.get("status") or candidate.get("final_status") or "").strip()
        if status:
            candidate["status"] = status

        event = self._status_event(candidate_id)
        prediction_events = [
            item for item in self.evidence_by_candidate.get(candidate_id, ())
            if item.get("event_type") == "prediction_recorded"
        ]
        status_owner = None
        artifact_ids: list[str] = []
        structures: list[dict[str, Any]] = []
        if event is not None:
            artifact_ids, structures, owner_verified = self._record_associations(
                candidate_id, event, limitations
            )
            if owner_verified:
                run_id = str(event.get("run_id") or "")
                if run_id:
                    relation = (
                        "current_run" if self.current_run_id == run_id
                        else "historical_run"
                    )
                    candidate["run_id"] = run_id
                    status_owner = {"run_id": run_id, "run_relation": relation}
        elif prediction_events:
            limitations.append(_limitation(
                "prediction_transaction_unverified",
                "No committed Prediction transaction could be verified for this Candidate.",
            ))

        associations: dict[str, Any] = {
            "evidence_total": len(self.evidence_by_candidate.get(candidate_id, ())),
            "artifact_total": len(artifact_ids),
            "artifact_ids": artifact_ids,
            "complete": not limitations,
            "limitations": limitations,
            "structures": structures,
            "shortlist": list(self.shortlists.get(candidate_id, ())),
        }
        if status_owner is not None:
            associations["status_owner"] = status_owner
        candidate["associations"] = associations
        return candidate


def project_candidate_science(
    *,
    candidates: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    artifacts: Sequence[Mapping[str, Any]],
    transactions: Sequence[Mapping[str, Any]],
    current_run_id: str | None,
    artifact_bytes_reader: ArtifactBytesReader = lambda path: Path(path).read_bytes(),
) -> CandidateScienceProjection:
    """Project normalized browser fields without mutating formal Store records."""
    builder = _ProjectionBuilder(
        evidence=evidence,
        artifacts=artifacts,
        transactions=transactions,
        current_run_id=current_run_id,
        artifact_bytes_reader=artifact_bytes_reader,
    )
    projected = tuple(builder.candidate(value) for value in candidates)
    return CandidateScienceProjection(
        candidates=projected,
        artifact_candidates={
            key: tuple(sorted(value))
            for key, value in builder.artifact_candidates.items()
        },
        artifact_roles=dict(builder.artifact_roles),
    )


__all__ = ["CandidateScienceProjection", "project_candidate_science"]
