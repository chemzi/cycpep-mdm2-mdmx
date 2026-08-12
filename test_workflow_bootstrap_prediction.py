"""Launcher acceptance tests for approval-gated initial Prediction."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import nullcontext

from core.context import ProjectContext
from workflow.boundaries import FormalBoundary
from workflow.diagnostics import DiagnosticStore
from workflow.service import (
    LauncherServiceDependencies,
    launch_project,
    resume_launcher_run,
    status_launcher_run,
)


LAUNCHER_ID = "launcher_0123456789abcdef0123456789abcdef"


def _context():
    return ProjectContext(project_id="project-1", config={
        "project_id": "project-1",
        "targets": [{"id": "TARGET"}],
        "review": {"status": "approved", "approved_digest": "approved", "content_digest": "approved"},
    })


def _bootstrap_plan(retry_index=0):
    plan = {
        "plan_id": f"planner_bootstrap{retry_index + 1}",
        "workflow_id": "workflow-bootstrap",
        "source": {"kind": "initial_prediction_bootstrap"},
        "approval_request": {"required_task_ids": ["T001"]},
        "tasks": [{"task_id": "T001"}],
    }
    if retry_index:
        plan["source"]["retry"] = {"retry_index": retry_index}
    return plan


def _regular_plan():
    return {
        "plan_id": "planner_regular1",
        "workflow_id": "workflow-regular",
        "source": {"critic_report_id": "critic-1"},
        "approval_request": {"required_task_ids": ["T002"]},
        "tasks": [{"task_id": "T002"}],
    }


class _World:
    def __init__(self):
        self.research = "not_started"
        self.design = "not_started"
        self.bootstrap_plan = "not_started"
        self.bootstrap_run = "not_started"
        self.bootstrap_run_status = "ready"
        self.prediction = "not_started"
        self.critic = "not_started"
        self.regular_plan = "not_started"
        self.direct_prediction = "not_started"
        self.bootstrap_prediction_blocker = None
        self.calls = []
        self.inspect_critic_calls = 0
        self.inspected_prediction_run_ids = []
        self.fail_prediction = False
        self.retry_index = 0


class _Runtime:
    prediction_invocation_id = "prediction_invocation_0123456789abcdef0123456789abcdef"
    prediction_run_id = "prediction_0123456789abcdef0123456789abcdef"

    def __init__(self, world): self.world = world
    def inspect_research(self):
        return FormalBoundary.not_started("research") if self.world.research == "not_started" else FormalBoundary.completed("research", completion_event_id="research-complete")
    def run_research(self): self.world.calls.append("research"); self.world.research = "completed"
    def inspect_design(self):
        return FormalBoundary.not_started("design") if self.world.design == "not_started" else FormalBoundary.completed("design", completion_event_id="design-complete", transaction_id="tx-design", candidate_ids=("C0001", "C0002"))
    def run_design(self): self.world.calls.append("design"); self.world.design = "completed"
    def inspect_prediction(self):
        if self.world.direct_prediction == "blocked":
            return FormalBoundary.blocked(
                "prediction",
                "prediction_execution_incomplete",
                "existing direct Prediction is pending",
            )
        if self.world.direct_prediction == "completed":
            return FormalBoundary.completed(
                "prediction",
                prediction_run_id="prediction-direct",
                handoff_path="direct-handoff.json",
            )
        return FormalBoundary.not_started("prediction")
    def run_prediction(self, _candidate_ids): self.world.calls.append("direct_prediction")
    def inspect_bootstrap_planner(self, _design):
        if self.world.bootstrap_plan == "not_started": return FormalBoundary.not_started("planner")
        plan = _bootstrap_plan(self.world.retry_index)
        return FormalBoundary.completed("planner", plan_id=plan["plan_id"], plan_path="bootstrap.json", plan_sha256="digest", plan_document=plan)
    def run_bootstrap_planner(self, _research, _design): self.world.calls.append("bootstrap_plan"); self.world.bootstrap_plan = "completed"
    def inspect_approvals(self, _planner): return FormalBoundary.not_started("approval")
    def inspect_orchestrator(self, plan):
        if plan["source"].get("kind") == "initial_prediction_bootstrap":
            if self.world.bootstrap_run == "not_started": return FormalBoundary.not_started("orchestrator")
            return FormalBoundary.completed("orchestrator", run_id=f"orchestrator-bootstrap-{self.world.retry_index}", workflow_id="workflow-bootstrap", plan_id=plan["plan_id"], run_path="bootstrap-run.json", formal_status=self.world.bootstrap_run_status, summary={})
        return FormalBoundary.not_started("orchestrator")
    def initialize_orchestrator(self, plan_path, approvals):
        self.world.calls.append(("initialize_bootstrap", tuple(approvals)))
        self.world.bootstrap_run = "completed"
    def inspect_transaction_recovery(self, _orchestrator): return FormalBoundary.completed("transaction")
    def recover_transactions(self): return None
    def drain(self, _run_path):
        self.world.calls.append("worker_prediction")
        if self.world.fail_prediction:
            self.world.bootstrap_run_status = "failed"
            raise RuntimeError("tool preflight failed")
        self.world.bootstrap_run_status = "completed"; self.world.prediction = "completed"
    def inspect_bootstrap_prediction(self, _plan, _orchestrator):
        if self.world.bootstrap_prediction_blocker:
            return FormalBoundary.blocked(
                "prediction",
                self.world.bootstrap_prediction_blocker,
                "Prediction owner readiness blocked",
            )
        if self.world.prediction != "completed": return FormalBoundary.not_started("prediction")
        return FormalBoundary.completed("prediction", prediction_run_id="prediction-domain", handoff_path="handoff.json")
    def inspect_critic(self, prediction):
        self.world.inspect_critic_calls += 1
        self.world.inspected_prediction_run_ids.append(
            prediction.references.get("prediction_run_id")
        )
        if self.world.critic == "not_started": return FormalBoundary.not_started("critic")
        return FormalBoundary.completed("critic", report_id="critic-1", report_path="critic.json")
    def run_critic(self, _handoff): self.world.calls.append("critic"); self.world.critic = "completed"
    def inspect_planner(self, _critic):
        if self.world.regular_plan == "not_started": return FormalBoundary.not_started("planner")
        return FormalBoundary.completed("planner", plan_id="planner_regular1", plan_path="regular.json", plan_sha256="digest", plan_document=_regular_plan())
    def run_planner(self, _report): self.world.calls.append("regular_plan"); self.world.regular_plan = "completed"
    def inspect_execution_failure(self, _orchestrator):
        if self.world.bootstrap_run_status != "failed": return FormalBoundary.not_started("execution")
        suffix = self.world.retry_index
        return FormalBoundary.completed("execution", evidence_id=f"failure-{suffix}", plan_id=_bootstrap_plan(suffix)["plan_id"], run_id=f"orchestrator-bootstrap-{suffix}", task_id="T001", attempt_id="T001-A01", transaction_id=f"tx-failed-{suffix}", formal_status="failed")
    def retry_bootstrap_prediction(self, _plan, _failure):
        self.world.calls.append("retry_plan")
        self.world.retry_index += 1
        self.world.bootstrap_run = "not_started"
        self.world.bootstrap_run_status = "ready"


def _deps(root, world):
    return LauncherServiceDependencies(
        diagnostics=DiagnosticStore(root), load_context=lambda _path: _context(),
        validate_project=lambda _value: None, bind_context=lambda _value: nullcontext(),
        runtime_factory=lambda *_args: _Runtime(world), launcher_id=lambda: LAUNCHER_ID,
    )


class WorkflowBootstrapPredictionTests(unittest.TestCase):
    def test_design_completion_creates_plan_and_waits_without_direct_prediction(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            result = launch_project(project_path="project.json", dependencies=_deps(tmp, world))
            self.assertEqual(result.payload.status, "awaiting_approval")
            self.assertEqual(result.payload.required_task_ids, ("T001",))
            self.assertEqual(world.calls, ["research", "design", "bootstrap_plan"])
            self.assertNotIn("direct_prediction", world.calls)
            before = list(world.calls)
            observed = status_launcher_run(launcher_run_id=LAUNCHER_ID, dependencies=_deps(tmp, world))
            self.assertEqual(observed.payload.status, "awaiting_approval")
            self.assertEqual(world.calls, before)

    def test_terminal_failure_requires_explicit_new_plan_and_new_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World(); world.fail_prediction = True; deps = _deps(tmp, world)
            launch_project(project_path="project.json", dependencies=deps)
            failed = resume_launcher_run(launcher_run_id=LAUNCHER_ID, approval_paths=("approval.json",), dependencies=deps)
            self.assertEqual(failed.payload.status, "failed")
            calls_after_failure = list(world.calls)

            ordinary = resume_launcher_run(launcher_run_id=LAUNCHER_ID, dependencies=deps)
            observed = status_launcher_run(launcher_run_id=LAUNCHER_ID, dependencies=deps)
            self.assertEqual(ordinary.payload.status, "failed")
            self.assertEqual(observed.payload.status, "failed")
            self.assertEqual(world.calls, calls_after_failure)

            retried = resume_launcher_run(
                launcher_run_id=LAUNCHER_ID,
                retry_bootstrap_prediction=True,
                dependencies=deps,
            )
            self.assertEqual(retried.payload.status, "awaiting_approval")
            self.assertEqual(retried.payload.required_task_ids, ("T001",))
            self.assertEqual(world.calls[-1], "retry_plan")
            self.assertEqual(world.calls.count("research"), 1)
            self.assertEqual(world.calls.count("design"), 1)
            self.assertEqual(world.calls.count("worker_prediction"), 1)

            repeated = resume_launcher_run(
                launcher_run_id=LAUNCHER_ID,
                retry_bootstrap_prediction=True,
                dependencies=deps,
            )
            self.assertEqual(repeated.payload.status, "awaiting_approval")
            self.assertEqual(world.calls.count("retry_plan"), 1)

    def test_retry_request_rejects_non_failed_execution_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World(); deps = _deps(tmp, world)
            launch_project(project_path="project.json", dependencies=deps)
            before = list(world.calls)

            result = resume_launcher_run(
                launcher_run_id=LAUNCHER_ID,
                retry_bootstrap_prediction=True,
                dependencies=deps,
            )

            self.assertEqual(result.payload.status, "blocked")
            self.assertEqual(
                result.payload.error.code, "bootstrap_retry_not_terminal_failed"
            )
            self.assertEqual(world.calls, before)

    def test_multiple_failed_retries_preserve_design_and_eventually_advance(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World(); world.fail_prediction = True; deps = _deps(tmp, world)
            launch_project(project_path="project.json", dependencies=deps)
            resume_launcher_run(
                launcher_run_id=LAUNCHER_ID,
                approval_paths=("approval-0.json",), dependencies=deps,
            )
            resume_launcher_run(
                launcher_run_id=LAUNCHER_ID,
                retry_bootstrap_prediction=True, dependencies=deps,
            )
            resume_launcher_run(
                launcher_run_id=LAUNCHER_ID,
                approval_paths=("approval-1.json",), dependencies=deps,
            )
            second_retry = resume_launcher_run(
                launcher_run_id=LAUNCHER_ID,
                retry_bootstrap_prediction=True, dependencies=deps,
            )
            self.assertEqual(second_retry.payload.status, "awaiting_approval")
            self.assertEqual(world.retry_index, 2)

            world.fail_prediction = False
            completed = resume_launcher_run(
                launcher_run_id=LAUNCHER_ID,
                approval_paths=("approval-2.json",), dependencies=deps,
            )
            self.assertEqual(completed.payload.status, "awaiting_approval")
            self.assertEqual(world.calls.count("research"), 1)
            self.assertEqual(world.calls.count("design"), 1)
            self.assertEqual(world.calls.count("worker_prediction"), 3)
            self.assertEqual(world.calls.count("retry_plan"), 2)

    def test_approval_runs_worker_then_owner_readiness_then_critic(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World(); deps = _deps(tmp, world)
            launch_project(project_path="project.json", dependencies=deps)
            result = resume_launcher_run(launcher_run_id=LAUNCHER_ID, approval_paths=("approval.json",), dependencies=deps)
            self.assertEqual(result.payload.status, "awaiting_approval")
            self.assertEqual(result.payload.required_task_ids, ("T002",))
            self.assertEqual(world.calls, [
                "research", "design", "bootstrap_plan",
                ("initialize_bootstrap", ("approval.json",)), "worker_prediction",
                "critic", "regular_plan",
            ])
            self.assertLess(world.calls.index("worker_prediction"), world.calls.index("critic"))
            self.assertNotIn("direct_prediction", world.calls)
            self.assertEqual(
                set(world.inspected_prediction_run_ids), {"prediction-domain"}
            )

    def test_direct_and_bootstrap_authorities_conflict_before_critic(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            world.design = "completed"
            world.direct_prediction = "completed"
            world.bootstrap_plan = "completed"

            result = launch_project(
                project_path="project.json", dependencies=_deps(tmp, world)
            )

            self.assertEqual(result.payload.status, "blocked")
            self.assertEqual(
                result.payload.error.code, "prediction_authority_conflict"
            )
            self.assertEqual(world.inspect_critic_calls, 0)

    def test_existing_direct_pending_run_stays_legacy_and_creates_no_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            world.direct_prediction = "blocked"

            result = launch_project(
                project_path="project.json", dependencies=_deps(tmp, world)
            )

            self.assertEqual(result.payload.status, "blocked")
            self.assertEqual(
                result.payload.error.code, "prediction_execution_incomplete"
            )
            self.assertNotIn("bootstrap_plan", world.calls)
            self.assertNotIn("direct_prediction", world.calls)
            self.assertEqual(world.inspect_critic_calls, 0)

    def test_pending_owner_record_blocks_critic_after_worker_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World(); deps = _deps(tmp, world)
            world.bootstrap_prediction_blocker = "prediction_execution_incomplete"
            launch_project(project_path="project.json", dependencies=deps)

            result = resume_launcher_run(
                launcher_run_id=LAUNCHER_ID,
                approval_paths=("approval.json",), dependencies=deps,
            )

            self.assertEqual(result.payload.status, "blocked")
            self.assertEqual(
                result.payload.error.code, "prediction_execution_incomplete"
            )
            self.assertEqual(world.inspect_critic_calls, 0)


if __name__ == "__main__": unittest.main()
