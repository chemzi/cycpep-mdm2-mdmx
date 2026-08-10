"""Characterization tests for PR #62 merge-blocker authority contracts."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace

from test_workflow_service import LAUNCHER_ID, _Runtime, _World, _dependencies
from workflow.boundaries import FormalBoundary
from workflow.errors import StructuredError
from workflow.models import DiagnosticReport, FormalTrace
from workflow.observations import with_plan_trace
from workflow.service import (
    LauncherServiceDependencies,
    launch_project,
    resume_launcher_run,
    status_launcher_run,
)


class _TracingRuntime(_Runtime):
    def inspect_critic(self, prediction):
        self.world.calls.append("inspect_critic")
        return super().inspect_critic(prediction)

    def inspect_planner(self, critic):
        self.world.calls.append("inspect_planner")
        return super().inspect_planner(critic)

    def inspect_transaction_recovery(self):
        self.world.calls.append("inspect_transaction_recovery")
        return self.world.transaction


def _tracing_dependencies(root, world):
    deps = _dependencies(root, world)
    return LauncherServiceDependencies(
        **{
            **deps.__dict__,
            "runtime_factory": lambda *_args: _TracingRuntime(world),
        }
    )


class WorkflowMergeBlockerCharacterizationTests(unittest.TestCase):
    def test_prediction_blocker_precedes_stale_critic_and_planner(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            deps = _tracing_dependencies(tmp, world)
            launch_project(project_path="approved.json", dependencies=deps)
            world.statuses.update(
                prediction="blocked", critic="completed", planner="completed"
            )
            world.calls.clear()

            result = resume_launcher_run(
                launcher_run_id=LAUNCHER_ID, dependencies=deps
            )

            self.assertEqual(result.exit_code, 3)
            self.assertEqual(result.payload.error.code, "prediction_recovery_ambiguous")
            self.assertNotIn("inspect_critic", world.calls)
            self.assertNotIn("inspect_planner", world.calls)

    def test_prediction_partial_state_precedes_old_downstream_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            deps = _tracing_dependencies(tmp, world)
            launch_project(project_path="approved.json", dependencies=deps)
            world.statuses.update(
                prediction="blocked", critic="completed", planner="completed"
            )
            world.calls.clear()

            result = status_launcher_run(
                launcher_run_id=LAUNCHER_ID, dependencies=deps
            )

            self.assertEqual(result.payload.error.code, "prediction_recovery_ambiguous")
            self.assertEqual(world.calls, [])

    def test_observation_preserves_failure_until_explicit_clear(self):
        report = DiagnosticReport.initial(
            launcher_run_id=LAUNCHER_ID,
            project_id="project-1",
            approved_content_binding="approved-content",
            project_locator="approved.json",
        ).with_failure(
            boundary="execution",
            error=StructuredError(
                code="worker_failed", component="execution", message="failed"
            ),
        )

        observed = report.with_observation(last_known_formal_status="failed")

        self.assertEqual(observed.failed_boundary, "execution")
        self.assertEqual(observed.failure.code, "worker_failed")
        self.assertTrue(hasattr(observed, "clear_failure"))

    def test_plan_trace_merge_preserves_task_attempt_and_transaction(self):
        report = DiagnosticReport.initial(
            launcher_run_id=LAUNCHER_ID,
            project_id="project-1",
            approved_content_binding="approved-content",
            project_locator="approved.json",
        )
        report = replace(
            report,
            formal_trace=FormalTrace(
                workflow_id="workflow-1",
                run_id="run-1",
                plan_id="old-plan",
                task_id="task-1",
                attempt_id="attempt-1",
                transaction_id="TX123",
            ),
        )

        enriched = with_plan_trace(
            report, {"workflow_id": "workflow-1", "plan_id": "plan-2"}
        )

        self.assertEqual(enriched.formal_trace.task_id, "task-1")
        self.assertEqual(enriched.formal_trace.attempt_id, "attempt-1")
        self.assertEqual(enriched.formal_trace.transaction_id, "TX123")

    def test_status_blocks_on_read_only_unresolved_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            deps = _tracing_dependencies(tmp, world)
            launch_project(project_path="approved.json", dependencies=deps)
            world.statuses.update(
                research="completed",
                design="completed",
                prediction="completed",
                critic="completed",
                planner="completed",
                orchestrator="completed",
            )
            world.orchestrator_status = "ready"
            world.transaction = FormalBoundary.blocked(
                "transaction",
                "transaction_recovery_unresolved",
                "recovery unresolved",
                transaction_id="TX123",
            )
            world.calls.clear()

            result = status_launcher_run(
                launcher_run_id=LAUNCHER_ID, dependencies=deps
            )

            self.assertEqual(result.exit_code, 3)
            self.assertEqual(result.payload.error.code, "transaction_recovery_unresolved")
            self.assertEqual(result.payload.formal_trace.transaction_id, "TX123")
            self.assertIn("inspect_transaction_recovery", world.calls)
            self.assertNotIn("execution", world.calls)

    def test_worker_failure_survives_repeated_status_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            deps = _tracing_dependencies(tmp, world)
            launch_project(project_path="approved.json", dependencies=deps)
            world.fail_at = "execution"
            first = resume_launcher_run(
                launcher_run_id=LAUNCHER_ID,
                approval_paths=("approval.json",),
                dependencies=deps,
            )
            drains = world.calls.count("execution")

            status_result = status_launcher_run(
                launcher_run_id=LAUNCHER_ID, dependencies=deps
            )
            resume_result = resume_launcher_run(
                launcher_run_id=LAUNCHER_ID, dependencies=deps
            )

            for result in (first, status_result, resume_result):
                self.assertEqual(result.payload.status, "failed")
                self.assertEqual(result.payload.error.component, "execution")
                self.assertEqual(result.payload.formal_trace.task_id, "task-1")
                self.assertEqual(result.payload.formal_trace.attempt_id, "attempt-1")
                self.assertEqual(
                    result.payload.formal_trace.transaction_id, "transaction-1"
                )
            self.assertEqual(world.calls.count("execution"), drains)

    def test_formal_owner_completion_explicitly_clears_prior_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            world.fail_at = "research"
            deps = _tracing_dependencies(tmp, world)
            failed = launch_project(project_path="approved.json", dependencies=deps)
            self.assertEqual(failed.payload.error.component, "research")

            world.fail_at = None
            world.statuses["research"] = "completed"
            recovered = resume_launcher_run(
                launcher_run_id=LAUNCHER_ID, dependencies=deps
            )
            persisted = deps.diagnostics.read(LAUNCHER_ID)

            self.assertEqual(recovered.payload.status, "awaiting_approval")
            self.assertIsNone(recovered.payload.error)
            self.assertIsNone(persisted.failure)
            self.assertIsNone(persisted.failed_boundary)

    def test_diagnostic_completion_never_overrides_formal_prediction_blocker(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            deps = _tracing_dependencies(tmp, world)
            launch_project(project_path="approved.json", dependencies=deps)
            world.statuses.update(
                prediction="blocked", critic="completed", planner="completed"
            )
            report = deps.diagnostics.read(LAUNCHER_ID).with_observation(
                last_completed_boundary="execution",
                last_known_formal_status="completed",
            )
            deps.diagnostics.write(report)

            result = status_launcher_run(
                launcher_run_id=LAUNCHER_ID, dependencies=deps
            )

            self.assertEqual(result.payload.status, "blocked")
            self.assertEqual(result.payload.error.code, "prediction_recovery_ambiguous")


if __name__ == "__main__":
    unittest.main()
