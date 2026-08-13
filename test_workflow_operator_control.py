"""Focused tests for exact pre-Orchestrator operator control."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from core.context import ProjectContext, ProjectPaths
from workflow.boundaries import FormalBoundary
from workflow.control_models import ApprovalCeilings, ManualApprovalRequest
from workflow.diagnostics import DiagnosticStore
from workflow.errors import DiagnosticContractError
from workflow.models import (
    BrowserResult,
    DiagnosticReport,
    LauncherCommandResult,
    RuntimeLocatorBinding,
)
from workflow.operator_control import (
    approve_and_resume,
    bound_launcher_context,
    bound_launcher_project,
    inspect_first_gate_auto_approval,
    inspect_pre_orchestrator_approval,
)
from workflow.service import LauncherServiceDependencies


LAUNCHER_ID = "launcher_0123456789abcdef0123456789abcdef"
PROJECT_ID = "project-1"
APPROVED_BINDING = "a" * 64
PLAN_ID = "planner_0123456789ab"
PLAN_SHA = "b" * 64


def _context(root: Path, *, project_id: str = PROJECT_ID) -> ProjectContext:
    return ProjectContext(
        project_id=project_id,
        config={
            "project_id": project_id,
            "targets": [{"id": "TARGET"}],
            "review": {
                "status": "approved",
                "approved_digest": APPROVED_BINDING,
                "content_digest": APPROVED_BINDING,
            },
        },
        paths=ProjectPaths(
            data_dir=root / "data",
            evidence_dir=root / "evidence",
            database_path=root / "data" / "store.db",
        ),
    )


def _plan(
    *, minutes: float | None = 2.5, retry: bool = False
) -> dict:
    source = {
        "kind": "initial_prediction_bootstrap",
        "project_id": PROJECT_ID,
        "approved_content_binding": APPROVED_BINDING,
        "launcher_run_id": LAUNCHER_ID,
    }
    if retry:
        source["retry"] = {"retry_index": 1}
    status = "estimated" if minutes is not None else "benchmark_required"
    return {
        "plan_id": PLAN_ID,
        "workflow_id": "workflow-1",
        "source": source,
        "approval_request": {"required_task_ids": ["T001"]},
        "tasks": [{
            "task_id": "T001",
            "action": "evaluate_new_design_candidates",
            "resource_request": {
                "class": "gpu",
                "gpu_job_slots": 1,
                "proposal_count": 0,
                "candidate_limit": 2,
                "estimated_gpu_minutes": minutes,
                "estimate_status": status,
            },
        }],
        "decision_metadata": {
            "estimator_version": "simple-v1",
            "estimate_calibration_status": (
                "provisional" if minutes is not None else "unavailable"
            ),
        },
        "budget_request": {
            "gpu_minutes": minutes,
            "gpu_minutes_status": status,
            "gpu_minutes_estimator_version": (
                "simple-v1" if minutes is not None else None
            ),
            "gpu_minutes_calibration_status": (
                "provisional" if minutes is not None else "unavailable"
            ),
        },
    }


class _Runtime:
    def __init__(self, plan: dict):
        self.plan = plan

    def inspect_design(self):
        return FormalBoundary.completed("design", completion_event_id="design-complete")

    def inspect_bootstrap_planner(self, _design):
        return FormalBoundary.completed(
            "planner",
            plan_id=self.plan["plan_id"],
            plan_sha256=PLAN_SHA,
            plan_path="C:/formal/bootstrap-plan.json",
            plan_document=self.plan,
        )

    def inspect_orchestrator(self, _plan):
        return FormalBoundary.not_started("orchestrator")

    def inspect_approvals(self, _planner):
        return FormalBoundary.not_started("approval")


def _dependencies(
    root: Path,
    *,
    context: ProjectContext | None = None,
    plan: dict | None = None,
    with_locator: bool = True,
):
    context = context or _context(root / "project")
    runtime = _Runtime(plan or _plan())
    diagnostics = DiagnosticStore(root / "diagnostics")
    project_path = root / "approved-project.json"
    locator = (
        RuntimeLocatorBinding.from_context(
            context, project_path, execution_root=root / "execution"
        )
        if with_locator
        else None
    )
    diagnostics.create(DiagnosticReport.initial(
        launcher_run_id=LAUNCHER_ID,
        project_id=PROJECT_ID,
        approved_content_binding=APPROVED_BINDING,
        project_locator=str(project_path.resolve()),
        runtime_locator_binding=locator,
    ))
    observations = {"formal_store": 0, "read_only": []}

    def validate_store(_binding, _context):
        observations["formal_store"] += 1

    def runtime_factory(_context, _run_id, _binding, read_only):
        observations["read_only"].append(read_only)
        return runtime

    deps = LauncherServiceDependencies(
        diagnostics=diagnostics,
        load_context=lambda _path: context,
        validate_project=lambda _config: None,
        bind_context=lambda _context: nullcontext(),
        runtime_factory=lambda *_args: runtime,
        launcher_id=lambda: LAUNCHER_ID,
        restore_context=lambda _binding: context,
        validate_formal_store=validate_store,
        runtime_factory_with_locator=runtime_factory,
    )
    return deps, observations


def _request(*, digest: str = PLAN_SHA, max_minutes: float | None = 10.0):
    return ManualApprovalRequest(
        launcher_run_id=LAUNCHER_ID,
        project_id=PROJECT_ID,
        approved_content_binding=APPROVED_BINDING,
        plan_id=PLAN_ID,
        plan_sha256=digest,
        required_task_ids=("T001",),
        approver="operator",
        justification="Reviewed exact bootstrap plan.",
        ceilings=ApprovalCeilings(
            max_gpu_job_slots=1,
            max_gpu_minutes=max_minutes,
            max_design_proposals=0,
            max_prediction_candidates=2,
        ),
    )


class WorkflowOperatorControlTests(unittest.TestCase):
    def test_awaiting_projection_is_formal_path_free_and_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps, observations = _dependencies(Path(tmp))

            projection = inspect_pre_orchestrator_approval(
                launcher_run_id=LAUNCHER_ID, dependencies=deps
            )

            self.assertEqual(projection.plan_id, PLAN_ID)
            self.assertEqual(projection.required_task_ids, ("T001",))
            self.assertEqual(projection.budget.gpu_minutes, 2.5)
            self.assertEqual(observations["formal_store"], 1)
            self.assertEqual(observations["read_only"], [True])
            self.assertNotIn("path", repr(projection.to_dict()).lower())

    def test_public_bound_context_uses_exact_validated_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps, observations = _dependencies(Path(tmp))

            with bound_launcher_context(
                launcher_run_id=LAUNCHER_ID, dependencies=deps
            ) as bound:
                self.assertEqual(bound.context.project_id, PROJECT_ID)
                self.assertEqual(bound.report.launcher_run_id, LAUNCHER_ID)

            self.assertEqual(observations["formal_store"], 1)

    def test_public_bound_launcher_project_yields_validated_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps, observations = _dependencies(Path(tmp))

            with bound_launcher_project(LAUNCHER_ID, deps) as context:
                self.assertEqual(context.project_id, PROJECT_ID)
                self.assertEqual(
                    context.config["review"]["approved_digest"], APPROVED_BINDING
                )

            self.assertEqual(observations["formal_store"], 1)

    def test_missing_locator_and_restored_binding_conflict_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps, _ = _dependencies(Path(tmp), with_locator=False)
            with self.assertRaises(DiagnosticContractError) as missing:
                inspect_pre_orchestrator_approval(
                    launcher_run_id=LAUNCHER_ID, dependencies=deps
                )
            self.assertEqual(missing.exception.code, "launcher_runtime_locator_unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deps, _ = _dependencies(root)
            sidecar = (
                root
                / "diagnostics"
                / f"{LAUNCHER_ID}.runtime-locator.json"
            )
            sidecar.write_text("{invalid", encoding="utf-8")
            with self.assertRaises(DiagnosticContractError) as invalid:
                inspect_pre_orchestrator_approval(
                    launcher_run_id=LAUNCHER_ID, dependencies=deps
                )
            self.assertEqual(invalid.exception.code, "launcher_runtime_locator_unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            deps, observations = _dependencies(
                Path(tmp), context=_context(Path(tmp) / "other", project_id="project-2")
            )
            with self.assertRaises(DiagnosticContractError) as conflict:
                inspect_pre_orchestrator_approval(
                    launcher_run_id=LAUNCHER_ID, dependencies=deps
                )
            self.assertEqual(conflict.exception.code, "control_binding_conflict")
            self.assertEqual(observations["formal_store"], 0)

    def test_auto_inspection_rejects_retry_but_manual_projection_accepts_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps, _ = _dependencies(Path(tmp), plan=_plan(retry=True))
            manual = inspect_pre_orchestrator_approval(
                launcher_run_id=LAUNCHER_ID, dependencies=deps
            )
            self.assertEqual(manual.plan_id, PLAN_ID)
            with self.assertRaises(DiagnosticContractError) as rejected:
                inspect_first_gate_auto_approval(
                    launcher_run_id=LAUNCHER_ID, dependencies=deps
                )
            self.assertEqual(rejected.exception.code, "approval_plan_stale")

    def test_stale_unavailable_and_exceeded_requests_record_no_approval(self):
        cases = (
            (_plan(), _request(digest="c" * 64), "approval_plan_stale"),
            (_plan(minutes=None), _request(), "approval_estimate_unavailable"),
            (_plan(minutes=12.0), _request(max_minutes=10.0), "approval_ceiling_exceeded"),
        )
        for plan, request, code in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                deps, _ = _dependencies(Path(tmp), plan=plan)
                approvals = []
                with self.assertRaises(DiagnosticContractError) as caught:
                    approve_and_resume(
                        request=request,
                        dependencies=deps,
                        approval_recorder=lambda **kwargs: approvals.append(kwargs),
                    )
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(approvals, [])

    def test_exact_manual_approval_records_and_resumes_under_same_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps, _ = _dependencies(Path(tmp))
            recorded = []
            continued = []
            expected = LauncherCommandResult(
                BrowserResult(status="completed", launcher_run_id=LAUNCHER_ID), 0
            )

            def record(**kwargs):
                recorded.append(kwargs)
                return {"approval_path": "C:/formal/approval.json"}

            def continue_run(**kwargs):
                continued.append(kwargs)
                self.assertEqual(
                    kwargs["session"].launcher_run_id,
                    kwargs["report"].launcher_run_id,
                )
                return expected

            with patch(
                "workflow.operator_control.continue_locked_launcher_run",
                side_effect=continue_run,
            ):
                result = approve_and_resume(
                    request=_request(),
                    dependencies=deps,
                    approval_recorder=record,
                )

            self.assertIs(result, expected)
            self.assertEqual(recorded[0]["plan_path"], "C:/formal/bootstrap-plan.json")
            self.assertEqual(recorded[0]["task_ids"], ["T001"])
            self.assertEqual(recorded[0]["max_gpu_minutes"], 10.0)
            self.assertEqual(len(continued), 1)


if __name__ == "__main__":
    unittest.main()
