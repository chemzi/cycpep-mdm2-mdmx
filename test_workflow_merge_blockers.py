"""Characterization tests for PR #62 merge-blocker authority contracts."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from execution.recovery import RecoveryResult
from test_workflow_service import LAUNCHER_ID, _Runtime, _World, _dependencies
from workflow.adapters import DefaultWorkflowRuntime
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

    def inspect_transaction_recovery(self, _orchestrator=None):
        self.world.calls.append("inspect_transaction_recovery")
        return self.world.transaction


class _TransactionRecoveryRuntime(_TracingRuntime):
    def __init__(self, world, recover=None):
        super().__init__(world)
        self._recover = recover

    def inspect_orchestrator(self, plan):
        self.world.calls.append("inspect_orchestrator")
        return super().inspect_orchestrator(plan)

    def recover_transactions(self):
        self.world.calls.append("recover_transactions")
        if self._recover is not None:
            self._recover()


def _tracing_dependencies(root, world):
    deps = _dependencies(root, world)
    return LauncherServiceDependencies(
        **{
            **deps.__dict__,
            "runtime_factory": lambda *_args: _TracingRuntime(world),
        }
    )


class WorkflowMergeBlockerCharacterizationTests(unittest.TestCase):
    def test_adapter_projects_skipped_active_as_explicit_live_owner(self):
        recovery = RecoveryResult(skipped_active=("TX-live",))
        orchestrator = FormalBoundary.completed("orchestrator", run_id="run-1")

        with patch(
            "execution.inspect_transaction_recovery", return_value=recovery
        ):
            transaction = DefaultWorkflowRuntime.inspect_transaction_recovery(
                orchestrator
            )

        self.assertEqual(transaction.status, "active")
        self.assertTrue(transaction.references.get("live_owner"))
        self.assertEqual(transaction.references.get("transaction_id"), "TX-live")
        self.assertEqual(
            transaction.references.get("transaction_ids"), ("TX-live",)
        )

    def test_live_owner_projects_running_even_when_orchestrator_reports_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            deps = _dependencies(tmp, world)
            launch_project(project_path="approved.json", dependencies=deps)
            world.statuses["orchestrator"] = "completed"
            world.orchestrator_status = "ready"
            world.transaction = FormalBoundary(
                status="active",
                boundary="transaction",
                references={
                    "live_owner": True,
                    "transaction_id": "TX-live",
                    "transaction_ids": ("TX-live",),
                },
            )
            runtime = _TransactionRecoveryRuntime(world)
            deps = LauncherServiceDependencies(
                **{**deps.__dict__, "runtime_factory": lambda *_args: runtime}
            )
            world.calls.clear()

            result = resume_launcher_run(
                launcher_run_id=LAUNCHER_ID, dependencies=deps
            )

            self.assertEqual(result.payload.status, "running")
            self.assertEqual(result.payload.formal_trace.transaction_id, "TX-live")
            self.assertNotIn("recover_transactions", world.calls)
            self.assertNotIn("execution", world.calls)

    def test_initial_clean_inspection_rereads_orchestrator_before_drain(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            deps = _dependencies(tmp, world)
            launch_project(project_path="approved.json", dependencies=deps)
            world.statuses["orchestrator"] = "completed"
            world.orchestrator_status = "ready"
            runtime = _TransactionRecoveryRuntime(world)
            deps = LauncherServiceDependencies(
                **{**deps.__dict__, "runtime_factory": lambda *_args: runtime}
            )
            world.calls.clear()

            result = resume_launcher_run(
                launcher_run_id=LAUNCHER_ID, dependencies=deps
            )

            inspection = world.calls.index("inspect_transaction_recovery")
            reread = world.calls.index("inspect_orchestrator", inspection + 1)
            drain = world.calls.index("execution")
            self.assertEqual(result.payload.status, "completed")
            self.assertLess(inspection, reread)
            self.assertLess(reread, drain)

    def test_resume_inspects_transactions_before_every_active_orchestrator_state(self):
        for formal_status in ("ready", "running", "pending"):
            with (
                self.subTest(formal_status=formal_status),
                tempfile.TemporaryDirectory() as tmp,
            ):
                world = _World()
                deps = _dependencies(tmp, world)
                launch_project(project_path="approved.json", dependencies=deps)
                world.statuses["orchestrator"] = "completed"
                world.orchestrator_status = formal_status
                runtime = _TransactionRecoveryRuntime(world)
                deps = LauncherServiceDependencies(
                    **{**deps.__dict__, "runtime_factory": lambda *_args: runtime}
                )
                world.calls.clear()

                result = resume_launcher_run(
                    launcher_run_id=LAUNCHER_ID, dependencies=deps
                )

                expected = "completed" if formal_status == "ready" else formal_status
                self.assertEqual(result.payload.status, expected)
                self.assertIn("inspect_transaction_recovery", world.calls)
                self.assertNotIn("recover_transactions", world.calls)
                inspection = world.calls.index("inspect_transaction_recovery")
                if "execution" in world.calls:
                    self.assertLess(inspection, world.calls.index("execution"))
                if formal_status in {"running", "pending"}:
                    self.assertNotIn("execution", world.calls)

    def test_stale_transaction_recovery_rereads_orchestrator_before_drain(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            deps = _dependencies(tmp, world)
            launch_project(project_path="approved.json", dependencies=deps)
            world.statuses["orchestrator"] = "completed"
            world.orchestrator_status = "running"
            world.transaction = FormalBoundary.blocked(
                "transaction",
                "transaction_recovery_unresolved",
                "stale owner requires recovery",
                transaction_id="TX-stale",
            )

            def recover():
                world.transaction = FormalBoundary.completed("transaction")
                world.orchestrator_status = "ready"

            runtime = _TransactionRecoveryRuntime(world, recover=recover)
            deps = LauncherServiceDependencies(
                **{**deps.__dict__, "runtime_factory": lambda *_args: runtime}
            )
            world.calls.clear()

            result = resume_launcher_run(
                launcher_run_id=LAUNCHER_ID, dependencies=deps
            )

            self.assertEqual(result.payload.status, "completed")
            first_inspection = world.calls.index("inspect_transaction_recovery")
            recovery = world.calls.index("recover_transactions")
            reread = world.calls.index("inspect_orchestrator", recovery + 1)
            second_inspection = world.calls.index(
                "inspect_transaction_recovery", first_inspection + 1
            )
            drain = world.calls.index("execution")
            self.assertLess(first_inspection, recovery)
            self.assertLess(recovery, reread)
            self.assertLess(reread, second_inspection)
            self.assertLess(second_inspection, drain)
            self.assertEqual(world.calls.count("execution"), 1)

    def test_unresolved_recovery_blocks_without_claim_or_scientific_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            deps = _dependencies(tmp, world)
            launch_project(project_path="approved.json", dependencies=deps)
            world.statuses["orchestrator"] = "completed"
            world.orchestrator_status = "pending"
            world.transaction = FormalBoundary.blocked(
                "transaction",
                "transaction_recovery_unresolved",
                "recovery remains unresolved",
                transaction_id="TX-unresolved",
            )
            runtime = _TransactionRecoveryRuntime(world)
            deps = LauncherServiceDependencies(
                **{**deps.__dict__, "runtime_factory": lambda *_args: runtime}
            )
            world.calls.clear()

            result = resume_launcher_run(
                launcher_run_id=LAUNCHER_ID, dependencies=deps
            )

            self.assertEqual(result.exit_code, 3)
            self.assertEqual(
                result.payload.error.code, "transaction_recovery_unresolved"
            )
            self.assertEqual(result.payload.formal_trace.transaction_id, "TX-unresolved")
            self.assertIn("recover_transactions", world.calls)
            self.assertGreaterEqual(world.calls.count("inspect_orchestrator"), 2)
            self.assertGreaterEqual(
                world.calls.count("inspect_transaction_recovery"), 2
            )
            self.assertNotIn("execution", world.calls)

    def test_formal_clean_clears_only_matching_transaction_blocker_and_merges_trace(self):
        for failure_code, clears in (
            ("transaction_recovery_unresolved", True),
            ("transaction_operator_hold", False),
        ):
            with (
                self.subTest(failure_code=failure_code),
                tempfile.TemporaryDirectory() as tmp,
            ):
                world = _World()
                deps = _dependencies(tmp, world)
                launch_project(project_path="approved.json", dependencies=deps)
                report = deps.diagnostics.read(LAUNCHER_ID)
                report = replace(
                    report,
                    formal_trace=FormalTrace(
                        workflow_id="old-workflow",
                        run_id="old-run",
                        plan_id="old-plan",
                        task_id="task-1",
                        attempt_id="attempt-1",
                        transaction_id="TX-old",
                    ),
                ).with_failure(
                    boundary="transaction",
                    error=StructuredError(
                        code=failure_code,
                        component="transaction",
                        message="prior transaction diagnostic",
                    ),
                )
                deps.diagnostics.write(report)
                world.statuses["orchestrator"] = "completed"
                world.orchestrator_status = "running"
                runtime = _TransactionRecoveryRuntime(world)
                deps = LauncherServiceDependencies(
                    **{**deps.__dict__, "runtime_factory": lambda *_args: runtime}
                )

                result = resume_launcher_run(
                    launcher_run_id=LAUNCHER_ID, dependencies=deps
                )
                persisted = deps.diagnostics.read(LAUNCHER_ID)

                self.assertEqual(result.payload.status, "running")
                self.assertEqual(persisted.formal_trace.workflow_id, "workflow-1")
                self.assertEqual(persisted.formal_trace.run_id, "run-1")
                self.assertEqual(persisted.formal_trace.plan_id, "plan-1")
                self.assertEqual(persisted.formal_trace.task_id, "task-1")
                self.assertEqual(persisted.formal_trace.attempt_id, "attempt-1")
                self.assertEqual(persisted.formal_trace.transaction_id, "TX-old")
                if clears:
                    self.assertIsNone(persisted.failure)
                else:
                    self.assertEqual(persisted.failure.code, failure_code)

    def test_diagnostic_update_failure_emits_bounded_operational_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            writes = [0]

            def fail_second_write(path, value):
                from execution.supervisor import durable_atomic_json

                writes[0] += 1
                if writes[0] == 2:
                    raise OSError("C:/internal/diagnostics/report.json unavailable")
                durable_atomic_json(path, value)

            world = _World()
            deps = _dependencies(tmp, world, writer=fail_second_write)

            with self.assertLogs("workflow.service", level="ERROR") as captured:
                result = launch_project(
                    project_path="approved.json", dependencies=deps
                )

            self.assertEqual(result.exit_code, 2)
            logged = "\n".join(captured.output)
            self.assertIn("launcher command failed", logged)
            self.assertNotIn("C:/internal", logged)

    def test_unbound_service_failure_emits_bounded_operational_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = _World()
            deps = _tracing_dependencies(tmp, world)

            with self.assertLogs("workflow.service", level="ERROR") as captured:
                result = status_launcher_run(
                    launcher_run_id="invalid", dependencies=deps
                )

            self.assertEqual(result.exit_code, 2)
            self.assertTrue(any("launcher command failed" in item for item in captured.output))

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
