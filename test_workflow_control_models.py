"""Focused contract tests for browser project-launch control models."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from workflow.control_models import (
    ApprovalBudgetProjection,
    ApprovalCeilings,
    AutoApprovalCeilings,
    ControlFailure,
    ControlFailureCategory,
    ControlFailureCode,
    FirstGateAutoApprovalPolicy,
    ManualApprovalRequest,
    PreOrchestratorApprovalProjection,
    ProjectLaunchOptions,
    ProjectLaunchRequest,
    ScopedReadIdentity,
    TaskResourceProjection,
)


LAUNCHER_ID = "launcher_0123456789abcdef0123456789abcdef"
PLAN_ID = "planner_0123456789ab"
DIGEST = "a" * 64


def _task(*, minutes: float | None = 2.5, status: str = "estimated"):
    return TaskResourceProjection(
        task_id="T001",
        action="evaluate_new_design_candidates",
        resource_class="gpu",
        gpu_job_slots=1,
        proposal_count=0,
        candidate_limit=10,
        estimated_gpu_minutes=minutes,
        estimate_status=status,
        estimator_version="simple-v1" if minutes is not None else None,
        calibration_status="provisional" if minutes is not None else "unavailable",
    )


class WorkflowControlModelTests(unittest.TestCase):
    def test_launch_request_keeps_optional_identity_and_first_gate_policy(self):
        policy = FirstGateAutoApprovalPolicy(
            approver="demo-operator",
            justification="Approved for this bootstrap gate.",
            ceilings=AutoApprovalCeilings(
                max_gpu_job_slots=1,
                max_gpu_minutes=60.0,
                max_design_proposals=8,
                max_prediction_candidates=16,
            ),
        )
        request = ProjectLaunchRequest(
            target_identifier="MDM2",
            options=ProjectLaunchOptions(
                identifier_type="gene",
                launcher_run_id=LAUNCHER_ID,
                first_gate_auto_policy=policy,
            ),
        )

        self.assertEqual(
            request.to_dict(),
            {
                "target_identifier": "MDM2",
                "options": {
                    "identifier_type": "gene",
                    "organism_id": 9606,
                    "epitope": None,
                    "objective": "binder",
                    "launcher_run_id": LAUNCHER_ID,
                    "first_gate_auto_policy": {
                        "approver": "demo-operator",
                        "justification": "Approved for this bootstrap gate.",
                        "ceilings": {
                            "max_gpu_job_slots": 1,
                            "max_gpu_minutes": 60.0,
                            "max_design_proposals": 8,
                            "max_prediction_candidates": 16,
                        },
                    },
                },
            },
        )
        self.assertEqual(ProjectLaunchRequest.from_dict(request.to_dict()), request)
        with self.assertRaises(FrozenInstanceError):
            request.target_identifier = "OTHER"

    def test_launcher_identity_and_auto_ceilings_are_fail_closed(self):
        with self.assertRaises(ValueError):
            ProjectLaunchOptions(launcher_run_id="../launcher_bad")
        with self.assertRaises(ValueError):
            AutoApprovalCeilings(1, 0.0, 8, 16)
        with self.assertRaises(ValueError):
            AutoApprovalCeilings(True, 60.0, 8, 16)

    def test_projection_binds_exact_plan_tasks_and_preserves_provisional_status(self):
        projection = PreOrchestratorApprovalProjection(
            launcher_run_id=LAUNCHER_ID,
            project_id="project-1",
            approved_content_binding=DIGEST,
            plan_id=PLAN_ID,
            plan_sha256=DIGEST,
            source_kind="initial_prediction_bootstrap",
            required_task_ids=("T001",),
            tasks=(_task(),),
            budget=ApprovalBudgetProjection(
                gpu_minutes=2.5,
                gpu_minutes_status="estimated",
                estimator_version="simple-v1",
                calibration_status="provisional",
            ),
        )

        payload = projection.to_dict()
        self.assertEqual(payload["plan_sha256"], DIGEST)
        self.assertEqual(projection.plan_digest, DIGEST)
        self.assertEqual(payload["tasks"][0]["estimated_gpu_minutes"], 2.5)
        self.assertEqual(payload["tasks"][0]["calibration_status"], "provisional")
        self.assertNotIn("path", repr(payload).lower())
        with self.assertRaises(ValueError):
            PreOrchestratorApprovalProjection(
                launcher_run_id=LAUNCHER_ID,
                project_id="project-1",
                approved_content_binding=DIGEST,
                plan_id=PLAN_ID,
                plan_sha256=DIGEST,
                source_kind="initial_prediction_bootstrap",
                required_task_ids=("T002",),
                tasks=(_task(),),
                budget=projection.budget,
            )

    def test_unavailable_estimate_remains_null_and_cannot_claim_calibration(self):
        resource = _task(minutes=None, status="benchmark_required")
        self.assertIsNone(resource.to_dict()["estimated_gpu_minutes"])
        with self.assertRaises(ValueError):
            _task(minutes=4.0, status="benchmark_required")
        with self.assertRaises(ValueError):
            TaskResourceProjection(
                task_id="T001",
                action="evaluate_new_design_candidates",
                resource_class="gpu",
                gpu_job_slots=1,
                proposal_count=0,
                candidate_limit=10,
                estimated_gpu_minutes=4.0,
                estimate_status="estimated",
                estimator_version="simple-v1",
                calibration_status="unavailable",
            )

    def test_manual_approval_echoes_exact_binding_and_allows_contract_null_limits(self):
        request = ManualApprovalRequest(
            launcher_run_id=LAUNCHER_ID,
            project_id="project-1",
            approved_content_binding=DIGEST,
            plan_id=PLAN_ID,
            plan_sha256=DIGEST,
            required_task_ids=("T001",),
            approver="operator",
            justification="Reviewed exact plan.",
            ceilings=ApprovalCeilings(max_gpu_minutes=60.0),
        )

        payload = request.to_dict()
        self.assertEqual(payload["required_task_ids"], ["T001"])
        self.assertEqual(payload["ceilings"]["max_gpu_minutes"], 60.0)
        self.assertIsNone(payload["ceilings"]["max_gpu_job_slots"])
        self.assertEqual(ManualApprovalRequest.from_dict(payload), request)

    def test_scoped_read_validates_launcher_identity(self):
        self.assertEqual(
            ScopedReadIdentity(LAUNCHER_ID).to_dict(),
            {"launcher_run_id": LAUNCHER_ID},
        )
        with self.assertRaises(ValueError):
            ScopedReadIdentity("launcher_short")

    def test_control_failure_has_fixed_category_and_sanitized_browser_message(self):
        failure = ControlFailure(
            code=ControlFailureCode.APPROVAL_ESTIMATE_UNAVAILABLE,
            category=ControlFailureCategory.ESTIMATE,
            component="planner",
            message=(
                "Traceback (most recent call last):\n"
                '  File "C:\\secret\\runner.py", line 3\n'
                "token=abc"
            ),
        )

        payload = failure.to_dict()
        self.assertEqual(payload["code"], "approval_estimate_unavailable")
        self.assertEqual(payload["category"], "estimate")
        self.assertNotIn("Traceback", payload["message"])
        self.assertNotIn("secret", payload["message"])
        self.assertNotIn("abc", payload["message"])
        with self.assertRaises(ValueError):
            ControlFailure(
                code=ControlFailureCode.APPROVAL_PLAN_STALE,
                category=ControlFailureCategory.CEILING,
                component="planner",
                message="Plan changed.",
            )


if __name__ == "__main__":
    unittest.main()
