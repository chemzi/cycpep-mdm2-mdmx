"""Prediction-owned Launcher correlation and recovery contract tests."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import data_layer
from agents import prediction
from prediction_pipeline import PredictionConfig, PredictionPipeline
from prediction_pipeline.contracts import (
    CRITIC_READY_STATUSES,
    ContractError,
    file_sha256,
    prediction_status_from_battery,
)
from prediction_pipeline.transaction_effects import PredictionPersistence

from _prediction_test_utils import project_config


UUID_PAYLOAD = "11111111111141118111111111111111"
LAUNCHER_ID = f"launcher_{UUID_PAYLOAD}"


class PredictionLauncherContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.project = project_config(("MDM2",))
        self.run_root = self.root / "original-runs"
        self.artifacts_root = self.root / "artifacts"
        self.correlation = prediction.PredictionCorrelation.for_launcher(
            launcher_run_id=LAUNCHER_ID,
            project_id=self.project["project_id"],
            approved_content_binding=self.project["review"]["approved_digest"],
        )
        self.rows = [{
            "candidate_id": "C0001",
            "sequence": "ACDEFGHI",
            "source_route": "test",
            "manifest_path": str(self.root / "design" / "manifest.json"),
        }]
        self.state = {
            "project_id": self.project["project_id"],
            "project_config": self.project,
            "thresholds": {},
        }
        self._patches = [
            patch.object(data_layer, "ACTIVE_PROJECT_CONFIG", self.project),
            patch.object(data_layer, "SQLITE_DB_PATH", self.root / "store.db"),
            patch.object(data_layer, "DATA_DIR", self.root / "data"),
            patch.object(data_layer, "EVIDENCE_DIR", self.root / "evidence"),
            patch.object(data_layer, "STATE_PATH", self.root / "data" / "state.json"),
            patch.object(data_layer, "LOG_PATH", self.root / "evidence" / "evidence_log.jsonl"),
            patch.object(data_layer, "INDEX_PATH", self.root / "data" / "candidate_index.csv"),
            patch.object(prediction.CandidateIndex, "load", return_value=self.rows),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self):
        for item in reversed(self._patches):
            item.stop()
        self._tmp.cleanup()

    def _run(self, *, resume=False):
        return prediction.run(
            state=self.state,
            artifacts_root=self.artifacts_root,
            run_root=self.run_root,
            config=PredictionConfig(),
            run_id=self.correlation.prediction_run_id,
            resume=resume,
            project_config=self.project,
            correlation=self.correlation,
        )

    def _materialize_owner_status(self, status, battery=None):
        assignments = (
            status
            if isinstance(status, dict)
            else {"C0001": (status, battery)}
        )
        with patch.object(
            PredictionPersistence, "record_handoff_ready", autospec=True
        ):
            self._run()
        run_dir = self.run_root / self.correlation.prediction_run_id
        categories = {}
        record_paths = {}
        for candidate_id, (record_status, record_battery) in assignments.items():
            record_path = run_dir / "records" / f"{candidate_id}.json"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["status"] = record_status
            record["battery"] = record_battery
            record_path.write_text(
                json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            categories.setdefault(record_status, []).append({
                "candidate_id": candidate_id,
                "sequence": record["candidate"].get("sequence"),
                "record_path": str(record_path),
                "record_sha256": file_sha256(record_path),
                "issues": record.get("issues", []),
            })
            record_paths[candidate_id] = record_path
        handoff_path = run_dir / "prediction_handoff.json"
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
        handoff["categories"] = categories
        handoff_path.write_text(
            json.dumps(handoff, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        prediction.EvidenceLogger.log(
            "prediction",
            "prediction_handoff_ready",
            {
                "run_id": self.correlation.prediction_run_id,
                "status_counts": {
                    key: len(values) for key, values in categories.items()
                },
                "protocol_identity": handoff["protocol_identity"],
                "handoff_path": str(handoff_path),
                "handoff_sha256": file_sha256(handoff_path),
                **self.correlation.receipt_fields(),
            },
            phase="evaluate",
        )
        return record_paths, handoff_path

    def test_prediction_owner_status_and_readiness_contract_is_single_source(self):
        ready_battery = {
            "competition_clearance": False,
            "metric_clearance": False,
            "triage_status": "valid",
            "missing_evidence": [],
            "missing_thresholds": [],
        }
        pending_battery = {
            **ready_battery,
            "missing_evidence": ["L1"],
        }
        self.assertEqual(
            prediction_status_from_battery(ready_battery), "needs_optimization"
        )
        self.assertIn("needs_optimization", CRITIC_READY_STATUSES)
        self.assertEqual(
            prediction_status_from_battery(pending_battery), "prediction_pending"
        )
        self.assertNotIn("prediction_pending", CRITIC_READY_STATUSES)

    def test_public_run_root_resolver_preserves_configured_precedence(self):
        explicit_argument = self.root / "argument-runs"
        configured_root = self.root / "configured-runs"
        data_root = self.root / "np-data"
        with patch.dict(
            os.environ,
            {
                "CYCPEP_PREDICTION_ROOT": str(configured_root),
                "NP_DATA": str(data_root),
            },
            clear=True,
        ):
            self.assertEqual(
                prediction.resolve_prediction_run_root(explicit_argument),
                explicit_argument.resolve(),
            )
            self.assertEqual(
                prediction.resolve_prediction_run_root(), configured_root.resolve()
            )

        with patch.dict(os.environ, {"NP_DATA": str(data_root)}, clear=True):
            self.assertEqual(
                prediction.resolve_prediction_run_root(),
                (data_root / "prediction_runs").resolve(),
            )

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                prediction.resolve_prediction_run_root(),
                (prediction.ROOT / "data" / "prediction_runs").resolve(),
            )

        self.assertFalse(explicit_argument.exists())
        self.assertFalse(configured_root.exists())
        self.assertFalse(data_root.exists())

    def test_fixed_prediction_identities_are_reconstructable_and_distinct(self):
        self.assertEqual(
            self.correlation.prediction_invocation_id,
            f"prediction_invocation_{UUID_PAYLOAD}",
        )
        self.assertEqual(
            self.correlation.prediction_run_id,
            f"prediction_{UUID_PAYLOAD}",
        )
        self.assertNotEqual(
            self.correlation.prediction_invocation_id,
            self.correlation.prediction_run_id,
        )

    def test_launcher_identity_rejects_noncanonical_uuid_payloads(self):
        invalid_ids = (
            "launcher_11111111-1111-4111-8111-111111111111",
            "launcher_aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaA",
            "launcher_1111111111111111111111111111111",
        )
        for launcher_run_id in invalid_ids:
            with self.subTest(launcher_run_id=launcher_run_id):
                with self.assertRaisesRegex(ContractError, "canonical"):
                    prediction.PredictionCorrelation.for_launcher(
                        launcher_run_id=launcher_run_id,
                        project_id=self.project["project_id"],
                        approved_content_binding=self.project["review"]["approved_digest"],
                    )

    def test_direct_correlation_construction_cannot_break_fixed_mapping(self):
        with self.assertRaisesRegex(ContractError, "preserve"):
            prediction.PredictionCorrelation(
                prediction_invocation_id=f"prediction_invocation_{UUID_PAYLOAD}",
                prediction_run_id="prediction_unrelated",
                launcher_run_id=LAUNCHER_ID,
                project_id=self.project["project_id"],
                approved_content_binding=self.project["review"]["approved_digest"],
            )

    def test_start_receipt_is_durable_before_pipeline_side_effect(self):
        observed = {}

        def fake_pipeline_run(pipeline):
            starts = data_layer.get_storage_backend().query(
                project_id=self.project["project_id"],
                agent="prediction",
                event_type="prediction_invocation_started",
            )
            observed["starts"] = starts
            observed["run_dir_exists"] = pipeline.run_dir.exists()
            return {"run_id": pipeline.run_id, "status_counts": {}}

        with patch.object(PredictionPipeline, "run", autospec=True, side_effect=fake_pipeline_run):
            self._run()

        self.assertEqual(len(observed["starts"]), 1)
        start = observed["starts"][0]
        self.assertEqual(start["prediction_run_locator"]["root"], str(self.run_root.resolve()))
        self.assertEqual(start["prediction_run_locator"]["run_id"], self.correlation.prediction_run_id)
        self.assertFalse(observed["run_dir_exists"])

    def test_start_receipt_persistence_failure_prevents_prediction(self):
        invoked = []
        with patch.object(
            prediction.EvidenceLogger,
            "log",
            side_effect=OSError("store unavailable"),
        ), patch.object(
            PredictionPipeline,
            "run",
            autospec=True,
            side_effect=lambda pipeline: invoked.append(pipeline),
        ):
            with self.assertRaises(OSError):
                self._run()
        self.assertEqual(invoked, [])
        self.assertFalse(self.run_root.exists())

    def test_start_receipt_projection_failure_prevents_prediction_after_durable_append(self):
        invoked = []
        with patch.object(
            data_layer,
            "_project_evidence",
            side_effect=OSError("projection unavailable"),
        ), patch.object(
            PredictionPipeline,
            "run",
            autospec=True,
            side_effect=lambda pipeline: invoked.append(pipeline),
        ):
            with self.assertRaises(OSError):
                self._run()

        self.assertEqual(invoked, [])
        starts = data_layer.get_storage_backend().query(
            project_id=self.project["project_id"],
            agent="prediction",
            event_type="prediction_invocation_started",
        )
        self.assertEqual(len(starts), 1)
        recovery = prediction.validate_prediction_invocation(
            self.correlation,
            store=data_layer.get_storage_backend(),
        )
        self.assertEqual(recovery.status, "started_without_completion")
        self.assertEqual(recovery.blocker_code, "prediction_recovery_ambiguous")

    def test_non_ready_invocation_is_recovered_from_original_receipt_locator(self):
        self._run()
        changed_root = self.root / "ambient-other-runs"
        with patch.dict(os.environ, {"CYCPEP_PREDICTION_ROOT": str(changed_root)}):
            first = prediction.validate_prediction_invocation(
                self.correlation,
                store=data_layer.get_storage_backend(),
            )
            second = prediction.validate_prediction_invocation(
                self.correlation,
                store=data_layer.get_storage_backend(),
            )

        self.assertEqual(first.status, "blocked")
        self.assertEqual(first.blocker_code, "prediction_execution_incomplete")
        self.assertEqual(first, second)
        self.assertEqual(first.run_root, self.run_root.resolve())
        self.assertFalse(changed_root.exists())

    def test_owner_ready_status_with_complete_battery_advances(self):
        self._materialize_owner_status("needs_optimization", {
            "competition_clearance": False,
            "metric_clearance": False,
            "triage_status": "valid",
            "missing_evidence": [],
            "missing_thresholds": [],
        })
        result = prediction.validate_prediction_invocation(
            self.correlation, store=data_layer.get_storage_backend()
        )
        self.assertEqual(result.status, "completed")

    def test_pending_status_blocks_before_critic(self):
        self._materialize_owner_status("prediction_pending", {
            "competition_clearance": False,
            "metric_clearance": False,
            "triage_status": "valid",
            "missing_evidence": ["L1"],
            "missing_thresholds": [],
        })
        result = prediction.validate_prediction_invocation(
            self.correlation, store=data_layer.get_storage_backend()
        )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.blocker_code, "prediction_execution_incomplete")

    def test_non_ready_terminal_status_blocks_before_critic(self):
        self._materialize_owner_status("invalid", None)
        result = prediction.validate_prediction_invocation(
            self.correlation, store=data_layer.get_storage_backend()
        )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.blocker_code, "prediction_execution_incomplete")

    def test_declared_ready_status_with_missing_evidence_is_not_completion(self):
        self._materialize_owner_status("finalized", {
            "competition_clearance": False,
            "metric_clearance": False,
            "triage_status": "valid",
            "missing_evidence": ["L1"],
            "missing_thresholds": [],
        })
        result = prediction.validate_prediction_invocation(
            self.correlation, store=data_layer.get_storage_backend()
        )
        self.assertEqual(result.status, "started_without_completion")
        self.assertEqual(result.blocker_code, "prediction_recovery_ambiguous")

    def test_mixed_ready_and_pending_records_block_as_scientifically_incomplete(self):
        self.rows.append({
            "candidate_id": "C0002",
            "sequence": "ACDEFGHK",
            "source_route": "test",
            "manifest_path": str(self.root / "design" / "manifest-2.json"),
        })
        ready = {
            "competition_clearance": False,
            "metric_clearance": False,
            "triage_status": "valid",
            "missing_evidence": [],
            "missing_thresholds": [],
        }
        pending = {**ready, "missing_evidence": ["L2"]}
        self._materialize_owner_status({
            "C0001": ("needs_optimization", ready),
            "C0002": ("prediction_pending", pending),
        })
        result = prediction.validate_prediction_invocation(
            self.correlation, store=data_layer.get_storage_backend()
        )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.blocker_code, "prediction_execution_incomplete")

    def test_candidate_coverage_and_record_integrity_contradictions_are_ambiguous(self):
        self.rows.append({
            "candidate_id": "C0002",
            "sequence": "ACDEFGHK",
            "source_route": "test",
            "manifest_path": str(self.root / "design" / "manifest-2.json"),
        })
        ready = {
            "competition_clearance": False,
            "metric_clearance": False,
            "triage_status": "valid",
            "missing_evidence": [],
            "missing_thresholds": [],
        }
        paths, _ = self._materialize_owner_status({
            "C0001": ("needs_optimization", ready),
        })
        missing = prediction.validate_prediction_invocation(
            self.correlation, store=data_layer.get_storage_backend()
        )
        self.assertEqual(missing.status, "started_without_completion")

        paths["C0001"].write_text("{}", encoding="utf-8")
        tampered = prediction.validate_prediction_invocation(
            self.correlation, store=data_layer.get_storage_backend()
        )
        self.assertEqual(tampered.status, "started_without_completion")
        self.assertEqual(tampered.blocker_code, "prediction_recovery_ambiguous")

    def test_started_without_coherent_completion_fails_closed(self):
        with patch.object(PredictionPipeline, "run", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self._run()
        result = prediction.validate_prediction_invocation(
            self.correlation,
            store=data_layer.get_storage_backend(),
        )
        self.assertEqual(result.status, "started_without_completion")
        self.assertEqual(result.blocker_code, "prediction_recovery_ambiguous")

    def test_tampered_run_manifest_fails_exact_validation(self):
        self._run()
        manifest_path = (
            self.run_root / self.correlation.prediction_run_id / "run_manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["required_targets"] = ["OTHER"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result = prediction.validate_prediction_invocation(
            self.correlation,
            store=data_layer.get_storage_backend(),
        )
        self.assertEqual(result.status, "started_without_completion")
        self.assertEqual(result.blocker_code, "prediction_recovery_ambiguous")

    def test_tampered_input_snapshot_fails_exact_validation(self):
        self._run()
        rows_path = (
            self.run_root
            / self.correlation.prediction_run_id
            / "inputs"
            / "candidate_rows.json"
        )
        rows = json.loads(rows_path.read_text(encoding="utf-8"))
        rows[0]["sequence"] = "AAAAAAAA"
        rows_path.write_text(json.dumps(rows), encoding="utf-8")

        result = prediction.validate_prediction_invocation(
            self.correlation,
            store=data_layer.get_storage_backend(),
        )
        self.assertEqual(result.status, "started_without_completion")
        self.assertEqual(result.blocker_code, "prediction_recovery_ambiguous")

    def test_conflicting_completion_correlation_fails_closed(self):
        self._run()
        store = data_layer.get_storage_backend()
        completion = store.query(
            project_id=self.project["project_id"],
            agent="prediction",
            event_type="prediction_handoff_ready",
        )[0]
        conflicting = {
            key: value for key, value in completion.items()
            if key not in {"event_id", "timestamp", "agent", "event_type", "phase"}
        }
        conflicting["approved_content_binding"] = "wrong-approved-content"
        prediction.EvidenceLogger.log(
            "prediction",
            "prediction_handoff_ready",
            conflicting,
            phase="evaluate",
        )

        result = prediction.validate_prediction_invocation(
            self.correlation,
            store=store,
        )
        self.assertEqual(result.status, "started_without_completion")
        self.assertEqual(result.blocker_code, "prediction_recovery_ambiguous")

    def test_conflicting_start_receipts_fail_closed(self):
        with patch.object(PredictionPipeline, "run", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self._run()
        start = data_layer.get_storage_backend().query(
            agent="prediction", event_type="prediction_invocation_started"
        )[0]
        duplicate_payload = {
            key: value for key, value in start.items()
            if key not in {"event_id", "timestamp", "agent", "event_type", "phase"}
        }
        prediction.EvidenceLogger.log(
            "prediction", "prediction_invocation_started", duplicate_payload,
            phase="evaluate",
        )
        result = prediction.validate_prediction_invocation(
            self.correlation,
            store=data_layer.get_storage_backend(),
        )
        self.assertEqual(result.status, "conflicting")
        self.assertEqual(result.blocker_code, "prediction_correlation_conflict")

    def test_launcher_correlation_is_persisted_in_formal_outputs(self):
        self._run()
        run_dir = self.run_root / self.correlation.prediction_run_id
        manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        handoff = json.loads((run_dir / "prediction_handoff.json").read_text(encoding="utf-8"))
        completions = data_layer.get_storage_backend().query(
            agent="prediction", event_type="prediction_handoff_ready"
        )
        self.assertEqual(manifest["prediction_invocation_id"], self.correlation.prediction_invocation_id)
        self.assertEqual(handoff["prediction_invocation_id"], self.correlation.prediction_invocation_id)
        self.assertEqual(completions[0]["prediction_invocation_id"], self.correlation.prediction_invocation_id)

    def test_legacy_manifest_omits_launcher_fields_and_strict_resume_still_works(self):
        pipeline = PredictionPipeline(
            candidate_rows=self.rows,
            project=self.project,
            thresholds={},
            artifacts_root=self.artifacts_root,
            run_root=self.root / "legacy-runs",
            config=PredictionConfig(),
            run_id="legacy_prediction",
        )
        pipeline.run()
        manifest_path = self.root / "legacy-runs" / "legacy_prediction" / "run_manifest.json"
        observed = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertNotIn("prediction_invocation_id", observed)
        self.assertNotIn("launcher_run_id", observed)
        self.assertNotIn("approved_content_binding", observed)
        self.assertNotIn("prediction_run_id", observed)
        self.assertFalse(any(value is None for value in observed.values()))

        resumed = PredictionPipeline(
            candidate_rows=self.rows,
            project=self.project,
            thresholds={},
            artifacts_root=self.artifacts_root,
            run_root=self.root / "legacy-runs",
            config=PredictionConfig(),
            run_id="legacy_prediction",
            resume=True,
        ).run()
        self.assertEqual(resumed["run_id"], "legacy_prediction")

    def test_raw_threshold_digest_receipt_remains_recoverable(self):
        raw_thresholds = {
            "L4_ring_closure": {"value": 3.0, "operator": "<="}
        }
        self.state["thresholds"] = raw_thresholds
        self._run()

        result = prediction.validate_prediction_invocation(
            self.correlation,
            store=data_layer.get_storage_backend(),
        )
        self.assertNotEqual(result.status, "started_without_completion")


if __name__ == "__main__":
    unittest.main()
