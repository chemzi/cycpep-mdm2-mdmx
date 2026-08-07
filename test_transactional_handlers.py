"""Critic and Calibration typed transaction boundary tests."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from contracts.transaction import TransactionContext, TransactionStatus
from execution.adapters import adapter_for, make_legacy_handler_adapter
from execution.config import ExecutionConfig
from execution.contracts import ExecutionContractError, validate_output_inventory
from execution.handlers import HandlerContext
from execution.results import ExecutionActionResult
from execution.worker import ExecutionWorker, _validate_action_result
from prediction_pipeline.contracts import file_sha256
from storage import SQLiteStore


class TransactionalHandlerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="transactional-handlers-"))

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

    def _case(self, action: str, root: Path) -> tuple[dict, str, str]:
        if action == "review_prediction_handoff":
            handoff = root / "prediction_handoff.json"
            handoff.write_text("{}", encoding="utf-8")
            dependency = {
                "role": "prediction_handoff",
                "path": str(handoff),
                "sha256": file_sha256(handoff),
            }
            task = {
                "task_id": "T001",
                "action": action,
                "phase": "critic",
                "parameters": {"min_cohort": 3, "low_diversity_similarity": 0.8},
                "candidate_scope": {"candidate_ids": [], "from_task_id": "T000"},
                "resource_request": {"class": "cpu", "proposal_count": 0, "candidate_limit": 0},
                "outputs": ["critic_report.json"],
            }
            return {
                "run_id": "run-typed",
                "task": task,
                "dependency_outputs": {"T000": [dependency]},
            }, "critic_report", dependency["sha256"]
        task = {
            "task_id": "T001",
            "action": action,
            "phase": "iterate",
            "parameters": {"threshold_keys": ["L2_ipsae:MDM2"]},
            "candidate_scope": {"candidate_ids": [], "from_task_id": None},
            "resource_request": {
                "class": "network_cpu", "proposal_count": 0, "candidate_limit": 0
            },
            "outputs": ["threshold_calibration_proposal.json"],
        }
        return {
            "run_id": "run-typed",
            "task": task,
            "trace_context": {"project_id": "typed-test"},
        }, "calibration_proposal", ""

    def _valid_payload(self, action: str, task: dict, dependency_digest: str) -> dict:
        if action == "review_prediction_handoff":
            digest = "a" * 64
            return {
                "schema_version": 1,
                "critic_version": "1.1.1",
                "report_id": f"critic_{digest[:12]}",
                "input_digest": digest,
                "source": {"prediction_handoff_sha256": dependency_digest},
                "verdict": "clear",
                "passed": True,
                "issues": [],
                "recommendations": [],
                "planner_handoff": {},
            }
        return {
            "schema_version": 1,
            "execution_worker_version": "test",
            "action": action,
            "task_id": task["task_id"],
            "project_id": "typed-test",
            "status": "pending_controls",
            "requested_threshold_keys": ["L2_ipsae:MDM2"],
            "current_thresholds": {},
            "control_data": {"available": False, "path": None, "sha256": None},
            "control_requirements": {},
            "applied_to_state": False,
            "created_at": "2026-08-07T00:00:00+00:00",
        }

    def _handler(self, action: str, role: str, payload: dict, *, invalid: bool = False):
        def handler(context: HandlerContext) -> ExecutionActionResult:
            self.assertTrue(context.transaction_managed)
            path = context.task_dir / "outputs" / f"{role}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({} if invalid else payload), encoding="utf-8")
            return ExecutionActionResult(
                state_updates={"typed_owner": action},
                evidence_events=({
                    "agent": "critic" if action == "review_prediction_handoff" else "execution",
                    "event_type": "critic_review" if action == "review_prediction_handoff" else "threshold_calibration",
                    "phase": "critic" if action == "review_prediction_handoff" else "iterate",
                    "status": "validated",
                },),
                outputs=((role, path),),
            )
        return handler

    def _run(self, action: str, root: Path, *, invalid: bool = False, attempt: int = 1):
        packet, role, dependency_digest = self._case(action, root)
        task_dir = root / f"task-attempt-{attempt}"
        store = SQLiteStore(root / "store.db", project_id="typed-test")
        store.replace_state("typed-test", {"phase": "iterate"})
        context = TransactionContext.create(
            workflow_id="workflow-typed",
            run_id="run-typed",
            task_id="T001",
            attempt_id=f"T001-A{attempt:02d}",
            action=action,
        )
        worker = ExecutionWorker(store, root / "staging", root / "artifacts")
        payload = self._valid_payload(action, packet["task"], dependency_digest)
        adapter = adapter_for(
            action,
            self._handler(action, role, payload, invalid=invalid),
            packet,
            self._config(root),
            task_dir,
            None,
        )
        result = worker.run(context, adapter, validator=_validate_action_result)
        return store, worker, context, result

    def test_success_atomically_commits_artifact_state_and_ordered_evidence(self):
        for action in ("review_prediction_handoff", "propose_threshold_calibration"):
            with self.subTest(action=action):
                root = self.root / action
                root.mkdir()
                store, _, context, result = self._run(action, root)
                self.assertEqual(context.status, TransactionStatus.COMMITTED)
                self.assertEqual(store.get_state("typed-test")["typed_owner"], action)
                artifact = store.get_artifact(result.artifacts[0].artifact_id)
                self.assertIsNotNone(artifact)
                self.assertTrue(Path(artifact["path"]).is_file())
                self.assertEqual(result.outputs[0][1], Path(artifact["path"]))
                self.assertIn(str(root / "artifacts"), str(result.outputs[0][1]))
                events = store.query(task_id="T001")
                expected = "critic_review" if action.startswith("review") else "threshold_calibration"
                self.assertEqual(
                    [event["event_type"] for event in events],
                    [expected, "execution_transaction_committed"],
                )
                self.assertEqual(events[0]["attempt_id"], "T001-A01")

    def test_semantic_validation_failure_rolls_back_both_handlers(self):
        for action in ("review_prediction_handoff", "propose_threshold_calibration"):
            with self.subTest(action=action):
                root = self.root / f"invalid-{action}"
                root.mkdir()
                with self.assertRaises(ExecutionContractError):
                    self._run(action, root, invalid=True)
                store = SQLiteStore(root / "store.db", project_id="typed-test")
                self.assertEqual(store.get_state("typed-test"), {"phase": "iterate"})
                self.assertEqual(store.query(event_type="critic_review"), [])
                self.assertEqual(store.query(event_type="threshold_calibration"), [])
                self.assertFalse(any((root / "artifacts").rglob("*.json")))

    def test_handler_exception_rolls_back_and_records_attempt(self):
        for action in ("review_prediction_handoff", "propose_threshold_calibration"):
            with self.subTest(action=action):
                root = self.root / f"exception-{action}"
                root.mkdir()
                packet, _, _ = self._case(action, root)
                store = SQLiteStore(root / "store.db", project_id="typed-test")
                context = TransactionContext.create(
                    workflow_id="workflow-typed", run_id="run-typed", task_id="T001",
                    attempt_id="T001-A01", action=action,
                )
                worker = ExecutionWorker(store, root / "staging", root / "artifacts")
                def failing_handler(_context):
                    raise RuntimeError("handler failed")
                adapter = adapter_for(
                    action, failing_handler, packet, self._config(root), root / "task", None
                )
                with self.assertRaisesRegex(RuntimeError, "handler failed"):
                    worker.run(context, adapter, validator=_validate_action_result)
                self.assertEqual(context.status, TransactionStatus.FAILED)
                failed = store.query(task_id="T001")
                self.assertEqual([event["attempt_id"] for event in failed], ["T001-A01"])
                self.assertEqual(store.get_state("typed-test"), {})

    def test_retry_keeps_workflow_and_task_but_changes_attempt(self):
        for action in ("review_prediction_handoff", "propose_threshold_calibration"):
            with self.subTest(action=action):
                root = self.root / f"retry-{action}"
                root.mkdir()
                with self.assertRaises(ExecutionContractError):
                    self._run(action, root, invalid=True, attempt=1)
                store, _, _, _ = self._run(action, root, attempt=2)
                events = store.query(task_id="T001")
                self.assertEqual({event["workflow_id"] for event in events}, {"workflow-typed"})
                self.assertEqual({event["task_id"] for event in events}, {"T001"})
                self.assertEqual(
                    {event["attempt_id"] for event in events}, {"T001-A01", "T001-A02"}
                )

    def test_both_actions_bypass_legacy_empty_transaction_bridge(self):
        for action in ("review_prediction_handoff", "propose_threshold_calibration"):
            with self.subTest(action=action), patch(
                "execution.adapters.make_legacy_handler_adapter",
                side_effect=AssertionError("legacy bridge used"),
            ):
                root = self.root / f"bridge-{action}"
                root.mkdir()
                self._run(action, root)

    def test_real_critic_and_calibration_handlers_use_typed_path(self):
        from execution.handlers import (
            propose_threshold_calibration,
            review_prediction_handoff,
        )
        from test_critic import complete_battery, metrics

        state = {
            "project_id": "typed-test",
            "phase": "iterate",
            "thresholds": {},
            "iteration_history": [],
            "project_config": {
                "project_id": "typed-test",
                "targets": [{"id": "MDM2"}, {"id": "MDMX"}],
            },
        }
        for action, handler in (
            ("review_prediction_handoff", review_prediction_handoff),
            ("propose_threshold_calibration", propose_threshold_calibration),
        ):
            with self.subTest(action=action), patch(
                "execution.adapters.make_legacy_handler_adapter",
                side_effect=AssertionError("legacy bridge used"),
            ), patch("execution.handlers.State.load", return_value=state), patch(
                "execution.handlers.CandidateIndex.load",
                return_value=[{
                    "candidate_id": "C0001",
                    "sequence": "ACDEFGHI",
                    "source_route": "route_A",
                }],
            ), patch(
                "agents.critic.report.State.update",
                side_effect=AssertionError("Critic mutated State before commit"),
            ), patch(
                "agents.critic.report.EvidenceLogger.critic_review",
                side_effect=AssertionError("Critic emitted Evidence before commit"),
            ):
                root = self.root / f"real-{action}"
                root.mkdir()
                packet, _, _ = self._case(action, root)
                if action == "review_prediction_handoff":
                    record = root / "record.json"
                    record.write_text(json.dumps({
                        "schema_version": 2,
                        "pipeline_version": "1.5.0",
                        "run_id": "prediction-run",
                        "candidate": {"candidate_id": "C0001", "sequence": "ACDEFGHI"},
                        "status": "finalized",
                        "metrics": metrics(),
                        "battery": complete_battery(),
                        "issues": [],
                        "provenance": [],
                        "artifact_inventory": [],
                    }), encoding="utf-8")
                    handoff = root / "prediction_handoff.json"
                    handoff.write_text(json.dumps({
                        "schema_version": 2,
                        "pipeline_version": "1.5.0",
                        "run_id": "prediction-run",
                        "project_id": "typed-test",
                        "required_targets": ["MDM2", "MDMX"],
                        "categories": {"finalized": [{
                            "candidate_id": "C0001",
                            "record_path": str(record),
                            "record_sha256": file_sha256(record),
                            "issues": [],
                        }]},
                        "downstream": {"authoritative_record_field": "record_path"},
                    }), encoding="utf-8")
                    packet["dependency_outputs"]["T000"][0].update({
                        "path": str(handoff),
                        "sha256": file_sha256(handoff),
                    })
                store = SQLiteStore(root / "store.db", project_id="typed-test")
                store.replace_state("typed-test", state)
                context = TransactionContext.create(
                    workflow_id="workflow-typed", run_id="run-typed", task_id="T001",
                    attempt_id="T001-A01", action=action,
                )
                worker = ExecutionWorker(store, root / "staging", root / "artifacts")
                adapter = adapter_for(
                    action, handler, packet, self._config(root), root / "task", None
                )
                result = worker.run(context, adapter, validator=_validate_action_result)
                self.assertEqual(context.status, TransactionStatus.COMMITTED)
                self.assertIsNotNone(store.get_artifact(result.artifacts[0].artifact_id))

    def test_legacy_adapter_contract_remains_available(self):
        root = self.root / "legacy"
        root.mkdir()
        packet, _, _ = self._case("propose_threshold_calibration", root)
        def legacy_handler(_context):
            return ExecutionActionResult()
        adapter = make_legacy_handler_adapter(
            legacy_handler, packet, self._config(root), root / "task", None
        )
        context = TransactionContext.create(
            workflow_id="workflow-legacy", run_id="run-legacy", task_id="T001",
            attempt_id="T001-A01", action="propose_threshold_calibration",
        )
        result = adapter(context, Mock())
        self.assertIsInstance(result, ExecutionActionResult)

    def test_calibration_rejects_threshold_key_mismatch(self):
        packet, role, _ = self._case("propose_threshold_calibration", self.root)
        payload = self._valid_payload(packet["task"]["action"], packet["task"], "")
        payload["requested_threshold_keys"] = ["different:key"]
        path = self.root / "threshold-mismatch.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ExecutionContractError, "threshold keys"):
            validate_output_inventory(
                packet["task"],
                [{"role": role, "path": str(path)}],
                approved_project_id="typed-test",
            )

    def test_calibration_validates_control_data_file_and_sha256(self):
        packet, role, _ = self._case("propose_threshold_calibration", self.root)
        payload = self._valid_payload(packet["task"]["action"], packet["task"], "")
        proposal = self.root / "control-proposal.json"
        payload["status"] = "ready_for_calibration"
        payload["control_data"] = {
            "available": True,
            "path": str(self.root / "missing-controls.json"),
            "sha256": "a" * 64,
        }
        proposal.write_text(json.dumps(payload), encoding="utf-8")
        inventory = [{"role": role, "path": str(proposal)}]
        with self.assertRaisesRegex(ExecutionContractError, "missing or changed"):
            validate_output_inventory(
                packet["task"], inventory, approved_project_id="typed-test"
            )

        controls = self.root / "controls.json"
        controls.write_text("{}", encoding="utf-8")
        payload["control_data"].update(path=str(controls), sha256="b" * 64)
        proposal.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ExecutionContractError, "missing or changed"):
            validate_output_inventory(
                packet["task"], inventory, approved_project_id="typed-test"
            )

        payload["control_data"]["sha256"] = file_sha256(controls)
        proposal.write_text(json.dumps(payload), encoding="utf-8")
        validate_output_inventory(
            packet["task"], inventory, approved_project_id="typed-test"
        )


if __name__ == "__main__":
    unittest.main()
