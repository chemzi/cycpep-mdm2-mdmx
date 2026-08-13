"""Characterization tests for the browser project-launch control baseline."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path

import data_layer
from agents.planner import build_initial_prediction_bootstrap_plan
from contracts.event import EvidenceEvent
from core.context import ProjectContext, ProjectPaths
from prediction_pipeline.execution_identity import build_prediction_execution_identity
from project_config import normalize_project_config
from target_bootstrap import (
    ReviewRequiredError,
    assert_project_approved,
    config_digest,
)
from web_api.workbench import WorkbenchReader
from workflow.boundaries import FormalBoundary
from workflow.diagnostics import DiagnosticStore, validate_launcher_run_id
from workflow.runtime_context import bind_project_context
from workflow.service import (
    LauncherServiceDependencies,
    launch_project,
    resume_launcher_run,
    status_launcher_run,
)


LAUNCHER_ID = "launcher_0123456789abcdef0123456789abcdef"


def _approved_config(project_id: str = "project-1") -> dict:
    config = normalize_project_config({
        "project_id": project_id,
        "name": project_id,
        "targets": [{"id": "TARGET", "uniprot": "P00001"}],
        "review": {"status": "approved"},
    })
    binding = config_digest(config)
    config["review"].update({
        "approved_digest": binding,
        "content_digest": binding,
    })
    return config


def _context(project_id: str, root: Path) -> ProjectContext:
    return ProjectContext.from_config(
        _approved_config(project_id),
        paths=ProjectPaths(
            data_dir=root / "data",
            evidence_dir=root / "evidence",
            output_dir=root / "output",
        ),
    )


class _BootstrapWorld:
    def __init__(self) -> None:
        self.research_completed = False
        self.design_completed = False
        self.plan_completed = False
        self.calls: list[object] = []


class _BootstrapRuntime:
    prediction_invocation_id = "prediction_invocation_0123456789abcdef0123456789abcdef"
    prediction_run_id = "prediction_0123456789abcdef0123456789abcdef"

    def __init__(self, world: _BootstrapWorld) -> None:
        self.world = world

    def inspect_prediction(self):
        return FormalBoundary.not_started("prediction")

    def inspect_research(self):
        if not self.world.research_completed:
            return FormalBoundary.not_started("research")
        return FormalBoundary.completed(
            "research", completion_event_id="research-complete"
        )

    def run_research(self):
        self.world.calls.append("research")
        self.world.research_completed = True

    def inspect_design(self):
        if not self.world.design_completed:
            return FormalBoundary.not_started("design")
        return FormalBoundary.completed(
            "design",
            completion_event_id="design-complete",
            transaction_id="tx-design",
            candidate_ids=("C0001", "C0002"),
            artifact_ids=("artifact-design",),
        )

    def run_design(self):
        self.world.calls.append("design")
        self.world.design_completed = True

    def inspect_bootstrap_planner(self, _design):
        if not self.world.plan_completed:
            return FormalBoundary.not_started("planner")
        plan = {
            "plan_id": "planner_0123456789ab",
            "workflow_id": "workflow-bootstrap",
            "source": {"kind": "initial_prediction_bootstrap"},
            "approval_request": {"required_task_ids": ["T001"]},
        }
        return FormalBoundary.completed(
            "planner",
            plan_id=plan["plan_id"],
            plan_path="C:/formal/bootstrap-plan.json",
            plan_sha256="0" * 64,
            plan_document=plan,
        )

    def run_bootstrap_planner(self, _research, _design):
        self.world.calls.append("bootstrap_plan")
        self.world.plan_completed = True

    def inspect_approvals(self, _planner):
        return FormalBoundary.not_started("approval")

    def inspect_orchestrator(self, _plan):
        return FormalBoundary.not_started("orchestrator")

    def initialize_orchestrator(self, _plan_path, _approvals):
        self.world.calls.append("orchestrator_initialized")


def _launcher_dependencies(
    root: Path,
    context: ProjectContext,
    world: _BootstrapWorld,
    *,
    launcher_ids,
) -> LauncherServiceDependencies:
    return LauncherServiceDependencies(
        diagnostics=DiagnosticStore(root / "diagnostics"),
        load_context=lambda _path: context,
        validate_project=assert_project_approved,
        bind_context=lambda _context: nullcontext(),
        runtime_factory=lambda _context, _launcher_id: _BootstrapRuntime(world),
        launcher_id=lambda: next(launcher_ids),
        execution_root_resolver=lambda: root / "execution",
    )


class FrontendProjectLaunchBaselineTests(unittest.TestCase):
    def test_only_exact_approved_project_content_passes_the_launch_gate(self):
        approved = _approved_config()
        draft = {**approved, "review": {**approved["review"], "status": "draft"}}
        tampered = {
            **approved,
            "targets": [{"id": "OTHER", "uniprot": "P00002"}],
        }

        assert_project_approved(approved)
        with self.assertRaises(ReviewRequiredError):
            assert_project_approved(draft)
        with self.assertRaises(ReviewRequiredError):
            assert_project_approved(tampered)

    def test_legacy_launch_uses_generated_valid_run_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = _context("project-1", root / "project")
            world = _BootstrapWorld()
            generated = iter((LAUNCHER_ID,))
            dependencies = _launcher_dependencies(
                root, context, world, launcher_ids=generated
            )

            result = launch_project(
                project_path=root / "approved-project.json",
                dependencies=dependencies,
            )

            self.assertEqual(result.payload.launcher_run_id, LAUNCHER_ID)
            self.assertEqual(
                validate_launcher_run_id(result.payload.launcher_run_id), LAUNCHER_ID
            )
            persisted = dependencies.diagnostics.read(LAUNCHER_ID)
            self.assertEqual(persisted.project_id, context.project_id)
            self.assertEqual(
                persisted.approved_content_binding,
                context.config["review"]["approved_digest"],
            )

    def test_launcher_run_identity_namespace_is_exact(self):
        self.assertEqual(validate_launcher_run_id(LAUNCHER_ID), LAUNCHER_ID)
        for invalid in (
            "launcher-0123456789abcdef0123456789abcdef",
            "launcher_0123456789ABCDEF0123456789ABCDEF",
            "launcher_short",
            "../launcher_0123456789abcdef0123456789abcdef",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(Exception) as caught:
                validate_launcher_run_id(invalid)
            self.assertEqual(caught.exception.code, "launcher_run_id_invalid")

    def test_initial_design_pause_is_read_only_until_explicit_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = _context("project-1", root / "project")
            world = _BootstrapWorld()
            dependencies = _launcher_dependencies(
                root, context, world, launcher_ids=iter((LAUNCHER_ID,))
            )

            launched = launch_project(
                project_path=root / "approved-project.json",
                dependencies=dependencies,
            )
            calls_at_pause = list(world.calls)
            observed = status_launcher_run(
                launcher_run_id=LAUNCHER_ID, dependencies=dependencies
            )
            resumed_without_approval = resume_launcher_run(
                launcher_run_id=LAUNCHER_ID, dependencies=dependencies
            )

            for result in (launched, observed, resumed_without_approval):
                self.assertEqual(result.payload.status, "awaiting_approval")
                self.assertEqual(result.payload.required_task_ids, ("T001",))
                self.assertIsNone(result.payload.formal_trace.run_id)
            self.assertEqual(calls_at_pause, ["research", "design", "bootstrap_plan"])
            self.assertEqual(world.calls, calls_at_pause)
            self.assertNotIn("orchestrator_initialized", world.calls)

    def test_switching_project_contexts_keeps_store_reads_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contexts = {
                name: _context(name, root / name) for name in ("project-a", "project-b")
            }
            projections = {}
            for project_id, context in contexts.items():
                with bind_project_context(context):
                    store = data_layer.get_storage_backend()
                    store.append(EvidenceEvent(
                        timestamp="2026-08-13T00:00:00+00:00",
                        event_id=f"event-{project_id}",
                        agent="research",
                        event_type="research_invocation_started",
                        payload={"project_id": project_id},
                        phase="research",
                    ).to_dict())
                    projections[project_id] = WorkbenchReader(store).read()

            for project_id, projection in projections.items():
                other = "project-b" if project_id == "project-a" else "project-a"
                self.assertEqual(projection["project"]["project_id"], project_id)
                evidence_ids = {
                    item["event_id"] for item in projection["evidence"]["items"]
                }
                self.assertIn(f"event-{project_id}", evidence_ids)
                self.assertNotIn(f"event-{other}", evidence_ids)

    def test_bootstrap_estimate_and_budget_use_one_provisional_interpretation(self):
        plan = build_initial_prediction_bootstrap_plan(source={
            "project_id": "project-1",
            "approved_content_binding": "approved-content",
            "launcher_run_id": LAUNCHER_ID,
            "research_completion_event_id": "research-complete",
            "design_invocation_id": (
                "design_initial_0123456789abcdef0123456789abcdef"
            ),
            "design_completion_event_id": "design-complete",
            "design_transaction_id": "tx-design",
            "candidate_ids": ["C0001", "C0002"],
            "execution_identity": build_prediction_execution_identity(),
        })

        resource = plan["tasks"][0]["resource_request"]
        self.assertEqual(resource["estimated_gpu_minutes"], 2.5)
        self.assertEqual(resource["estimate_status"], "estimated")
        self.assertEqual(plan["decision_metadata"]["total_estimated_gpu_minutes"], 2.5)
        self.assertEqual(plan["decision_metadata"]["estimator_version"], "simple-v1")
        self.assertEqual(plan["budget_request"]["gpu_minutes"], 2.5)
        self.assertEqual(
            plan["budget_request"]["gpu_minutes_status"], "estimated"
        )
        self.assertEqual(
            plan["budget_request"]["gpu_minutes_estimator_version"], "simple-v1"
        )
        self.assertEqual(
            plan["budget_request"]["gpu_minutes_calibration_status"], "provisional"
        )


if __name__ == "__main__":
    unittest.main()
