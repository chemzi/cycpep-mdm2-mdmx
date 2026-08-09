"""Execution Worker registry, process and Orchestrator integration tests."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import data_layer
from agents.orchestrator import initialize, status
from agents.planner import record_approval, run as planner_run
from execution.config import ExecutionConfig
from execution.contracts import (
    V2_RESERVED_ACTIONS,
    ExecutionContractError,
    assert_action_executable,
    validate_task_parameters,
)
from execution.handlers import _artifact_bundle_complete
from execution.supervisor import _terminate_group, run_process
from execution.worker import _orchestrator_state_for_transaction, execute_task
from execution.results import ExecutionActionResult
from prediction_pipeline.contracts import object_sha256
from storage import SQLiteStore


POLICY_CONSTRAINTS = [
    "do_not_change_thresholds_automatically",
    "do_not_delete_candidates_automatically",
    "do_not_start_gpu_jobs_without_planner_budget_and_execution_approval",
    "reuse_complete_prediction_evidence",
]


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="execution-test-"))
        self.original_paths = (
            data_layer.DATA_DIR,
            data_layer.EVIDENCE_DIR,
            data_layer.STATE_PATH,
            data_layer.LOG_PATH,
            data_layer.INDEX_PATH,
        )
        data_layer.DATA_DIR = self.root / "data"
        data_layer.EVIDENCE_DIR = self.root / "evidence"
        data_layer.STATE_PATH = data_layer.DATA_DIR / "state.json"
        data_layer.LOG_PATH = data_layer.EVIDENCE_DIR / "evidence_log.jsonl"
        data_layer.INDEX_PATH = data_layer.DATA_DIR / "candidate_index.csv"
        data_layer.State.save(self._state())

    def tearDown(self):
        (
            data_layer.DATA_DIR,
            data_layer.EVIDENCE_DIR,
            data_layer.STATE_PATH,
            data_layer.LOG_PATH,
            data_layer.INDEX_PATH,
        ) = self.original_paths

    @staticmethod
    def _state():
        return {
            "project_id": "execution_test",
            "round": 2,
            "phase": "critic",
            "design_budget": {"route_A_mdm2": 20},
            "thresholds": {"L2_ipsae": {"value": None}},
            "project_config": {
                "project_id": "execution_test",
                "modality": "head_to_tail_cyclic_peptide",
                "targets": [{
                    "id": "MDM2",
                    "required": True,
                    "design": {"lengths": [8, 10, 12]},
                }],
                "review": {
                    "status": "approved",
                    "approved_digest": "a" * 64,
                    "content_digest": "a" * 64,
                },
            },
            "iteration_history": [],
        }

    def _report(self, *, calibration=False):
        action = "calibrate_thresholds" if calibration else "iterate_interface_design"
        code = "threshold_calibration_pending" if calibration else "l2_interface_confidence_low"
        issue = {
            "code": code,
            "severity": "medium" if calibration else "high",
            "category": "calibration" if calibration else "scientific_metric",
            "message": code,
            "candidate_ids": ["C0001"],
            "evidence": [{"threshold_keys": ["L2_ipsae:MDM2"]}] if calibration else [],
            "recommended_action": action,
            "owner_hint": "research" if calibration else "design",
            "blocks_finalization": True,
        }
        digest = object_sha256({"action": action, "root": str(self.root)})
        report_id = f"critic_{digest[:12]}"
        report = {
            "schema_version": 1,
            "critic_version": "1.1.1",
            "report_id": report_id,
            "input_digest": digest,
            "source": {
                "prediction_handoff": str(self.root / "prediction.json"),
                "prediction_handoff_sha256": "b" * 64,
                "prediction_run_id": "prediction_execution_test",
                "prediction_pipeline_version": "1.5.1",
                "project_id": "execution_test",
                "required_targets": ["MDM2"],
                "record_count": 1,
            },
            "verdict": "review" if calibration else "iterate",
            "passed": False,
            "summary": "fixture",
            "issue_counts": {},
            "issues": [issue],
            "metrics_snapshot": {},
            "recommendations": [{
                "action": action,
                "owner_hint": issue["owner_hint"],
                "priority": "P2" if calibration else "P1",
                "reason_codes": [code],
                "approval_required": calibration,
            }],
            "planner_handoff": {
                "critic_report_id": report_id,
                "issue_codes": [code],
                "recommended_actions": [action],
                "policy_constraints": POLICY_CONSTRAINTS,
            },
        }
        path = self.root / f"{report_id}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    def _config(self, *, design_python: Path | None = None):
        repo = Path(__file__).resolve().parent
        missing = self.root / "missing"
        return ExecutionConfig(
            repo_root=repo,
            execution_root=self.root / "execution",
            core_python=Path("/usr/bin/python3"),
            design_python=design_python or missing,
            prediction_python=missing,
            prediction_artifacts_root=self.root / "artifacts",
            prediction_runs_root=self.root / "prediction_runs",
            colabdesign_dir=missing,
            colabdesign_params=missing,
            cuda_data_dir=missing,
            boltz_executable=None,
            boltz_cache=None,
            boltz_checkpoint=None,
            prodigy_executable=None,
            pyrosetta_python=None,
            control_data_path=None,
        )

    def test_planner_materializes_a_typed_design_job(self):
        plan = planner_run(critic_report_path=self._report())["plan"]
        task = plan["tasks"][0]
        parameters = validate_task_parameters(task)
        self.assertEqual(task["action"], "iterate_design")
        self.assertEqual(parameters["design_jobs"][0]["target_id"], "MDM2")
        self.assertEqual(parameters["design_jobs"][0]["route"], "A")
        self.assertEqual(parameters["design_jobs"][0]["lengths"], [8, 10, 12])
        self.assertEqual(
            sum(job["proposal_count"] for job in parameters["design_jobs"]),
            task["resource_request"]["proposal_count"],
        )

    def test_materialized_design_job_consumes_experience_length_preference(self):
        # P1-1 输出侧：无显式长度配置时，Planner 任务构造消费失败经验偏好
        from agents.planner.task_builder import _materialize_design_jobs

        def battery(passed, failed_layers):
            return {
                "all_layers_pass": passed,
                "competition_clearance": passed,
                "failed_layers": [] if passed else failed_layers,
                "hard_failures": [],
                "missing_thresholds": [],
                "triage_status": "shortlisted" if passed else "needs_optimization",
                "layer_values": {"L4_nc_distance_post": 1.2 if passed else 3.4},
                "target_pass": {},
                "required_targets": ["MDM2"],
            }

        for index in range(6):
            data_layer.EvidenceLogger.battery_evaluated(
                {"candidate_id": f"E10{index:03d}", "sequence": "ABCDEFGHIJ",
                 "source_route": "route_A"},
                battery(False, ["l4_pass"]),
            )
        for index in range(6):
            data_layer.EvidenceLogger.battery_evaluated(
                {"candidate_id": f"E12{index:03d}", "sequence": "ABCDEFGHIJKL",
                 "source_route": "route_A"},
                battery(True, []),
            )
        state = {
            "round": 1,
            "project_config": {
                "project_id": "execution_test",
                "targets": [{"id": "MDM2", "required": True, "design": {}}],
            },
        }
        jobs = _materialize_design_jobs(
            state=state,
            required_targets=["MDM2"],
            budgets={"route_A_mdm2": 10},
            requested=10,
            seed_material="experience-length-test",
        )
        self.assertEqual([job["lengths"] for job in jobs], [[12]])
        applied = [
            event for event in data_layer.EvidenceLogger.get_all()
            if event.get("event_type") == "experience_applied"
        ]
        self.assertTrue(any(event.get("new_lengths") == [12] for event in applied))

    def test_materialized_design_job_explicit_lengths_win_over_experience(self):
        # P2-1：显式长度配置永远优先于经验偏好
        from agents.planner.task_builder import _materialize_design_jobs

        data_layer.EvidenceLogger.battery_evaluated(
            {"candidate_id": "E1000", "sequence": "ABCDEFGHIJ",
             "source_route": "route_A"},
            {"all_layers_pass": False, "competition_clearance": False,
             "failed_layers": ["l4_pass"], "hard_failures": [],
             "missing_thresholds": [], "triage_status": "needs_optimization",
             "layer_values": {}, "target_pass": {},
             "required_targets": ["MDM2"]},
        )
        state = {
            "round": 1,
            "project_config": {
                "project_id": "execution_test",
                "targets": [{
                    "id": "MDM2", "required": True,
                    "design": {"lengths": [8, 9]},
                }],
            },
        }
        jobs = _materialize_design_jobs(
            state=state,
            required_targets=["MDM2"],
            budgets={"route_A_mdm2": 10},
            requested=10,
            seed_material="explicit-lengths-win",
        )
        self.assertEqual(jobs[0]["lengths"], [8, 9])

    def test_design_cli_forwards_lengths_into_design_config(self):
        # P1-1 输出端回归：Execution 传的 --lengths 必须真正进入
        # design_config，否则官告“显式参数优先”但实际被
        # route_a 内部的 apply_experience_preference 二次覆盖（重复记账）。
        from agents.design import cli

        captured = {}

        class _FakeDesign:
            def __init__(self):
                pass

            def design_rfpeptides(self, target_spec=None, design_config=None):
                captured["design_config"] = dict(design_config or {})
                return []

        with patch("agents.design.cli.Design", _FakeDesign), patch(
            "agents.design.cli.configure_candidate_updates"
        ), patch(
            "agents.design.cli.flush_candidate_updates"
        ):
            rc = cli.main([
                "--route", "A",
                "--target", "MDM2",
                "--n", "1",
                "--lengths", "12",
                "--seed", "42",
                "--candidate-updates-path", str(self.root / "cli-updates.json"),
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(captured["design_config"].get("lengths"), [12])


    def test_reserved_v2_actions_have_no_v1_handler(self):
        for action in V2_RESERVED_ACTIONS:
            task = {
                "action": action,
                "parameters": {},
                "candidate_scope": {"candidate_ids": [], "from_task_id": None},
                "resource_request": {"proposal_count": 0, "candidate_limit": 0},
                "outputs": [],
            }
            with self.assertRaisesRegex(ExecutionContractError, "reserved for v2"):
                assert_action_executable(task)

    def test_terminate_group_windows_branch_avoids_os_killpg(self):
        # P2-1: os.killpg is POSIX-only; on Windows the timeout/interrupt
        # path must use taskkill /T instead of raising AttributeError.
        process = Mock()
        process.poll.return_value = None
        process.pid = 4242
        process.wait.return_value = 0
        with patch("execution.supervisor.os.name", "nt"), patch(
            "execution.supervisor.subprocess.run"
        ) as run:
            _terminate_group(process, grace_seconds=1.0)
        args = run.call_args.args[0]
        self.assertEqual(args[:3], ["taskkill", "/F", "/T"])
        self.assertEqual(args[-1], "4242")
        process.wait.assert_called()

    def test_terminate_group_returns_early_when_process_finished(self):
        process = Mock()
        process.poll.return_value = 0
        with patch("execution.supervisor.os.name", "nt"), patch(
            "execution.supervisor.subprocess.run"
        ) as run:
            _terminate_group(process, grace_seconds=1.0)
        run.assert_not_called()

    def test_process_arguments_are_not_interpreted_by_a_shell(self):
        marker = self.root / "must_not_exist"
        result = run_process(
            [sys.executable, "-c", "import sys; print(sys.argv[1])", f"safe; touch {marker}"],
            cwd=self.root,
            logs_dir=self.root / "logs",
            timeout_seconds=10,
            label="shell_injection_regression",
        )
        self.assertEqual(result["returncode"], 0)
        self.assertFalse(marker.exists())
        self.assertIn("safe; touch", Path(result["stdout"]).read_text())

    def test_calibration_handler_completes_without_mutating_thresholds(self):
        plan_result = planner_run(critic_report_path=self._report(calibration=True))
        task_id = plan_result["plan"]["tasks"][0]["task_id"]
        approval = record_approval(
            plan_path=plan_result["plan_path"],
            task_ids=[task_id],
            approver="PI-test",
            justification="review threshold proposal only",
        )
        initialized = initialize(
            plan_path=plan_result["plan_path"],
            approval_paths=[approval["approval_path"]],
        )
        before = data_layer.State.load()["thresholds"]
        receipt = execute_task(
            run_path=initialized["run_path"],
            task_id=task_id,
            worker_id="execution-test",
            config=self._config(),
        )
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(data_layer.State.load()["thresholds"], before)
        proposal = json.loads(Path(receipt["outputs"][0]["path"]).read_text())
        self.assertIn(str(self.root / "execution" / "artifacts"), receipt["outputs"][0]["path"])
        self.assertFalse(proposal["applied_to_state"])
        self.assertEqual(proposal["status"], "pending_controls")

    def test_failed_gpu_handler_closes_claim_and_releases_lease(self):
        plan_result = planner_run(critic_report_path=self._report())
        task_id = plan_result["plan"]["tasks"][0]["task_id"]
        approval = record_approval(
            plan_path=plan_result["plan_path"],
            task_ids=plan_result["plan"]["approval_request"]["required_task_ids"],
            approver="PI-test",
            justification="bounded failure regression",
            max_gpu_job_slots=1,
            max_gpu_minutes=1,
            max_design_proposals=12,
            max_prediction_candidates=12,
        )
        initialized = initialize(
            plan_path=plan_result["plan_path"],
            approval_paths=[approval["approval_path"]],
        )
        with self.assertRaisesRegex(ExecutionContractError, "executable not found"):
            execute_task(
                run_path=initialized["run_path"],
                task_id=task_id,
                worker_id="execution-test",
                config=self._config(),
            )
        snapshot = status(run_path=initialized["run_path"])["run"]
        self.assertEqual(snapshot["tasks"][task_id]["status"], "failed")
        self.assertIsNone(snapshot["resources"]["gpu_lease"])
        self.assertFalse((data_layer.DATA_DIR / "orchestrator" / "gpu_lease.json").exists())


    def test_handler_context_injects_project_config(self):
        from execution.handlers import HandlerContext, propose_threshold_calibration
        plan_result = planner_run(critic_report_path=self._report(calibration=True))
        task = plan_result["plan"]["tasks"][0]
        injected = {"project_id": "keap1", "targets": [{"id": "KEAP1"}]}
        task_dir = self.root / "injected_task"
        task_dir.mkdir(parents=True)
        outcome = propose_threshold_calibration(HandlerContext(
            packet={"task": task},
            config=self._config(),
            task_dir=task_dir,
            project_config=injected,
        ))
        proposal = json.loads(
            Path(outcome.outputs[0][1]).read_text(encoding="utf-8")
        )
        self.assertEqual(proposal["project_id"], "keap1")
        # State is untouched by the injection.
        self.assertEqual(data_layer.State.load()["project_id"], "execution_test")

    def _complete_failure_case(self, *, rollback_error: Exception | None = None):
        config = self._config()
        store = SQLiteStore(self.root / "closure.db", project_id="execution_test")
        task = {
            "task_id": "T001",
            "action": "propose_threshold_calibration",
            "phase": "iterate",
            "parameters": {"threshold_keys": []},
            "candidate_scope": {"candidate_ids": [], "from_task_id": None},
            "resource_request": {
                "class": "network_cpu", "proposal_count": 0, "candidate_limit": 0
            },
            "outputs": ["threshold_calibration_proposal.json"],
        }
        claimed = {
            "claim_token": "claim-close",
            "dispatch_packet_path": str(self.root / "dispatch.json"),
            "dispatch_packet_sha256": "unused",
            "run": {
                "run_id": "run-close",
                "workflow_id": "workflow-close",
                "plan": {
                    "project_id": "execution_test",
                    "plan_id": "plan-close",
                },
                "tasks": {"T001": {"attempts": 1}},
                "resources": {},
            },
        }
        packet = {"run_id": "run-close", "task": task}

        def adapter(_context, _staging):
            return ExecutionActionResult(state_updates={"committed_then_closed": True})

        fail_mock = Mock(return_value={})
        evidence_mock = Mock()
        patches = [
            patch("execution.worker.claim", return_value=claimed),
            patch("execution.worker._read_packet", return_value=packet),
            patch("execution.worker.handler_for", return_value=Mock()),
            patch("execution.worker.adapter_for", return_value=adapter),
            patch("execution.worker.complete", side_effect=RuntimeError("complete failed")),
            patch("execution.worker.probe_orchestrator_state", return_value="open"),
            patch("execution.worker.fail", fail_mock),
            patch("execution.worker.get_storage_backend", return_value=store),
            patch("execution.worker.refresh_projections"),
            patch("execution.worker.EvidenceLogger.log", evidence_mock),
        ]
        if rollback_error is not None:
            patches.append(patch(
                "execution.worker.ExecutionWorker.rollback", side_effect=rollback_error
            ))
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            with self.assertRaisesRegex(RuntimeError, "complete failed"):
                execute_task(
                    run_path=self.root / "run.json",
                    task_id="T001",
                    worker_id="execution-test",
                    config=config,
                )
        failure_path = config.task_dir("run-close", "T001", 1) / "execution_failure.json"
        return (
            store,
            fail_mock,
            evidence_mock,
            json.loads(failure_path.read_text(encoding="utf-8")),
        )

    def test_post_commit_bookkeeping_failure_is_not_a_task_failure(self):
        """complete() succeeded => receipt/evidence/cleanup failure must not retry."""
        config = self._config()
        store = SQLiteStore(self.root / "post-commit.db", project_id="execution_test")
        task = {
            "task_id": "T001",
            "action": "propose_threshold_calibration",
            "phase": "iterate",
            "parameters": {"threshold_keys": []},
            "candidate_scope": {"candidate_ids": [], "from_task_id": None},
            "resource_request": {
                "class": "network_cpu", "proposal_count": 0, "candidate_limit": 0
            },
            "outputs": ["threshold_calibration_proposal.json"],
        }
        claimed = {
            "claim_token": "claim-post",
            "dispatch_packet_path": str(self.root / "dispatch.json"),
            "dispatch_packet_sha256": "unused",
            "run": {
                "run_id": "run-post",
                "workflow_id": "workflow-post",
                "plan": {"project_id": "execution_test", "plan_id": "plan-post"},
                "tasks": {"T001": {"attempts": 1}},
                "resources": {},
            },
        }
        packet = {"run_id": "run-post", "task": task}

        def adapter(_context, _staging):
            return ExecutionActionResult(state_updates={"committed": True})

        completed_run = {
            "run": {
                "status": "running",
                "tasks": {"T001": {"status": "succeeded", "outputs": []}},
            }
        }
        fail_mock = Mock(return_value={})
        real_atomic_json = __import__(
            "execution.supervisor", fromlist=["atomic_json"]
        ).atomic_json

        def flaky_atomic_json(path, value):
            if Path(path).name == "execution_receipt.json":
                raise OSError("disk full on receipt")
            return real_atomic_json(path, value)

        def flaky_evidence_log(agent, event_type, *args, **kwargs):
            if event_type == "execution_task_completed":
                raise RuntimeError("evidence backend down")
            return Mock()

        with ExitStack() as stack:
            stack.enter_context(patch("execution.worker.claim", return_value=claimed))
            stack.enter_context(patch("execution.worker._read_packet", return_value=packet))
            stack.enter_context(patch("execution.worker.handler_for", return_value=Mock()))
            stack.enter_context(patch("execution.worker.adapter_for", return_value=adapter))
            stack.enter_context(patch(
                "execution.worker.complete", return_value=completed_run
            ))
            stack.enter_context(patch("execution.worker.fail", fail_mock))
            stack.enter_context(patch(
                "execution.worker.get_storage_backend", return_value=store
            ))
            stack.enter_context(patch("execution.worker.refresh_projections"))
            stack.enter_context(patch(
                "execution.worker.EvidenceLogger.log", side_effect=flaky_evidence_log
            ))
            stack.enter_context(patch(
                "execution.worker.atomic_json", side_effect=flaky_atomic_json
            ))
            receipt = execute_task(
                run_path=self.root / "run.json",
                task_id="T001",
                worker_id="execution-test",
                config=config,
            )
        # Caller sees a committed success, not a retryable failure.
        self.assertEqual(receipt["status"], "succeeded")
        warning_steps = {w["step"] for w in receipt["post_commit_warnings"]}
        self.assertIn("execution_receipt", warning_steps)
        self.assertIn("completion_evidence", warning_steps)
        # Formal data is intact and the task was never marked failed.
        self.assertTrue(store.get_state("execution_test")["committed"])
        fail_mock.assert_not_called()
        task_dir = config.task_dir("run-post", "T001", 1)
        self.assertFalse((task_dir / "execution_failure.json").exists())

    def test_complete_failure_compensates_then_closes_orchestrator(self):
        store, fail_mock, evidence_mock, failure = self._complete_failure_case()
        self.assertNotIn("committed_then_closed", store.get_state("execution_test"))
        self.assertNotIn("compensation_error", failure)
        fail_mock.assert_called_once()
        self.assertEqual(evidence_mock.call_args_list[-1].args[1], "execution_task_failed")

    def test_compensation_failure_is_non_retryable_and_preserves_original_error(self):
        store, fail_mock, evidence_mock, failure = self._complete_failure_case(
            rollback_error=RuntimeError("rollback failed")
        )
        self.assertTrue(store.get_state("execution_test")["committed_then_closed"])
        self.assertEqual(failure["code"], "transaction_recovery_unresolved")
        self.assertFalse(failure["retryable"])
        self.assertTrue(failure["integrity_unresolved"])
        self.assertEqual(failure["original_error"]["code"], "RuntimeError")
        self.assertEqual(failure["original_error"]["message"], "complete failed")
        self.assertEqual(failure["compensation_error"]["message"], "rollback failed")
        fail_mock.assert_called_once()
        self.assertEqual(
            fail_mock.call_args.kwargs["error_info"].code,
            "transaction_recovery_unresolved",
        )
        self.assertEqual(evidence_mock.call_args_list[-1].args[1], "execution_task_failed")

    def test_recovery_requires_matching_succeeded_orchestrator_attempt(self):
        context = {
            "task_id": "T001",
            "attempt_id": "T001-A01",
            "metadata": {"orchestrator_run_path": str(self.root / "run.json")},
        }
        snapshot = {
            "run": {
                "tasks": {
                    "T001": {"status": "succeeded", "attempts": 1}
                }
            }
        }
        with patch("agents.orchestrator.status", return_value=snapshot):
            self.assertEqual(_orchestrator_state_for_transaction(context), "closed")
        snapshot["run"]["tasks"]["T001"]["attempts"] = 2
        with patch("agents.orchestrator.status", return_value=snapshot):
            self.assertEqual(_orchestrator_state_for_transaction(context), "open")

    def test_orchestrator_probe_unknown_on_unreadable_snapshot(self):
        context = {
            "task_id": "T001",
            "attempt_id": "T001-A01",
            "metadata": {"orchestrator_run_path": str(self.root / "missing.json")},
        }
        with patch(
            "agents.orchestrator.status",
            side_effect=OSError("transient read failure"),
        ):
            self.assertEqual(_orchestrator_state_for_transaction(context), "unknown")
        # No run path at all is also UNKNOWN, never "open".
        self.assertEqual(
            _orchestrator_state_for_transaction({"task_id": "T001", "metadata": {}}),
            "unknown",
        )


class ArtifactBundleCompletenessTests(unittest.TestCase):
    """_artifact_bundle_complete derives expectations from the protocol."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bundle-complete-test-"))

    def _bundle_path(
        self, *, protocol="current", af2_seeds=(0, 1, 2), include_boltz=True,
        prodigy_count=None, rosetta_count=None,
    ) -> Path:
        predictions = [
            {"predictor": "ColabDesign", "seed": seed} for seed in af2_seeds
        ]
        if include_boltz:
            predictions.append({"predictor": "Boltz", "seed": 101})
        count = len(predictions)
        from prediction_pipeline.protocol import protocol_binding
        if protocol == "current":
            protocol_value = protocol_binding()
        elif protocol == "stale":
            protocol_value = {"name": "old", "version": "old", "sha256": "b" * 64}
        else:
            protocol_value = None
        raw = {
            "schema_version": 1,
            "candidate_id": "C0001",
            "sequence": "ACDE",
            "protocol": protocol_value,
            "global": {
                "monomer_predictions": [{"predictor": "ColabDesign", "seed": 0}],
                "post_relax_pdb": "relax.pdb",
                "post_relax_metadata": "relax.json",
            },
            "targets": {
                "MDM2": {
                    "complex_predictions": predictions,
                    "prodigy_outputs": [{}] * (
                        prodigy_count if prodigy_count is not None else count
                    ),
                    "rosetta_outputs": [{}] * (
                        rosetta_count if rosetta_count is not None else count
                    ),
                }
            },
        }
        path = self.root / "artifacts.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def test_complete_bundle_passes(self):
        self.assertTrue(
            _artifact_bundle_complete(self._bundle_path(), ["MDM2"])
        )

    def test_missing_boltz_not_complete(self):
        self.assertFalse(
            _artifact_bundle_complete(
                self._bundle_path(include_boltz=False), ["MDM2"]
            )
        )

    def test_incomplete_af2_ensemble_not_complete(self):
        self.assertFalse(
            _artifact_bundle_complete(
                self._bundle_path(af2_seeds=(0, 1)), ["MDM2"]
            )
        )

    def test_stale_protocol_not_reusable(self):
        self.assertFalse(
            _artifact_bundle_complete(
                self._bundle_path(protocol="stale"), ["MDM2"]
            )
        )

    def test_legacy_bundle_not_reusable(self):
        self.assertFalse(
            _artifact_bundle_complete(
                self._bundle_path(protocol=None), ["MDM2"]
            )
        )

    def test_short_prodigy_cover_not_complete(self):
        self.assertFalse(
            _artifact_bundle_complete(
                self._bundle_path(prodigy_count=1), ["MDM2"]
            )
        )

    def test_expanded_protocol_ensemble_invalidates_old_bundle(self):
        # Protocol ensemble grows 3 -> 4: a bundle produced under the old
        # 3-member protocol must be judged incomplete (not silently reused).
        from unittest.mock import patch
        from execution import handlers as handlers_module
        expanded = {
            "seeds": [0, 1, 2, 3],
            "model_numbers": [0, 1, 2, 3],
            "num_recycles": 3,
        }
        with patch.object(handlers_module, "_AF2_PRODIGY_PROTOCOL", expanded):
            self.assertFalse(
                _artifact_bundle_complete(self._bundle_path(), ["MDM2"])
            )


if __name__ == "__main__":
    unittest.main()

