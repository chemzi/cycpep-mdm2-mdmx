"""Prediction persistence boundary for legacy writes and typed proposals.

Scientific computation stays in :mod:`prediction_pipeline.pipeline`.  This
module only translates its completed records into either the compatibility
write APIs or transaction-managed mutation/evidence proposals.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import data_layer
from data_layer import CandidateIndex, EvidenceLogger, State

from .contracts import file_sha256
from .protocol import protocol_binding


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class PredictionPersistence:
    def __init__(
        self,
        *,
        run_id: str,
        required_targets: tuple[str, ...],
        candidate_ids: tuple[str, ...],
        thresholds_digest: str,
        defer_formal_writes: bool,
        artifact_id_prefix: str,
        launcher_correlation: dict[str, str] | None = None,
    ) -> None:
        self.run_id = run_id
        self.required_targets = required_targets
        self.candidate_ids = candidate_ids
        self.thresholds_digest = thresholds_digest
        self.deferred = defer_formal_writes
        self.artifact_id_prefix = artifact_id_prefix
        self.launcher_correlation = (
            dict(launcher_correlation) if launcher_correlation is not None else None
        )
        self.candidate_patches: list[dict] = []
        self.state_updates: dict[str, Any] = {}
        self.state_appends: list[dict] = []
        self.evidence_events: list[dict] = []
        self.record_artifacts: list[dict] = []

    def record_artifact_id(self, candidate_id: str) -> str:
        return f"{self.artifact_id_prefix}-prediction-record-{candidate_id}"

    def handoff_artifact_id(self) -> str:
        return f"{self.artifact_id_prefix}-prediction_handoff"

    def record_reference(self, candidate_id: str, path: Path, sha256: str) -> dict:
        if self.deferred:
            return {"record_artifact_id": self.record_artifact_id(candidate_id)}
        return {"record_path": str(path), "record_sha256": sha256}

    def prediction_metadata(
        self,
        *,
        candidate_id: str,
        record_path: Path,
        record: dict,
        input_digest: str,
        evidence_status: str,
    ) -> dict:
        value = {
            "schema_version": record["schema_version"],
            "pipeline_version": record["pipeline_version"],
            "run_id": self.run_id,
            "record_sha256": record["record_sha256"],
            "input_digest": input_digest,
            "artifact_digest": record["cache_key"]["artifact_digest"],
            "evidence_status": evidence_status,
            "issues": record["issues"],
            "protocol_identity": protocol_binding(),
        }
        if self.deferred:
            value["record_artifact_id"] = self.record_artifact_id(candidate_id)
        else:
            value["record_path"] = str(record_path)
        return value

    def record_label(self, candidate_id: str, record_path: Path) -> str:
        return (
            self.record_artifact_id(candidate_id)
            if self.deferred else str(record_path)
        )

    def record_event(
        self,
        event_type: str,
        payload: dict,
        *,
        candidate_id: str | None = None,
    ) -> None:
        if not self.deferred:
            EvidenceLogger.log(
                "prediction",
                event_type,
                payload,
                targets=list(self.required_targets),
                phase="evaluate",
            )
            return
        payload = dict(payload)
        if "run_id" in payload:
            payload.pop("run_id")
            payload["prediction_run_id"] = self.run_id
        event = {
            "agent": "prediction",
            "event_type": event_type,
            "phase": "evaluate",
            "targets": list(self.required_targets),
            **payload,
        }
        if candidate_id:
            event["candidate_id"] = candidate_id
        self.evidence_events.append(event)

    def record_battery_evaluated(self, candidate_snapshot: dict, battery: dict) -> None:
        """Record the structured seven-layer verdict for the experience loop.

        Mirrors ``EvidenceLogger.battery_evaluated`` but flows through the
        transaction boundary (PR41) so the event is committed atomically with
        the prediction record instead of being dropped in transaction mode.
        """
        sequence = str(candidate_snapshot.get("sequence") or "")
        payload = {
            "candidate_id": candidate_snapshot.get("candidate_id"),
            "sequence": sequence,
            "length": len(sequence) if sequence else None,
            "route": candidate_snapshot.get("source_route"),
            "passed": bool(battery.get("all_layers_pass")),
            "competition_clearance": bool(battery.get("competition_clearance")),
            "failed_layers": battery.get("failed_layers") or [],
            "hard_failures": battery.get("hard_failures") or [],
            "missing_thresholds": battery.get("missing_thresholds") or [],
            "triage_status": battery.get("triage_status"),
            "layer_values": battery.get("layer_values") or {},
            "target_pass": battery.get("target_pass") or {},
            "protocol_identity": protocol_binding(),
            "thresholds_digest": self.thresholds_digest,
        }
        self.record_event(
            "battery_evaluated",
            payload,
            candidate_id=candidate_snapshot.get("candidate_id"),
        )

    def record_scoring_events(
        self,
        *,
        candidate_id: str,
        record_path: Path,
        record: dict,
        metrics: dict,
        battery: dict,
        tool_trace: dict,
        layer_keys: tuple[str, ...],
    ) -> None:
        if self.deferred:
            tool_trace = dict(tool_trace)
            tool_trace.pop("output_path", None)
            tool_trace["output_artifact_id"] = self.record_artifact_id(candidate_id)
        reference = self.record_reference(
            candidate_id, record_path, record["record_sha256"]
        )
        for layer, pass_key in enumerate(layer_keys, start=1):
            self.record_event(
                "candidate_scored",
                {
                    "layer": layer,
                    "scores": {"metrics": metrics},
                    "tool_trace": tool_trace,
                    "passed": bool(battery[pass_key]),
                    "protocol_identity": protocol_binding(),
                    **reference,
                },
                candidate_id=candidate_id,
            )
        self.record_event(
            "prediction_recorded",
            {
                "prediction_status": record["status"],
                **reference,
                "issues": record["issues"],
                "protocol_identity": protocol_binding(),
            },
            candidate_id=candidate_id,
        )
        if record["status"] == "finalized":
            self.record_event(
                "candidate_finalized",
                {**reference, "protocol_identity": protocol_binding()},
                candidate_id=candidate_id,
            )

    def record_invalid_event(
        self, candidate_id: str, record_path: Path, record: dict
    ) -> None:
        self.record_event(
            "prediction_recorded",
            {
                "prediction_status": "invalid",
                **self.record_reference(
                    candidate_id, record_path, record["record_sha256"]
                ),
                "issues": record["issues"],
                "protocol_identity": protocol_binding(),
            },
            candidate_id=candidate_id if candidate_id != "unknown" else None,
        )

    def record_run_started(
        self,
        *,
        pipeline_version: str,
        run_dir: Path,
        candidate_count: int,
        config_digest: str,
    ) -> None:
        payload = {
            "run_id": self.run_id,
            "pipeline_version": pipeline_version,
            "candidate_count": candidate_count,
            "config_digest": config_digest,
            "protocol_identity": protocol_binding(),
        }
        if not self.deferred:
            payload["run_dir"] = str(run_dir)
        self.record_event("prediction_run_started", payload)

    def category_entry(self, record: dict, record_path: Path) -> dict:
        value = {
            "candidate_id": record["candidate"]["candidate_id"],
            "sequence": record["candidate"].get("sequence"),
            "record_path": str(record_path),
            "record_sha256": record.get("record_sha256"),
            "issues": record.get("issues", []),
        }
        if self.deferred:
            value["record_artifact_id"] = self.record_artifact_id(
                record["candidate"]["candidate_id"]
            )
        return value

    def record_handoff_ready(self, summary: dict, handoff_path: Path) -> None:
        reference = (
            {"handoff_artifact_id": self.handoff_artifact_id()}
            if self.deferred
            else {
                "handoff_path": str(handoff_path),
                "handoff_sha256": file_sha256(handoff_path),
            }
        )
        correlation = self.launcher_correlation or {}
        self.record_event(
            "prediction_handoff_ready",
            {
                "run_id": self.run_id,
                "status_counts": summary["status_counts"],
                "protocol_identity": protocol_binding(),
                "candidate_ids": list(self.candidate_ids),
                "thresholds_digest": self.thresholds_digest,
                **reference,
                **correlation,
            },
        )

    @staticmethod
    def _score_patch(scores: dict) -> dict:
        patch: dict[str, Any] = {}
        for key, value in scores.items():
            if key == "threshold_audit" and isinstance(value, dict):
                patch["threshold_audit_json"] = json.dumps(
                    value, ensure_ascii=False, separators=(",", ":")
                )
            elif key in data_layer.INDEX_COLUMNS:
                patch[key] = value if isinstance(value, str) else str(value)
        patch["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        return patch

    def persist_candidate(
        self,
        candidate_id: str,
        scores: dict,
        *,
        status: str,
        notes: str,
    ) -> None:
        if not self.deferred:
            CandidateIndex.update_score(candidate_id, scores)
            CandidateIndex.update_status(candidate_id, status, notes=notes)
            return
        patch = self._score_patch(scores)
        patch.update({"final_status": status, "notes": notes})
        self.candidate_patches.append({
            "candidate_id": candidate_id,
            "patch": patch,
        })

    def remember_record(self, candidate_id: str, path: Path) -> None:
        if self.deferred:
            self.record_artifacts.append({
                "candidate_id": candidate_id,
                "artifact_id": self.record_artifact_id(candidate_id),
                "path": str(path),
            })

    def persist_state(self, summary: dict, handoff_path: Path) -> None:
        if not self.deferred:
            updated_state = State.update({
                "phase": "evaluate",
                "prediction": summary,
            })
            if not any(
                entry.get("agent") == "prediction"
                and (entry.get("summary") or {}).get("run_id") == self.run_id
                for entry in updated_state.get("iteration_history", [])
            ):
                State.append_history({
                    "phase": "evaluate",
                    "agent": "prediction",
                    "timestamp": _utcnow(),
                    "summary": summary,
                })
            return

        state_summary = dict(summary)
        state_summary.pop("run_dir", None)
        state_summary.pop("handoff_path", None)
        state_summary.update({
            "handoff_artifact_id": self.handoff_artifact_id(),
            "record_artifacts": {
                item["candidate_id"]: {"artifact_id": item["artifact_id"]}
                for item in self.record_artifacts
            },
        })
        self.state_updates = {"phase": "evaluate", "prediction": state_summary}
        self.state_appends = [{
            "kind": "append_if_absent",
            "key": "iteration_history",
            "item": {
                "phase": "evaluate",
                "agent": "prediction",
                "timestamp": _utcnow(),
                "summary": state_summary,
            },
            "identity_path": ["summary", "run_id"],
            "identity_value": self.run_id,
        }]

    def effects(self, handoff_path: Path) -> dict:
        if not self.deferred:
            raise RuntimeError("transaction effects require deferred persistence")
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "protocol_identity": protocol_binding(),
            "candidate_patches": list(self.candidate_patches),
            "state_updates": dict(self.state_updates),
            "state_appends": list(self.state_appends),
            "evidence_events": list(self.evidence_events),
            "record_artifacts": list(self.record_artifacts),
            "handoff_artifact": {
                "artifact_id": self.handoff_artifact_id(),
                "path": str(handoff_path),
            },
        }
