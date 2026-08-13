"""Focused tests for the browser project-control application service."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from project_config import normalize_project_config
from target_bootstrap import config_digest
from web_api.project_control import ProjectControlError, ProjectControlService
from workflow.control_models import (
    ApprovalCeilings,
    ApprovalBudgetProjection,
    AutoApprovalCeilings,
    FirstGateAutoApprovalPolicy,
    ManualApprovalRequest,
    PreOrchestratorApprovalProjection,
    ProjectLaunchOptions,
    ProjectLaunchRequest,
    TaskResourceProjection,
)
from workflow.errors import DiagnosticContractError
from workflow.models import BrowserResult, LauncherCommandResult, StructuredError


LAUNCHER_ID = "launcher_0123456789abcdef0123456789abcdef"
DIGEST = "a" * 64


def _draft(*, approved=False):
    value = normalize_project_config({
        "project_id": "project-1",
        "name": "MDM2 project",
        "objective": "binder",
        "targets": [{
            "id": "MDM2", "uniprot": "Q00987",
            "coordinate_path": "C:/secret/target.pdb",
            "structure": {"status": "ready", "coordinate_path": "C:/secret/a.pdb"},
        }],
        "bootstrap": {"llm_error": "token=secret", "ambiguous_identifier": False},
        "review": {"status": "draft", "revision": 1, "blocking_issues": []},
    })
    if approved:
        binding = config_digest(value)
        value["review"].update({
            "status": "approved", "approved_digest": binding,
            "content_digest": binding,
        })
    return value


def _result(status="awaiting_approval"):
    return LauncherCommandResult(
        BrowserResult(
            status=status, launcher_run_id=LAUNCHER_ID,
            project_id="project-1", approved_content_binding=DIGEST,
            required_task_ids=("T001",) if status == "awaiting_approval" else (),
        ),
        0,
    )


def _projection(*, minutes=10.0, slots=1, proposals=2, candidates=4):
    status = "estimated" if minutes is not None else "benchmark_required"
    return PreOrchestratorApprovalProjection(
        launcher_run_id=LAUNCHER_ID,
        project_id="project-1",
        approved_content_binding=DIGEST,
        plan_id="planner_0123456789ab",
        plan_sha256=DIGEST,
        source_kind="initial_prediction_bootstrap",
        required_task_ids=("T001",),
        tasks=(TaskResourceProjection(
            task_id="T001", action="evaluate_new_design_candidates",
            resource_class="gpu", gpu_job_slots=slots,
            proposal_count=proposals, candidate_limit=candidates,
            estimated_gpu_minutes=minutes, estimate_status=status,
            estimator_version="simple-v1" if minutes is not None else None,
            calibration_status="provisional" if minutes is not None else "unavailable",
        ),),
        budget=ApprovalBudgetProjection(
            gpu_minutes=minutes, gpu_minutes_status=status,
            estimator_version="simple-v1" if minutes is not None else None,
            calibration_status="provisional" if minutes is not None else "unavailable",
        ),
    )


def _two_task_projection():
    first = _projection(minutes=6.0, slots=1, proposals=1, candidates=2)
    second = TaskResourceProjection(
        task_id="T002", action="evaluate_new_design_candidates",
        resource_class="gpu", gpu_job_slots=2, proposal_count=2,
        candidate_limit=3, estimated_gpu_minutes=4.0,
        estimate_status="estimated", estimator_version="simple-v1",
        calibration_status="provisional",
    )
    return PreOrchestratorApprovalProjection(
        launcher_run_id=first.launcher_run_id,
        project_id=first.project_id,
        approved_content_binding=first.approved_content_binding,
        plan_id=first.plan_id,
        plan_sha256=first.plan_sha256,
        source_kind=first.source_kind,
        required_task_ids=("T001", "T002"),
        tasks=(first.tasks[0], second),
        budget=ApprovalBudgetProjection(
            gpu_minutes=10.0, gpu_minutes_status="estimated",
            estimator_version="simple-v1", calibration_status="provisional",
        ),
    )


def _options(*, policy=True, **ceiling_overrides):
    ceilings = {
        "max_gpu_job_slots": 1, "max_gpu_minutes": 20.0,
        "max_design_proposals": 2, "max_prediction_candidates": 4,
        **ceiling_overrides,
    }
    auto = None if not policy else FirstGateAutoApprovalPolicy(
        approver="operator", justification="Approve exact first GPU gate.",
        ceilings=AutoApprovalCeilings(**ceilings),
    )
    return ProjectLaunchOptions(
        launcher_run_id=LAUNCHER_ID, first_gate_auto_policy=auto
    )


def _mark_stored_draft_approved(root):
    path = Path(root) / "drf_demo.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    binding = config_digest(value)
    value["review"].update({
        "status": "approved",
        "approved_digest": binding,
        "content_digest": binding,
    })
    path.write_text(json.dumps(value), encoding="utf-8")


class WebProjectControlTests(unittest.TestCase):
    def _service(self, root, *, draft, **overrides):
        bootstrapper = Mock()
        bootstrapper.create_draft.return_value = draft
        defaults = {
            "bootstrapper_factory": lambda: bootstrapper,
            "launcher": Mock(return_value=_result()),
            "launcher_status": Mock(return_value=_result()),
            "approval_inspector": Mock(return_value=_projection()),
            "auto_approval_inspector": Mock(return_value=_projection()),
            "approval_resumer": Mock(return_value=_result("running")),
            "draft_id_factory": lambda: "drf_demo",
        }
        defaults.update(overrides)
        return ProjectControlService(root, **defaults), bootstrapper, defaults

    def test_create_and_retrieve_return_only_safe_review_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, bootstrapper, _ = self._service(tmp, draft=_draft())
            created = service.create_draft(ProjectLaunchRequest("MDM2"))
            retrieved = service.retrieve_draft("drf_demo")

        self.assertEqual(created, retrieved)
        self.assertEqual(created["project_id"], "project-1")
        self.assertNotIn("coordinate_path", repr(created))
        self.assertNotIn("llm_error", repr(created))
        bootstrapper.create_draft.assert_called_once()

    def test_missing_launcher_status_is_a_retryable_not_found_failure(self):
        missing = LauncherCommandResult(
            BrowserResult(
                status="failed",
                launcher_run_id=LAUNCHER_ID,
                error=StructuredError(
                    code="launcher_diagnostic_not_found",
                    component="launcher",
                    message="Launcher diagnostic does not exist.",
                ),
            ),
            2,
        )
        with tempfile.TemporaryDirectory() as tmp:
            service, _, _ = self._service(
                tmp, draft=_draft(), launcher_status=Mock(return_value=missing)
            )

            view = service.status(LAUNCHER_ID)

        self.assertEqual(view["control_failure"]["code"], "launcher_run_not_found")
        self.assertEqual(view["launcher"]["status"], "failed")

    def test_continue_run_delegates_to_launcher_without_inventing_approval(self):
        continuation = Mock(return_value=_result("awaiting_approval"))
        with tempfile.TemporaryDirectory() as tmp:
            service, _, _ = self._service(
                tmp, draft=_draft(), launcher_resume=continuation
            )

            view = service.continue_run(LAUNCHER_ID)

        self.assertEqual(view["launcher"]["status"], "awaiting_approval")
        continuation.assert_called_once_with(launcher_run_id=LAUNCHER_ID)

    def test_project_must_be_approved_before_launcher_is_called(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = Mock(return_value=_result())
            service, _, _ = self._service(tmp, draft=_draft(), launcher=launcher)
            service.create_draft(ProjectLaunchRequest("MDM2"))
            with self.assertRaises(ProjectControlError) as caught:
                service.launch_project("drf_demo", _options(policy=False))

        self.assertEqual(caught.exception.to_dict()["code"], "project_review_blocked")
        launcher.assert_not_called()

    def test_approving_an_already_exact_approved_project_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            approver = Mock()
            service, _, _ = self._service(tmp, draft=_draft(), project_approver=approver)
            service.create_draft(ProjectLaunchRequest("MDM2"))
            _mark_stored_draft_approved(tmp)
            first = service.approve_project("drf_demo")
            second = service.approve_project("drf_demo")

        self.assertEqual(first, second)
        approver.assert_not_called()

    def test_fitting_auto_policy_records_one_normal_exact_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            resumer = Mock(return_value=_result("running"))
            service, _, defaults = self._service(
                tmp, draft=_draft(), approval_resumer=resumer
            )
            service.create_draft(ProjectLaunchRequest("MDM2"))
            _mark_stored_draft_approved(tmp)
            view = service.launch_project("drf_demo", _options())

        self.assertEqual(view["launcher"]["status"], "running")
        request = resumer.call_args.kwargs["request"]
        self.assertEqual(request.required_task_ids, ("T001",))
        self.assertEqual(request.ceilings.max_gpu_minutes, 20.0)
        defaults["launcher"].assert_called_once_with(
            project_path=Path(tmp) / "drf_demo.json", launcher_run_id=LAUNCHER_ID
        )
        resumer.assert_called_once()

    def test_unavailable_estimate_never_approves(self):
        with tempfile.TemporaryDirectory() as tmp:
            resumer = Mock(return_value=_result("running"))
            service, _, _ = self._service(
                tmp, draft=_draft(),
                auto_approval_inspector=Mock(return_value=_projection(minutes=None)),
                approval_resumer=resumer,
            )
            service.create_draft(ProjectLaunchRequest("MDM2"))
            _mark_stored_draft_approved(tmp)
            view = service.launch_project("drf_demo", _options())

        self.assertEqual(view["control_failure"]["code"], "approval_estimate_unavailable")
        self.assertEqual(view["launcher"]["status"], "awaiting_approval")
        resumer.assert_not_called()

    def test_auto_policy_uses_max_slots_and_summed_resources(self):
        projection = _two_task_projection()
        with tempfile.TemporaryDirectory() as tmp:
            resumer = Mock(return_value=_result("running"))
            service, _, _ = self._service(
                tmp, draft=_draft(),
                auto_approval_inspector=Mock(return_value=projection),
                approval_resumer=resumer,
            )
            service.create_draft(ProjectLaunchRequest("MDM2"))
            _mark_stored_draft_approved(tmp)
            view = service.launch_project(
                "drf_demo",
                _options(
                    max_gpu_job_slots=2,
                    max_gpu_minutes=10.0,
                    max_design_proposals=3,
                    max_prediction_candidates=5,
                ),
            )

        self.assertEqual(view["launcher"]["status"], "running")
        self.assertEqual(
            resumer.call_args.kwargs["request"].required_task_ids,
            ("T001", "T002"),
        )

    def test_each_auto_ceiling_fails_closed(self):
        cases = {
            "max_gpu_job_slots": 0,
            "max_gpu_minutes": 5.0,
            "max_design_proposals": 1,
            "max_prediction_candidates": 3,
        }
        for ceiling, value in cases.items():
            with self.subTest(ceiling=ceiling), tempfile.TemporaryDirectory() as tmp:
                resumer = Mock(return_value=_result("running"))
                service, _, _ = self._service(
                    tmp, draft=_draft(), approval_resumer=resumer
                )
                service.create_draft(ProjectLaunchRequest("MDM2"))
                _mark_stored_draft_approved(tmp)
                view = service.launch_project(
                    "drf_demo", _options(**{ceiling: value})
                )
                self.assertEqual(view["control_failure"]["ceiling"], ceiling)
                resumer.assert_not_called()

    def test_retry_or_later_plan_is_never_auto_approved(self):
        for label in ("retry", "later"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                inspector = Mock(side_effect=DiagnosticContractError(
                    "approval_plan_stale", "C:/secret/plan.json is not the first gate"
                ))
                resumer = Mock(return_value=_result("running"))
                service, _, _ = self._service(
                    tmp, draft=_draft(),
                    auto_approval_inspector=inspector, approval_resumer=resumer,
                )
                service.create_draft(ProjectLaunchRequest("MDM2"))
                _mark_stored_draft_approved(tmp)
                view = service.launch_project("drf_demo", _options())
                self.assertEqual(view["control_failure"]["code"], "approval_plan_stale")
                self.assertNotIn("secret", view["control_failure"]["message"])
                resumer.assert_not_called()

    def test_repeat_after_first_gate_does_not_reapply_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = Mock(side_effect=(_result(), _result("running")))
            resumer = Mock(return_value=_result("running"))
            inspector = Mock(return_value=_projection())
            service, _, _ = self._service(
                tmp, draft=_draft(), launcher=launcher,
                auto_approval_inspector=inspector, approval_resumer=resumer,
            )
            service.create_draft(ProjectLaunchRequest("MDM2"))
            _mark_stored_draft_approved(tmp)
            first = service.launch_project("drf_demo", _options())
            second = service.launch_project("drf_demo", _options())

        self.assertEqual((first["launcher"]["status"], second["launcher"]["status"]), ("running", "running"))
        inspector.assert_called_once()
        resumer.assert_called_once()

    def test_manual_exact_approval_delegates_and_stale_failure_is_safe(self):
        request = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            service, _, _ = self._service(tmp, draft=_draft())
            with self.assertRaises(TypeError):
                service.approve_and_continue(request)
            service._approval_resumer = Mock(side_effect=DiagnosticContractError(
                "approval_plan_stale", "C:/secret/plan.json changed"
            ))
            exact = _manual_request()
            view = service.approve_and_continue(exact)

        self.assertIsNone(view["launcher"])
        self.assertEqual(view["control_failure"]["code"], "approval_plan_stale")
        self.assertNotIn("secret", view["control_failure"]["message"])


def _manual_request():
    projection = _projection()
    return ManualApprovalRequest(
        launcher_run_id=projection.launcher_run_id,
        project_id=projection.project_id,
        approved_content_binding=projection.approved_content_binding,
        plan_id=projection.plan_id,
        plan_sha256=projection.plan_sha256,
        required_task_ids=projection.required_task_ids,
        approver="operator",
        justification="Reviewed exact plan.",
        ceilings=ApprovalCeilings(max_gpu_job_slots=1, max_gpu_minutes=20.0),
    )


if __name__ == "__main__":
    unittest.main()
    ManualApprovalRequest,
