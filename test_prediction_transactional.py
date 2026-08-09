"""Prediction typed transaction boundary regression tests."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from contracts.trace import TraceContext
from contracts.transaction import TransactionContext, TransactionStatus
from execution.adapters import adapter_for
from execution.config import ExecutionConfig
from execution.contracts import ExecutionContractError
from execution.handlers import HandlerContext, evaluate_new_design_candidates
from execution.prediction_effects import load_prediction_transaction_effects
from execution.results import (
    CandidatePatchMutation,
    ExecutionActionResult,
    StateAppendMutation,
)
from execution.worker import ExecutionWorker, _validate_action_result
from prediction_pipeline.contracts import file_sha256
from prediction_pipeline.pipeline import PredictionPipeline
from prediction_pipeline.protocol import protocol_binding
from storage import SQLiteStore
from test_prediction_pipeline import SEQUENCE, project_config, write_monomer


class FailingCommitStore(SQLiteStore):
    def commit_transaction(self, **_kwargs):
        raise RuntimeError("injected commit failure")


class PredictionTransactionalTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="prediction-transaction-test-"))
        design = self.root / "design" / "C0001"
        design.mkdir(parents=True)
        refold = design / "refold.pdb"
        reference = design / "reference.pdb"
        write_monomer(refold)
        write_monomer(reference, bfactor=80.0)
        manifest = design / "manifest.json"
        manifest.write_text(json.dumps({
            "candidate_id": "C0001",
            "sequence": SEQUENCE,
            "length": len(SEQUENCE),
            "source_route": "test_route",
            "source_batch": "typed",
            "cyclization_type": "head-to-tail_amide",
            "refold_pdb": str(refold),
            "refold_pdb_hash": file_sha256(refold),
            "design_reference_pdb": str(reference),
            "design_reference_pdb_hash": file_sha256(reference),
            "design_reference_role": "rfdiffusion_target_bound_backbone",
            "backbone_pdb": str(reference),
        }), encoding="utf-8")
        self.row = {
            "candidate_id": "C0001",
            "sequence": SEQUENCE,
            "source_route": "test_route",
            "source_batch": "typed",
            "cyclization_type": "head-to-tail_amide",
            "manifest_path": str(manifest),
            "design_pdb_path": str(refold),
            "design_pdb_hash": file_sha256(refold),
            "final_status": "pending",
            "notes": "",
        }
        self.project = project_config(("MDM2",))
        self.state = {
            "project_id": "prediction_test",
            "phase": "design",
            "thresholds": {},
            "iteration_history": [],
            "project_config": self.project,
        }

    def _config(self, root: Path) -> ExecutionConfig:
        return ExecutionConfig(
            repo_root=Path(__file__).resolve().parent,
            execution_root=root / "execution",
            core_python=Path(sys.executable),
            design_python=Path(sys.executable),
            prediction_python=Path(sys.executable),
            prediction_artifacts_root=root / "prediction_artifacts",
            prediction_runs_root=root / "prediction_runs",
            colabdesign_dir=root,
            colabdesign_params=root,
            cuda_data_dir=root,
            boltz_executable=None,
            boltz_cache=None,
            boltz_checkpoint=None,
            prodigy_executable=None,
            pyrosetta_python=None,
            control_data_path=None,
        )

    def _packet(self) -> dict:
        return {
            "run_id": "run-prediction-typed",
            "task_attempt": 1,
            "task": {
                "task_id": "T001",
                "action": "evaluate_new_design_candidates",
                "phase": "evaluate",
                "parameters": {
                    "reuse_complete_evidence": False,
                    "evidence_mode": "reuse_or_generate_full",
                    "predictor_protocol": protocol_binding(),
                },
                "candidate_scope": {
                    "candidate_ids": ["C0001"],
                    "from_task_id": None,
                },
                "resource_request": {
                    "class": "gpu",
                    "proposal_count": 0,
                    "candidate_limit": 1,
                },
                "outputs": ["prediction_handoff.json"],
            },
            "trace_context": {"project_id": "prediction_test"},
        }

    @staticmethod
    def _typed_result(effects: dict, handoff: Path) -> ExecutionActionResult:
        return ExecutionActionResult(
            candidate_patches=tuple(
                CandidatePatchMutation(item["candidate_id"], item["patch"])
                for item in effects["candidate_patches"]
            ),
            state_updates=effects["state_updates"],
            state_appends=tuple(
                StateAppendMutation(
                    key=item["key"],
                    item=item["item"],
                    identity_path=tuple(item["identity_path"]),
                    identity_value=item["identity_value"],
                )
                for item in effects["state_appends"]
            ),
            evidence_events=tuple(effects["evidence_events"]),
            outputs=(("prediction_handoff", handoff),),
        )

    def _handler(self, *, tamper_record: bool = False, with_record_input: bool = False):
        def handler(context) -> ExecutionActionResult:
            run_id = f"prediction_{context.transaction_id[-12:]}"
            pipeline = PredictionPipeline(
                candidate_rows=[self.row],
                project=self.project,
                thresholds={},
                artifacts_root=self.root / "missing-artifacts",
                run_root=context.task_dir / "prediction-runs",
                candidate_ids=["C0001"],
                run_id=run_id,
                defer_formal_writes=True,
                artifact_id_prefix=context.transaction_id,
            )
            pipeline.run()
            if with_record_input:
                source = context.task_dir / "prediction-input.json"
                source.write_text('{"validated": true}', encoding="utf-8")
                record_path = Path(pipeline.persistence.record_artifacts[0]["path"])
                record = json.loads(record_path.read_text(encoding="utf-8"))
                record["artifact_inventory"] = [{
                    "role": "artifact_bundle", "path": str(source),
                    "sha256": file_sha256(source),
                }]
                record_path.write_text(json.dumps(record), encoding="utf-8")
                handoff = json.loads(pipeline.handoff_path.read_text(encoding="utf-8"))
                handoff["categories"]["prediction_pending"][0]["record_sha256"] = (
                    file_sha256(record_path)
                )
                pipeline.handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
            effects = pipeline.transaction_effects()
            if tamper_record:
                Path(effects["record_artifacts"][0]["path"]).write_text(
                    "{}", encoding="utf-8"
                )
            return self._typed_result(effects, pipeline.handoff_path)

        return handler

    def _agent_process(self, mutate_effects=None):
        def fake_process(argv, **_kwargs):
            from agents.prediction import run as run_prediction

            values = [str(item) for item in argv]
            candidates = [
                values[index + 1]
                for index, item in enumerate(values) if item == "--candidate"
            ]
            effects_path = values[values.index("--effects-output") + 1]
            run_prediction(
                artifacts_root=values[values.index("--artifacts-root") + 1],
                run_root=values[values.index("--run-root") + 1],
                candidate_ids=candidates,
                run_id=values[values.index("--run-id") + 1],
                effects_output=effects_path,
                transaction_id=values[values.index("--transaction-id") + 1],
            )
            if mutate_effects is not None:
                effects = json.loads(Path(effects_path).read_text(encoding="utf-8"))
                mutate_effects(effects)
                Path(effects_path).write_text(
                    json.dumps(effects, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
            return {"elapsed_seconds": 0.01}

        return fake_process

    def _run(
        self,
        root: Path,
        *,
        store_class=SQLiteStore,
        handler=None,
        attempt: int = 1,
    ):
        root.mkdir(parents=True, exist_ok=True)
        store = store_class(root / "store.db", project_id="prediction_test")
        store.replace_state("prediction_test", self.state)
        if store.get("C0001") is None:
            store.upsert(self.row, duplicate_policy="insert_only")
        context = TransactionContext.create(
            workflow_id="workflow-prediction",
            run_id="run-prediction-typed",
            task_id="T001",
            attempt_id=f"T001-A{attempt:02d}",
            action="evaluate_new_design_candidates",
            metadata={"project_id": "prediction_test"},
        )
        packet = self._packet()
        packet["task_attempt"] = attempt
        config = self._config(root)
        adapter = adapter_for(
            "evaluate_new_design_candidates",
            handler or self._handler(),
            packet,
            config,
            root / f"task-{attempt}",
            self.project,
        )
        worker = ExecutionWorker(
            store,
            root / "staging",
            config.execution_root / "artifacts",
        )
        trace = TraceContext(
            project_id="prediction_test",
            workflow_id="workflow-prediction",
            run_id="run-prediction-typed",
            task_id="T001",
            attempt_id=f"T001-A{attempt:02d}",
        )
        result = worker.run(
            context,
            adapter,
            validator=_validate_action_result,
            trace_context=trace,
        )
        return store, worker, context, result

    def test_candidate_patch_contract_has_only_candidate_id_and_patch(self):
        mutation = CandidatePatchMutation("C0001", {"final_status": "invalid"})
        self.assertEqual(
            mutation.to_dict(),
            {"candidate_id": "C0001", "patch": {"final_status": "invalid"}},
        )

    def test_pipeline_emits_proposals_without_direct_formal_writes(self):
        root = self.root / "proposal-only"
        pipeline = PredictionPipeline(
            candidate_rows=[self.row],
            project=self.project,
            thresholds={},
            artifacts_root=root / "missing-artifacts",
            run_root=root / "runs",
            run_id="prediction_proposal_only",
            defer_formal_writes=True,
            artifact_id_prefix="tx-proposal-only",
        )
        with patch(
            "prediction_pipeline.transaction_effects.State.update",
            side_effect=AssertionError("State.update called"),
        ), patch(
            "prediction_pipeline.transaction_effects.State.append_history",
            side_effect=AssertionError("State.append_history called"),
        ), patch(
            "prediction_pipeline.transaction_effects.CandidateIndex.update_score",
            side_effect=AssertionError("CandidateIndex.update_score called"),
        ), patch(
            "prediction_pipeline.transaction_effects.CandidateIndex.update_status",
            side_effect=AssertionError("CandidateIndex.update_status called"),
        ), patch(
            "prediction_pipeline.transaction_effects.EvidenceLogger.log",
            side_effect=AssertionError("EvidenceLogger.log called"),
        ):
            pipeline.run()
        effects = pipeline.transaction_effects()
        self.assertEqual([item["candidate_id"] for item in effects["candidate_patches"]], ["C0001"])
        prediction = effects["state_updates"]["prediction"]
        self.assertIn("handoff_artifact_id", prediction)
        self.assertNotIn("handoff_path", prediction)
        metrics = json.loads(effects["candidate_patches"][0]["patch"]["metrics_json"])
        self.assertIn("record_artifact_id", metrics["prediction"])
        self.assertNotIn("record_path", metrics["prediction"])
        for event in effects["evidence_events"]:
            self.assertEqual(event["protocol_identity"], protocol_binding())

    def test_transactional_prediction_emits_battery_evaluated_evidence(self):
        # P1-1: 事务模式（defer_formal_writes=True）也必须把 battery_evaluated
        # 收集进 effects，由 Execution 原子提交，而不是静默丢弃。
        root = self.root / "battery-tx"
        pipeline = PredictionPipeline(
            candidate_rows=[self.row],
            project=self.project,
            thresholds={},
            artifacts_root=root / "missing-artifacts",
            run_root=root / "runs",
            run_id="prediction_battery_tx",
            defer_formal_writes=True,
            artifact_id_prefix="tx-battery",
        )
        with patch(
            "prediction_pipeline.transaction_effects.State.update",
            side_effect=AssertionError("State.update called"),
        ), patch(
            "prediction_pipeline.transaction_effects.State.append_history",
            side_effect=AssertionError("State.append_history called"),
        ), patch(
            "prediction_pipeline.transaction_effects.CandidateIndex.update_score",
            side_effect=AssertionError("CandidateIndex.update_score called"),
        ), patch(
            "prediction_pipeline.transaction_effects.CandidateIndex.update_status",
            side_effect=AssertionError("CandidateIndex.update_status called"),
        ), patch(
            "prediction_pipeline.transaction_effects.EvidenceLogger.log",
            side_effect=AssertionError("EvidenceLogger.log called"),
        ):
            pipeline.run()
        events = pipeline.transaction_effects()["evidence_events"]
        battery = [
            event for event in events
            if event.get("event_type") == "battery_evaluated"
        ]
        self.assertEqual(len(battery), 1)
        self.assertEqual(battery[0]["candidate_id"], self.row["candidate_id"])
        self.assertEqual(battery[0]["targets"], ["MDM2"])
        self.assertIn("failed_layers", battery[0])

    def test_battery_evaluated_passes_transaction_scope_validation(self):
        # PR44 回归：battery_evaluated 事件必须通过 Execution 边界
        # _evidence_proposals 的证据类型白名单校验，否则含 battery 的
        # 事务预测会在 Execution 侧被误拒（CI ubuntu 实测失败）。
        root = self.root / "battery-tx-scope"
        pipeline = PredictionPipeline(
            candidate_rows=[self.row],
            project=self.project,
            thresholds={},
            artifacts_root=root / "missing-artifacts",
            run_root=root / "runs",
            run_id="prediction_battery_scope",
            defer_formal_writes=True,
            artifact_id_prefix="tx-battery-scope",
        )
        pipeline.run()
        effects_path = root / "effects.json"
        effects_path.write_text(
            json.dumps(pipeline.transaction_effects()), encoding="utf-8"
        )
        effects = load_prediction_transaction_effects(
            path=effects_path,
            candidate_ids=["C0001"],
            run_id="prediction_battery_scope",
            transaction_id="tx-battery-scope",
            expected_protocol=protocol_binding(),
        )
        self.assertTrue(any(
            event.get("event_type") == "battery_evaluated"
            for event in effects["evidence_events"]
        ))

    def test_real_handler_and_agent_cli_emit_typed_effects(self):
        root = self.root / "real-handler"
        existing = root / "prediction_artifacts" / "C0001"
        existing.mkdir(parents=True)
        packet = self._packet()
        packet["task_attempt"] = 1
        packet["task"]["parameters"]["reuse_complete_evidence"] = True

        handler_context = HandlerContext(
            packet=packet,
            config=self._config(root),
            task_dir=root / "task",
            project_config=self.project,
            transaction_managed=True,
            transaction_id="tx-real-handler",
        )
        with patch(
            "execution.handlers._artifact_bundle_complete", return_value=True
        ), patch(
            "execution.handlers.run_process", side_effect=self._agent_process()
        ), patch(
            "execution.handlers.State.load", return_value=self.state
        ), patch(
            "agents.prediction.State.load", return_value=self.state
        ), patch(
            "agents.prediction.CandidateIndex.load", return_value=[self.row]
        ), patch(
            "prediction_pipeline.transaction_effects.State.update",
            side_effect=AssertionError("State.update called"),
        ), patch(
            "prediction_pipeline.transaction_effects.CandidateIndex.update_score",
            side_effect=AssertionError("CandidateIndex.update_score called"),
        ), patch(
            "prediction_pipeline.transaction_effects.CandidateIndex.update_status",
            side_effect=AssertionError("CandidateIndex.update_status called"),
        ), patch(
            "prediction_pipeline.transaction_effects.EvidenceLogger.log",
            side_effect=AssertionError("EvidenceLogger.log called"),
        ):
            result = evaluate_new_design_candidates(handler_context)
        self.assertEqual(result.candidate_patches[0].candidate_id, "C0001")
        self.assertEqual(result.state_updates["phase"], "evaluate")
        self.assertTrue(result.evidence_events)
        self.assertTrue(result.outputs[0][1].is_file())

    def test_malicious_effects_cannot_patch_candidate_identity(self):
        root = self.root / "malicious-effects"
        (root / "prediction_artifacts" / "C0001").mkdir(parents=True)
        packet = self._packet()
        packet["task"]["parameters"]["reuse_complete_evidence"] = True

        def inject_identity_patch(effects):
            effects["candidate_patches"][0]["patch"]["sequence"] = "AAAAAAA"

        with patch.object(
            self, "_packet", return_value=packet
        ), patch(
            "execution.handlers._artifact_bundle_complete", return_value=True
        ), patch(
            "execution.handlers.run_process",
            side_effect=self._agent_process(inject_identity_patch),
        ), patch(
            "execution.handlers.State.load", return_value=self.state
        ), patch(
            "agents.prediction.State.load", return_value=self.state
        ), patch(
            "agents.prediction.CandidateIndex.load", return_value=[self.row]
        ), self.assertRaisesRegex(Exception, "non-owned fields"):
            self._run(root, handler=evaluate_new_design_candidates)
        store = SQLiteStore(root / "store.db", project_id="prediction_test")
        self.assertEqual(store.get("C0001")["sequence"], SEQUENCE)
        self.assertEqual(store.get("C0001")["final_status"], "pending")
        self.assertEqual(store.query(event_type="prediction_recorded"), [])

    def test_success_atomically_commits_patch_artifacts_state_and_evidence(self):
        store, _, context, result = self._run(
            self.root / "success", handler=self._handler(with_record_input=True)
        )
        self.assertEqual(context.status, TransactionStatus.COMMITTED)
        row = store.get("C0001")
        self.assertEqual(row["final_status"], "prediction_pending")
        prediction = store.get_state("prediction_test")["prediction"]
        self.assertIn("handoff_artifact_id", prediction)
        handoff_artifact = store.get_artifact(prediction["handoff_artifact_id"])
        self.assertIsNotNone(handoff_artifact)
        handoff_path = Path(handoff_artifact["path"])
        self.assertNotIn("handoff_sha256", prediction)
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
        handoff_item = handoff["categories"]["prediction_pending"][0]
        record_artifact = store.get_artifact(handoff_item["record_artifact_id"])
        self.assertEqual(handoff_item["record_path"], record_artifact["path"])
        self.assertTrue(Path(record_artifact["path"]).is_file())
        record = json.loads(Path(record_artifact["path"]).read_text(encoding="utf-8"))
        input_reference = record["artifact_inventory"][0]
        input_artifact = store.get_artifact(input_reference["artifact_id"])
        self.assertEqual(input_reference["path"], input_artifact["path"])
        self.assertTrue(Path(input_artifact["path"]).is_file())
        metrics = json.loads(row["metrics_json"])
        self.assertEqual(
            metrics["prediction"]["record_sha256"],
            handoff_item["record_sha256"],
        )
        self.assertEqual(
            prediction["record_artifacts"]["C0001"],
            {"artifact_id": handoff_item["record_artifact_id"]},
        )
        artifact_types = {
            item.artifact_type for item in result.artifacts
        }
        self.assertIn("prediction_handoff", artifact_types)
        self.assertIn("prediction_record", artifact_types)
        events = store.query(task_id="T001")
        self.assertTrue(any(item["event_type"] == "prediction_recorded" for item in events))
        recorded = next(item for item in events if item["event_type"] == "prediction_recorded")
        self.assertEqual(recorded["workflow_id"], "workflow-prediction")
        self.assertEqual(recorded["attempt_id"], "T001-A01")
        self.assertEqual(recorded["transaction_id"], context.transaction_id)
        self.assertEqual(recorded["candidate_id"], "C0001")
        self.assertEqual(recorded["protocol_identity"], protocol_binding())
        self.assertEqual(recorded["record_artifact_id"], handoff_item["record_artifact_id"])
        self.assertNotIn("artifact_sha256", recorded)

    def test_validation_failure_leaves_formal_store_unchanged(self):
        root = self.root / "validation-failure"
        with self.assertRaises(ExecutionContractError) as raised:
            self._run(root, handler=self._handler(tamper_record=True))
        self.assertEqual(raised.exception.code, "prediction_record_invalid")
        store = SQLiteStore(root / "store.db", project_id="prediction_test")
        self.assertEqual(store.get("C0001")["final_status"], "pending")
        self.assertEqual(store.query(event_type="prediction_recorded"), [])
        self.assertFalse(any((root / "execution" / "artifacts").rglob("*.json")))

    def test_subprocess_failure_has_no_formal_prediction_effects(self):
        root = self.root / "subprocess-failure"

        def failing_handler(_context):
            raise RuntimeError("predictor subprocess failed")

        with self.assertRaisesRegex(RuntimeError, "subprocess failed"):
            self._run(root, handler=failing_handler)
        store = SQLiteStore(root / "store.db", project_id="prediction_test")
        self.assertEqual(store.get("C0001")["final_status"], "pending")
        self.assertEqual(store.query(event_type="prediction_recorded"), [])

    def test_db_commit_failure_removes_moved_artifacts_and_candidate_patch(self):
        root = self.root / "commit-failure"
        with self.assertRaisesRegex(RuntimeError, "injected commit failure"):
            self._run(root, store_class=FailingCommitStore)
        store = SQLiteStore(root / "store.db", project_id="prediction_test")
        self.assertEqual(store.get("C0001")["final_status"], "pending")
        self.assertEqual(store.query(event_type="prediction_recorded"), [])
        self.assertFalse(any((root / "execution" / "artifacts").rglob("*.json")))

    def test_post_commit_compensation_restores_owned_candidate_patch(self):
        store, worker, context, _ = self._run(self.root / "compensation")
        self.assertEqual(store.get("C0001")["final_status"], "prediction_pending")
        worker.rollback(context)
        self.assertEqual(context.status, TransactionStatus.ROLLED_BACK)
        self.assertEqual(store.get("C0001")["final_status"], "pending")

    def test_retry_changes_attempt_without_duplicate_prediction_effects(self):
        root = self.root / "retry"
        with self.assertRaises(Exception):
            self._run(root, handler=self._handler(tamper_record=True), attempt=1)
        store, _, context, _ = self._run(root, attempt=2)
        self.assertEqual(context.status, TransactionStatus.COMMITTED)
        events = store.query(event_type="prediction_recorded")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["attempt_id"], "T001-A02")

    def test_protocol_mismatch_is_rejected_before_commit(self):
        root = self.root / "protocol-mismatch"
        handler = self._handler()
        packet = self._packet()
        packet["task"]["parameters"]["predictor_protocol"] = {
            "name": "old",
            "version": "old",
            "sha256": "0" * 64,
        }
        store = SQLiteStore(root / "store.db", project_id="prediction_test")
        store.replace_state("prediction_test", self.state)
        store.upsert(self.row, duplicate_policy="insert_only")
        context = TransactionContext.create(
            workflow_id="workflow-prediction",
            run_id="run-prediction-typed",
            task_id="T001",
            attempt_id="T001-A01",
            action="evaluate_new_design_candidates",
            metadata={"project_id": "prediction_test"},
        )
        config = self._config(root)
        adapter = adapter_for(
            "evaluate_new_design_candidates",
            handler,
            packet,
            config,
            root / "task",
            self.project,
        )
        worker = ExecutionWorker(
            store,
            root / "staging",
            config.execution_root / "artifacts",
        )
        with self.assertRaisesRegex(Exception, "unsupported protocol"):
            worker.run(context, adapter, validator=_validate_action_result)
        self.assertEqual(store.get("C0001")["final_status"], "pending")


if __name__ == "__main__":
    unittest.main()
