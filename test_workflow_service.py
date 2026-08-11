"""Acceptance and crash-recovery tests for the Launcher service."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path

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


def _context(binding="approved-content", project_id="project-1"):
    return ProjectContext(
        project_id=project_id,
        config={
            "project_id": project_id,
            "targets": [{"id": "TARGET"}],
            "review": {
                "status": "approved",
                "approved_digest": binding,
                "content_digest": binding,
            },
        },
    )


class _World:
    def __init__(self):
        self.statuses = {
            key: "not_started"
            for key in ("research", "design", "prediction", "critic", "planner", "orchestrator")
        }
        self.calls = []
        self.fail_at = None
        self.blocker_codes = {
            boundary: f"{boundary}_recovery_ambiguous"
            for boundary in self.statuses
        }
        self.orchestrator_status = "ready"
        self.required_task_ids = ("task-1",)
        self.approvals = FormalBoundary.not_started("approval")
        self.transaction = FormalBoundary.completed("transaction")


class _Runtime:
    prediction_invocation_id = "prediction_invocation_0123456789abcdef0123456789abcdef"
    prediction_run_id = "prediction_0123456789abcdef0123456789abcdef"

    def __init__(self, world):
        self.world = world

    def _inspect(self, boundary, **refs):
        status = self.world.statuses[boundary]
        if status == "blocked":
            return FormalBoundary.blocked(
                boundary, self.world.blocker_codes[boundary], "partial formal state"
            )
        if status == "not_started":
            return FormalBoundary.not_started(boundary)
        return FormalBoundary.completed(boundary, **refs)

    def _run(self, boundary):
        self.world.calls.append(boundary)
        if self.world.fail_at == boundary:
            raise RuntimeError(f"{boundary} failed")
        self.world.statuses[boundary] = "completed"

    def inspect_research(self):
        return self._inspect(
            "research", completion_event_id="research-complete", evidence_ids=("research-evidence",)
        )

    def run_research(self):
        self._run("research")

    def inspect_design(self):
        return self._inspect(
            "design",
            completion_event_id="design-complete",
            candidate_ids=("candidate-1",),
            artifact_ids=("artifact-1",),
        )

    def run_design(self):
        self._run("design")

    def inspect_prediction(self):
        return self._inspect(
            "prediction",
            prediction_invocation_id=self.prediction_invocation_id,
            prediction_run_id=self.prediction_run_id,
            completion_event_id="prediction-complete",
            handoff_path="C:/internal/prediction_handoff.json",
            run_root="C:/internal/prediction-root",
        )

    def run_prediction(self, candidate_ids):
        self.world.calls.append(("prediction_candidates", candidate_ids))
        self._run("prediction")

    def inspect_critic(self, prediction):
        return self._inspect(
            "critic", report_id="critic-1", report_path="C:/internal/critic.json"
        )

    def run_critic(self, handoff_path):
        self._run("critic")

    def inspect_planner(self, critic):
        plan = {
            "plan_id": "plan-1",
            "workflow_id": "workflow-1",
            "approval_request": {"required_task_ids": list(self.world.required_task_ids)},
        }
        return self._inspect(
            "planner",
            plan_id="plan-1",
            plan_path="C:/internal/plan.json",
            plan_sha256="plan-digest",
            plan_document=plan,
        )

    def run_planner(self, report_path):
        self._run("planner")

    def inspect_approvals(self, planner):
        return self.world.approvals

    def inspect_orchestrator(self, plan):
        if self.world.fail_at == "orchestrator_status":
            return FormalBoundary.blocked(
                "orchestrator", "orchestrator_status_unavailable", "status unavailable"
            )
        return self._inspect(
            "orchestrator",
            run_id="run-1",
            workflow_id="workflow-1",
            plan_id="plan-1",
            run_path="C:/internal/orchestrator.json",
            formal_status=self.world.orchestrator_status,
            summary={"task_status_counts": {"succeeded": 1}},
        )

    def initialize_orchestrator(self, plan_path, approvals):
        self.world.calls.append(("initialize", tuple(approvals)))
        if self.world.fail_at == "approval":
            raise RuntimeError("approval rejected")
        if self.world.fail_at == "orchestrator":
            raise RuntimeError("orchestrator failed")
        self.world.statuses["orchestrator"] = "completed"

    def inspect_execution_failure(self, orchestrator):
        if self.world.fail_at != "execution":
            return FormalBoundary.not_started("execution")
        return FormalBoundary.completed(
            "execution",
            evidence_id="execution-failure-evidence",
            run_id="run-1",
            workflow_id="workflow-1",
            plan_id="plan-1",
            task_id="task-1",
            attempt_id="attempt-1",
            transaction_id="transaction-1",
            formal_status="failed",
        )

    def recover_transactions(self):
        return None

    def inspect_transaction_recovery(self, _orchestrator=None):
        return self.world.transaction

    def drain(self, run_path):
        self.world.calls.append("execution")
        if self.world.fail_at == "execution":
            self.world.orchestrator_status = "failed"
            raise RuntimeError("worker failed")
        self.world.orchestrator_status = "completed"


def _dependencies(root, world, *, writer=None, context_loader=None):
    diagnostics = DiagnosticStore(root, **({"durable_writer": writer} if writer else {}))
    return LauncherServiceDependencies(
        diagnostics=diagnostics,
        load_context=context_loader or (lambda _path: _context()),
        validate_project=lambda _config: None,
        bind_context=lambda _context: nullcontext(),
        runtime_factory=lambda _context, _launcher_id: _Runtime(world),
        launcher_id=lambda: LAUNCHER_ID,
    )


class WorkflowServiceAcceptanceTests(unittest.TestCase):
    def test_initial_diagnostic_failure_constructs_no_runtime_or_science(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            runtimes = []

            def fail_create(_path, _value):
                raise OSError("disk unavailable")

            deps = _dependencies(tmp, world, writer=fail_create)
            deps = LauncherServiceDependencies(
                **{
                    **deps.__dict__,
                    "runtime_factory": lambda *_args: runtimes.append(True),
                }
            )

            result = launch_project(project_path="approved.json", dependencies=deps)

            self.assertEqual(result.exit_code, 2)
            self.assertEqual(result.payload.error.code, "launcher_diagnostic_persistence_failed")
            self.assertEqual(result.payload.launcher_run_id, LAUNCHER_ID)
            self.assertEqual(runtimes, [])
            self.assertEqual(world.calls, [])

    def test_happy_path_stops_at_explicit_approval_and_passes_design_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            deps = _dependencies(tmp, world)

            result = launch_project(project_path="approved.json", dependencies=deps)

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.payload.status, "awaiting_approval")
            self.assertEqual(result.payload.required_task_ids, ("task-1",))
            self.assertEqual(
                world.calls,
                [
                    "research", "design", ("prediction_candidates", ("candidate-1",)),
                    "prediction", "critic", "planner",
                ],
            )
            self.assertIsNone(result.payload.formal_trace.run_id)
            self.assertNotIn("internal", str(result.payload.to_dict()))

    def test_valid_approval_continues_through_worker_and_repeated_resume_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            deps = _dependencies(tmp, world)
            first = launch_project(project_path="approved.json", dependencies=deps)
            self.assertEqual(first.payload.status, "awaiting_approval")

            resumed = resume_launcher_run(
                launcher_run_id=LAUNCHER_ID,
                approval_paths=("approval.json",),
                dependencies=deps,
            )
            calls_after_completion = list(world.calls)
            repeated = resume_launcher_run(
                launcher_run_id=LAUNCHER_ID, dependencies=deps
            )

            self.assertEqual(resumed.payload.status, "completed")
            self.assertEqual(resumed.payload.formal_trace.run_id, "run-1")
            self.assertEqual(repeated.payload.status, "completed")
            self.assertEqual(world.calls, calls_after_completion)

    def test_status_is_read_only_and_diagnostic_completion_claim_is_not_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            deps = _dependencies(tmp, world)
            launch_project(project_path="approved.json", dependencies=deps)
            for boundary in ("research", "design", "prediction", "critic", "planner"):
                world.statuses[boundary] = "not_started"
            deps.diagnostics.write(
                deps.diagnostics.read(LAUNCHER_ID).with_observation(
                    last_completed_boundary="execution", last_known_formal_status="completed"
                )
            )
            world.calls.clear()

            result = status_launcher_run(launcher_run_id=LAUNCHER_ID, dependencies=deps)

            self.assertEqual(result.payload.status, "pending")
            self.assertEqual(result.payload.boundary, "research")
            self.assertEqual(world.calls, [])

    def test_formal_downstream_state_wins_over_earlier_receipt_ambiguity(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            deps = _dependencies(tmp, world)
            launch_project(project_path="approved.json", dependencies=deps)
            world.statuses["research"] = "blocked"
            world.statuses["design"] = "blocked"
            world.statuses["orchestrator"] = "completed"
            world.orchestrator_status = "completed"
            world.calls.clear()

            result = resume_launcher_run(launcher_run_id=LAUNCHER_ID, dependencies=deps)

            self.assertEqual(result.payload.status, "completed")
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(world.calls, [])

    def test_each_scientific_boundary_failure_is_fail_fast(self):
        for failed in ("research", "design", "prediction", "critic", "planner"):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as tmp:
                world = _World()
                world.fail_at = failed
                deps = _dependencies(tmp, world)

                result = launch_project(project_path="approved.json", dependencies=deps)

                self.assertEqual(result.exit_code, 2)
                self.assertEqual(result.payload.error.component, failed)
                calls = [item for item in world.calls if isinstance(item, str)]
                self.assertEqual(calls[-1], failed)

    def test_partial_design_or_prediction_fails_closed_without_scientific_call(self):
        for boundary in ("design", "prediction"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                world = _World()
                world.statuses["research"] = "completed"
                world.statuses[boundary] = "blocked"
                deps = _dependencies(tmp, world)

                result = launch_project(project_path="approved.json", dependencies=deps)

                self.assertEqual(result.exit_code, 3)
                self.assertEqual(result.payload.error.code, f"{boundary}_recovery_ambiguous")
                self.assertNotIn(boundary, world.calls)

    def test_owner_blockers_are_identical_across_launch_status_and_resume(self):
        cases = (
            ("design", "initial_design_no_valid_candidates"),
            ("design", "initial_design_scientific_tool_failed"),
            ("prediction", "prediction_execution_incomplete"),
        )
        for boundary, code in cases:
            with self.subTest(boundary=boundary, code=code), tempfile.TemporaryDirectory() as tmp:
                world = _World()
                world.statuses["research"] = "completed"
                if boundary == "prediction":
                    world.statuses["design"] = "completed"
                world.statuses[boundary] = "blocked"
                world.blocker_codes[boundary] = code
                deps = _dependencies(tmp, world)

                launched = launch_project(project_path="approved.json", dependencies=deps)
                calls_after_launch = list(world.calls)
                observed = status_launcher_run(
                    launcher_run_id=LAUNCHER_ID, dependencies=deps
                )
                resumed = resume_launcher_run(
                    launcher_run_id=LAUNCHER_ID, dependencies=deps
                )

                for result in (launched, observed, resumed):
                    self.assertEqual(result.payload.status, "blocked")
                    self.assertEqual(result.payload.boundary, boundary)
                    self.assertEqual(result.payload.error.code, code)
                self.assertEqual(world.calls, calls_after_launch)
                self.assertNotIn("critic", world.calls)

    def test_invalid_approval_and_orchestrator_status_failure_never_reach_worker(self):
        for failure in ("approval", "orchestrator", "orchestrator_status"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                world = _World()
                deps = _dependencies(tmp, world)
                launch_project(project_path="approved.json", dependencies=deps)
                world.fail_at = failure
                world.calls.clear()

                result = resume_launcher_run(
                    launcher_run_id=LAUNCHER_ID,
                    approval_paths=("approval.json",),
                    dependencies=deps,
                )

                self.assertNotEqual(result.payload.status, "completed")
                self.assertNotIn("execution", world.calls)

    def test_worker_failure_projects_formal_trace_and_recovery_error_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            deps = _dependencies(tmp, world)
            launch_project(project_path="approved.json", dependencies=deps)
            world.fail_at = "execution"

            result = resume_launcher_run(
                launcher_run_id=LAUNCHER_ID,
                approval_paths=("approval.json",),
                dependencies=deps,
            )

            self.assertEqual(result.exit_code, 2)
            self.assertEqual(result.payload.error.component, "execution")
            self.assertEqual(result.payload.formal_trace.task_id, "task-1")
            self.assertEqual(result.payload.formal_trace.attempt_id, "attempt-1")
            self.assertEqual(result.payload.formal_trace.transaction_id, "transaction-1")
            self.assertIn("execution-failure-evidence", result.payload.evidence_ids)

        class RecoveryBlocked(RuntimeError):
            code = "transaction_recovery_unresolved"
            unresolved = ("transaction-recovery-1",)

        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            deps = _dependencies(tmp, world)
            launch_project(project_path="approved.json", dependencies=deps)
            world.transaction = FormalBoundary.blocked(
                "transaction",
                "transaction_recovery_unresolved",
                "formal transaction recovery requires operator action",
                transaction_id="transaction-recovery-1",
            )
            runtime = _Runtime(world)
            runtime.recover_transactions = lambda: (_ for _ in ()).throw(
                RecoveryBlocked("blocked")
            )
            deps = LauncherServiceDependencies(
                **{**deps.__dict__, "runtime_factory": lambda *_args: runtime}
            )

            result = resume_launcher_run(
                launcher_run_id=LAUNCHER_ID,
                approval_paths=("approval.json",),
                dependencies=deps,
            )

            self.assertEqual(result.exit_code, 3)
            self.assertEqual(result.payload.status, "blocked")
            self.assertEqual(result.payload.error.code, "transaction_recovery_unresolved")
            self.assertEqual(
                result.payload.formal_trace.transaction_id,
                "transaction-recovery-1",
            )
            self.assertNotIn("execution", world.calls)

    def test_read_only_status_projects_every_formal_orchestrator_outcome(self):
        expected = {
            "completed": 0,
            "completed_required": 0,
            "blocked": 3,
            "failed": 2,
            "awaiting_approval": 0,
            "ready": 0,
            "running": 0,
            "pending": 0,
        }
        for formal_status, exit_code in expected.items():
            with self.subTest(formal_status=formal_status), tempfile.TemporaryDirectory() as tmp:
                world = _World()
                deps = _dependencies(tmp, world)
                launch_project(project_path="approved.json", dependencies=deps)
                world.statuses["orchestrator"] = "completed"
                world.orchestrator_status = formal_status
                world.calls.clear()

                result = status_launcher_run(launcher_run_id=LAUNCHER_ID, dependencies=deps)

                self.assertEqual(result.payload.status, formal_status)
                self.assertEqual(result.exit_code, exit_code)
                self.assertEqual(result.payload.task_status_counts, {"succeeded": 1})
                self.assertEqual(world.calls, [])


    def test_changed_project_or_approved_content_blocks_before_formal_inspection(self):
        for context in (_context(project_id="project-2"), _context(binding="changed")):
            with self.subTest(context=context.project_id), tempfile.TemporaryDirectory() as tmp:
                world = _World()
                current = [_context()]
                deps = _dependencies(tmp, world, context_loader=lambda _path: current[0])
                launch_project(project_path="approved.json", dependencies=deps)
                world.calls.clear()
                current[0] = context

                result = resume_launcher_run(launcher_run_id=LAUNCHER_ID, dependencies=deps)

                self.assertEqual(result.exit_code, 2)
                self.assertEqual(world.calls, [])
                self.assertIn(result.payload.error.code, {
                    "launcher_project_binding_changed", "launcher_approved_content_changed"
                })

    def test_formal_completion_survives_following_diagnostic_write_failure(self):
        # Initial creation writes the runtime-locator sidecar before the journal.
        for boundary, failed_write in (("research", 3), ("design", 4), ("prediction", 5)):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                writes = [0]

                def writer(path, value):
                    from execution.supervisor import durable_atomic_json

                    writes[0] += 1
                    if writes[0] == failed_write:
                        raise OSError("injected bookkeeping failure")
                    durable_atomic_json(path, value)

                world = _World()
                deps = _dependencies(tmp, world, writer=writer)
                failed = launch_project(project_path="approved.json", dependencies=deps)
                invoked_before = world.calls.count(boundary)
                resumed = resume_launcher_run(launcher_run_id=LAUNCHER_ID, dependencies=deps)

                self.assertEqual(failed.exit_code, 2)
                self.assertEqual(world.calls.count(boundary), invoked_before)
                self.assertEqual(resumed.payload.status, "awaiting_approval")

    def test_post_drain_diagnostic_failure_resume_does_not_drain_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            failed_once = [False]

            def writer(path, value):
                from execution.supervisor import durable_atomic_json

                if value.get("last_completed_boundary") == "execution" and not failed_once[0]:
                    failed_once[0] = True
                    raise OSError("post-drain bookkeeping failed")
                durable_atomic_json(path, value)

            world = _World()
            deps = _dependencies(tmp, world, writer=writer)
            launch_project(project_path="approved.json", dependencies=deps)
            failed = resume_launcher_run(
                launcher_run_id=LAUNCHER_ID,
                approval_paths=("approval.json",),
                dependencies=deps,
            )
            drains = world.calls.count("execution")
            resumed = resume_launcher_run(launcher_run_id=LAUNCHER_ID, dependencies=deps)

            self.assertEqual(failed.exit_code, 2)
            self.assertEqual(resumed.payload.status, "completed")
            self.assertEqual(world.calls.count("execution"), drains)
            repaired = deps.diagnostics.read(LAUNCHER_ID)
            self.assertEqual(repaired.last_completed_boundary, "execution")
            self.assertEqual(repaired.last_known_formal_status, "completed")
            self.assertEqual(repaired.formal_trace.run_id, "run-1")

    def test_repeated_blocked_and_approval_waiting_resume_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            deps = _dependencies(tmp, world)
            launch_project(project_path="approved.json", dependencies=deps)
            calls = list(world.calls)
            for _ in range(2):
                result = resume_launcher_run(launcher_run_id=LAUNCHER_ID, dependencies=deps)
                self.assertEqual(result.payload.status, "awaiting_approval")
            self.assertEqual(world.calls, calls)

        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            world.statuses["research"] = "completed"
            world.statuses["design"] = "blocked"
            deps = _dependencies(tmp, world)
            launch_project(project_path="approved.json", dependencies=deps)
            calls = list(world.calls)
            for _ in range(2):
                result = resume_launcher_run(launcher_run_id=LAUNCHER_ID, dependencies=deps)
                self.assertEqual(result.payload.status, "blocked")
            self.assertEqual(world.calls, calls)

        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            deps = _dependencies(tmp, world)
            launch_project(project_path="approved.json", dependencies=deps)
            world.statuses["orchestrator"] = "completed"
            world.orchestrator_status = "failed"
            world.calls.clear()
            for _ in range(2):
                result = resume_launcher_run(launcher_run_id=LAUNCHER_ID, dependencies=deps)
                self.assertEqual(result.payload.status, "failed")
            self.assertEqual(world.calls, [])

    def test_edited_diagnostic_prediction_locator_never_selects_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            from workflow.models import PredictionRunLocator

            world = _World()
            deps = _dependencies(tmp, world)
            launch_project(project_path="approved.json", dependencies=deps)
            report = deps.diagnostics.read(LAUNCHER_ID).with_observation(
                prediction_run_locator=PredictionRunLocator(
                    root="Z:/stale/attacker-selected", run_id=_Runtime.prediction_run_id
                )
            )
            deps.diagnostics.write(report)
            world.calls.clear()

            result = resume_launcher_run(launcher_run_id=LAUNCHER_ID, dependencies=deps)

            self.assertEqual(result.payload.status, "awaiting_approval")
            self.assertFalse(any(
                item == "prediction" or (
                    isinstance(item, tuple) and item[0] == "prediction_candidates"
                )
                for item in world.calls
            ))


if __name__ == "__main__":
    unittest.main()
