"""Cross-command recovery tests for the Launcher's durable runtime locator."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from core.context import ProjectContext, ProjectPaths
from contracts.event import EvidenceEvent
from test_workflow_service import LAUNCHER_ID, _Runtime, _World
from workflow.boundaries import FormalBoundary
from workflow.diagnostics import DiagnosticStore, resolve_diagnostics_root
from workflow.models import DiagnosticReport, RuntimeLocatorBinding
from workflow.runtime_context import bind_project_context
from workflow.runtime_locator import restore_project_context
from workflow.service import (
    LauncherServiceDependencies,
    launch_project,
    resume_launcher_run,
    status_launcher_run,
)


def _context(root: Path, *, project_id: str = "project-1") -> ProjectContext:
    return ProjectContext(
        project_id=project_id,
        config={
            "project_id": project_id,
            "targets": [{"id": "TARGET"}],
            "review": {
                "status": "approved",
                "approved_digest": "approved-content",
                "content_digest": "approved-content",
            },
        },
        paths=ProjectPaths(
            data_dir=root / "data",
            evidence_dir=root / "evidence",
            database_path=root / "formal" / "store.db",
        ),
    )


def _restore(binding: RuntimeLocatorBinding, config: dict) -> ProjectContext:
    return ProjectContext.from_config(config, paths=binding.project_paths())


class _StoreReceiptRuntime(_Runtime):
    """Minimal runtime whose upstream completion is read from the formal Store."""

    def __init__(self, world, context, launcher_run_id, stores, invocations):
        super().__init__(world)
        from data_layer import get_storage_backend

        self.context = context
        self.launcher_run_id = launcher_run_id
        self.store = get_storage_backend()
        self._invocations = invocations
        stores.append(self.store)

    def _receipt(self):
        matches = self.store.query(
            project_id=self.context.project_id,
            agent="research",
            event_type="research_completion_receipt",
        )
        return next(
            (
                item
                for item in matches
                if item.get("launcher_run_id") == self.launcher_run_id
            ),
            None,
        )

    def inspect_research(self):
        receipt = self._receipt()
        if receipt is None:
            return FormalBoundary.not_started("research")
        return FormalBoundary.completed(
            "research",
            completion_event_id=receipt["event_id"],
            evidence_ids=(receipt["event_id"],),
        )

    def run_research(self):
        self._invocations.append("research")
        event = EvidenceEvent(
            timestamp="2026-08-10T00:00:00+00:00",
            event_id="runtime-locator-research-complete",
            agent="research",
            event_type="research_completion_receipt",
            phase="research",
            payload={
                "project_id": self.context.project_id,
                "launcher_run_id": self.launcher_run_id,
                "research_invocation_id": "research-runtime-locator",
                "approved_content_binding": "approved-content",
                "evidence_ids": [],
            },
        )
        self.store.append(event.to_dict())

    def inspect_design(self):
        return FormalBoundary.completed(
            "design",
            completion_event_id="design-complete",
            candidate_ids=("candidate-1",),
            artifact_ids=("artifact-1",),
        )

    def inspect_prediction(self):
        if self._receipt() is None:
            return FormalBoundary.not_started("prediction")
        return FormalBoundary.completed(
            "prediction",
            prediction_invocation_id=self.prediction_invocation_id,
            prediction_run_id=self.prediction_run_id,
            completion_event_id="prediction-complete",
            handoff_path="C:/internal/prediction_handoff.json",
            run_root="C:/internal/prediction-root",
        )

    def inspect_critic(self, prediction):
        return FormalBoundary.completed(
            "critic", report_id="critic-1", report_path="C:/internal/critic.json"
        )

    def inspect_planner(self, critic):
        return FormalBoundary.completed(
            "planner",
            plan_id="plan-1",
            plan_path="C:/internal/plan.json",
            plan_sha256="plan-digest",
            plan_document={
                "plan_id": "plan-1",
                "workflow_id": "workflow-1",
                "approval_request": {"required_task_ids": ["task-1"]},
            },
        )


class _ExecutionRootRecoveryRuntime(_StoreReceiptRuntime):
    def __init__(self, *args, execution_config, inspected, recovered, drained):
        super().__init__(*args)
        self.execution_config = execution_config
        self._inspected = inspected
        self._recovered = recovered
        self._drained = drained

    def inspect_transaction_recovery(self, _orchestrator=None):
        execution_root = self.execution_config.execution_root
        self._inspected.append(execution_root)
        marker = execution_root / ".staging" / "TX123" / "metadata" / "commit.json"
        if marker.is_file():
            return FormalBoundary.blocked(
                "transaction",
                "transaction_recovery_unresolved",
                "formal transaction recovery requires operator action",
                transaction_id="TX123",
            )
        return FormalBoundary.completed("transaction")

    def recover_transactions(self):
        self._recovered.append(self.execution_config.execution_root)

    def drain(self, run_path):
        self._drained.append((self.execution_config.execution_root, run_path))


class RuntimeLocatorModelTests(unittest.TestCase):
    def test_locator_round_trip_is_internal_and_carries_no_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            internal_root = Path(tmp).resolve()
            binding = RuntimeLocatorBinding(
                project_locator=str(internal_root / "approved" / "project.json"),
                data_dir=str(internal_root / "data"),
                evidence_dir=str(internal_root / "evidence"),
                database_path=str(internal_root / "formal" / "store.db"),
                execution_root=str(internal_root / "execution"),
            )
            report = DiagnosticReport.initial(
                launcher_run_id=LAUNCHER_ID,
                project_id="project-1",
                approved_content_binding="approved-content",
                project_locator=binding.project_locator,
                runtime_locator_binding=binding,
            )

            restored = DiagnosticReport.from_dict(report.to_dict())
            browser = restored.browser_projection(status="pending").to_dict()
            encoded_browser = json.dumps(browser)
            encoded_internal_root = json.dumps(str(internal_root))[1:-1]

            self.assertEqual(restored.runtime_locator_binding, binding)
            self.assertNotIn("runtime_locator_binding", browser)
            self.assertNotIn(encoded_internal_root, encoded_browser)
            self.assertFalse(hasattr(binding, "status"))
            self.assertFalse(hasattr(binding, "authorize_transition"))

    def test_default_restore_ignores_changed_runtime_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_path = root / "approved-project.json"
            context = _context(root / "runtime-a")
            project_path.write_text(
                json.dumps(dict(context.config)), encoding="utf-8"
            )
            binding = RuntimeLocatorBinding.from_context(
                context,
                project_path,
                execution_root=root / "runtime-a" / "execution",
            )
            environment_b = {
                "CYCPEP_DATA_DIR": str(root / "runtime-b" / "data"),
                "CYCPEP_EVIDENCE_DIR": str(root / "runtime-b" / "evidence"),
                "CYCPEP_DB_PATH": str(root / "runtime-b" / "formal" / "store.db"),
                "NP_DATA": str(root / "runtime-b"),
            }

            with patch.dict(os.environ, environment_b, clear=False):
                restored = restore_project_context(binding)

            self.assertEqual(restored.resolve_paths(), context.resolve_paths())
            self.assertNotEqual(
                restored.resolve_paths().data_dir,
                Path(environment_b["CYCPEP_DATA_DIR"]),
            )


class RuntimeLocatorServiceTests(unittest.TestCase):
    def test_status_and_resume_keep_transaction_recovery_on_original_execution_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_path = root / "approved-project.json"
            project_path.write_text(
                json.dumps(dict(_context(root / "unused").config)), encoding="utf-8"
            )
            runtime_a = root / "runtime-a"
            runtime_b = root / "runtime-b"
            environment_a = {
                "CYCPEP_DATA_DIR": str(runtime_a / "data"),
                "CYCPEP_EVIDENCE_DIR": str(runtime_a / "evidence"),
                "CYCPEP_DB_PATH": str(runtime_a / "formal" / "store.db"),
                "CYCPEP_EXECUTION_ROOT": str(runtime_a / "execution"),
                "NP_DATA": str(runtime_a),
            }
            environment_b = {
                "CYCPEP_DATA_DIR": str(runtime_b / "data"),
                "CYCPEP_EVIDENCE_DIR": str(runtime_b / "evidence"),
                "CYCPEP_DB_PATH": str(runtime_b / "formal" / "store.db"),
                "CYCPEP_EXECUTION_ROOT": str(runtime_b / "execution"),
                "NP_DATA": str(runtime_b),
            }
            world = _World()
            stores = []
            invocations = []
            inspected = []
            recovered = []
            drained = []

            def diagnostic_root():
                return resolve_diagnostics_root(
                    env=os.environ, repository_root=root / "repository"
                )

            def runtime(context, launcher_run_id, **kwargs):
                return _ExecutionRootRecoveryRuntime(
                    world,
                    context,
                    launcher_run_id,
                    stores,
                    invocations,
                    execution_config=kwargs["execution_config"],
                    inspected=inspected,
                    recovered=recovered,
                    drained=drained,
                )

            with (
                patch("workflow.service.resolve_diagnostics_root", diagnostic_root),
                patch("workflow.service.assert_project_approved", lambda _config: None),
                patch("workflow.adapters.DefaultWorkflowRuntime", runtime),
                patch("workflow.service.uuid.uuid4") as launcher_uuid,
            ):
                launcher_uuid.return_value.hex = LAUNCHER_ID.removeprefix("launcher_")
                with patch.dict(os.environ, environment_a, clear=False):
                    launched = launch_project(project_path=project_path)
                    marker = (
                        runtime_a
                        / "execution"
                        / ".staging"
                        / "TX123"
                        / "metadata"
                        / "commit.json"
                    )
                    marker.parent.mkdir(parents=True)
                    marker.write_text('{"status":"COMMITTED"}', encoding="utf-8")
                    world.statuses["orchestrator"] = "completed"
                    world.orchestrator_status = "running"
                with patch.dict(os.environ, environment_b, clear=False):
                    status = status_launcher_run(launcher_run_id=LAUNCHER_ID)
                    resumed = resume_launcher_run(launcher_run_id=LAUNCHER_ID)

            execution_a = (runtime_a / "execution").resolve()
            self.assertEqual(launched.payload.status, "awaiting_approval")
            self.assertEqual(status.payload.error.code, "transaction_recovery_unresolved")
            self.assertEqual(resumed.payload.error.code, "transaction_recovery_unresolved")
            self.assertEqual(status.payload.formal_trace.transaction_id, "TX123")
            self.assertEqual(resumed.payload.formal_trace.transaction_id, "TX123")
            self.assertEqual(inspected, [execution_a, execution_a, execution_a])
            self.assertEqual(recovered, [execution_a])
            self.assertEqual(drained, [])
            self.assertFalse((runtime_b / "execution").exists())

    def test_default_dependencies_find_run_after_np_data_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_path = root / "approved-project.json"
            project_path.write_text(
                json.dumps(dict(_context(root / "unused").config)), encoding="utf-8"
            )
            runtime_a = root / "runtime-a"
            runtime_b = root / "runtime-b"
            environment_a = {
                "CYCPEP_DATA_DIR": str(runtime_a / "data"),
                "CYCPEP_EVIDENCE_DIR": str(runtime_a / "evidence"),
                "CYCPEP_DB_PATH": str(runtime_a / "formal" / "store.db"),
                "NP_DATA": str(runtime_a),
                "CYCPEP_EXECUTION_ROOT": str(runtime_a / "execution"),
            }
            environment_b = {
                "CYCPEP_DATA_DIR": str(runtime_b / "data"),
                "CYCPEP_EVIDENCE_DIR": str(runtime_b / "evidence"),
                "CYCPEP_DB_PATH": str(runtime_b / "formal" / "store.db"),
                "NP_DATA": str(runtime_b),
                "CYCPEP_EXECUTION_ROOT": str(runtime_b / "execution"),
            }
            world = _World()
            stores = []
            invocations = []
            runtime_modes = []
            execution_roots = []

            def diagnostic_root():
                return resolve_diagnostics_root(
                    env=os.environ, repository_root=root / "repository"
                )

            def runtime(context, launcher_run_id, **_kwargs):
                runtime_modes.append(bool(_kwargs.get("read_only")))
                execution_roots.append(_kwargs["execution_config"].execution_root)
                return _StoreReceiptRuntime(
                    world, context, launcher_run_id, stores, invocations
                )

            with (
                patch("workflow.service.resolve_diagnostics_root", diagnostic_root),
                patch("workflow.service.assert_project_approved", lambda _config: None),
                patch("workflow.adapters.DefaultWorkflowRuntime", runtime),
                patch("workflow.service.uuid.uuid4") as launcher_uuid,
            ):
                launcher_uuid.return_value.hex = LAUNCHER_ID.removeprefix("launcher_")
                with patch.dict(os.environ, environment_a, clear=False):
                    launched = launch_project(project_path=project_path)
                with patch.dict(os.environ, environment_b, clear=False):
                    status = status_launcher_run(launcher_run_id=LAUNCHER_ID)
                    resumed = resume_launcher_run(launcher_run_id=LAUNCHER_ID)

            self.assertEqual(launched.payload.status, "awaiting_approval")
            self.assertEqual(status.payload.status, "awaiting_approval")
            self.assertEqual(resumed.payload.status, "awaiting_approval")
            self.assertEqual(invocations, ["research"])
            self.assertEqual(runtime_modes, [False, True, False])
            self.assertEqual(
                execution_roots, [(runtime_a / "execution").resolve()] * 3
            )
            self.assertEqual(
                [store.path for store in stores],
                [(runtime_a / "formal" / "store.db").resolve()] * 3,
            )
            diagnostics = root / "repository" / "data" / "launcher_diagnostics"
            self.assertTrue((diagnostics / f"{LAUNCHER_ID}.json").is_file())
            self.assertTrue(
                (diagnostics / f"{LAUNCHER_ID}.runtime-locator.json").is_file()
            )
            self.assertFalse((runtime_b / "launcher_diagnostics").exists())

    def test_default_commands_do_not_recreate_a_missing_original_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_path = root / "approved-project.json"
            project_path.write_text(
                json.dumps(dict(_context(root / "unused").config)), encoding="utf-8"
            )
            runtime_root = root / "runtime-a"
            environment = {
                "CYCPEP_DATA_DIR": str(runtime_root / "data"),
                "CYCPEP_EVIDENCE_DIR": str(runtime_root / "evidence"),
                "CYCPEP_DB_PATH": str(runtime_root / "formal" / "store.db"),
                "NP_DATA": str(runtime_root),
            }
            world = _World()
            stores = []
            invocations = []
            runtime_calls = []

            def diagnostic_root():
                return resolve_diagnostics_root(
                    env=os.environ, repository_root=root / "repository"
                )

            def runtime(context, launcher_run_id, **kwargs):
                runtime_calls.append(kwargs.get("read_only", False))
                return _StoreReceiptRuntime(
                    world, context, launcher_run_id, stores, invocations
                )

            with (
                patch("workflow.service.resolve_diagnostics_root", diagnostic_root),
                patch("workflow.service.assert_project_approved", lambda _config: None),
                patch("workflow.adapters.DefaultWorkflowRuntime", runtime),
                patch("workflow.service.uuid.uuid4") as launcher_uuid,
                patch.dict(os.environ, environment, clear=False),
            ):
                launcher_uuid.return_value.hex = LAUNCHER_ID.removeprefix("launcher_")
                launched = launch_project(project_path=project_path)
                database = runtime_root / "formal" / "store.db"
                self.assertTrue(database.is_file())
                database.unlink()

                status = status_launcher_run(launcher_run_id=LAUNCHER_ID)
                resumed = resume_launcher_run(launcher_run_id=LAUNCHER_ID)

            self.assertEqual(launched.payload.status, "awaiting_approval")
            self.assertEqual(status.exit_code, 3)
            self.assertEqual(resumed.exit_code, 3)
            self.assertEqual(
                status.payload.error.code, "launcher_runtime_locator_unavailable"
            )
            self.assertEqual(
                resumed.payload.error.code, "launcher_runtime_locator_unavailable"
            )
            self.assertEqual(runtime_calls, [False])
            self.assertEqual(invocations, ["research"])
            self.assertFalse(database.exists())

    def test_default_commands_do_not_initialize_an_empty_original_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_path = root / "approved-project.json"
            project_path.write_text(
                json.dumps(dict(_context(root / "unused").config)), encoding="utf-8"
            )
            runtime_root = root / "runtime-a"
            environment = {
                "CYCPEP_DATA_DIR": str(runtime_root / "data"),
                "CYCPEP_EVIDENCE_DIR": str(runtime_root / "evidence"),
                "CYCPEP_DB_PATH": str(runtime_root / "formal" / "store.db"),
                "NP_DATA": str(runtime_root),
            }
            world = _World()
            stores = []
            invocations = []
            runtime_calls = []

            def diagnostic_root():
                return resolve_diagnostics_root(
                    env=os.environ, repository_root=root / "repository"
                )

            def runtime(context, launcher_run_id, **kwargs):
                runtime_calls.append(kwargs.get("read_only", False))
                return _StoreReceiptRuntime(
                    world, context, launcher_run_id, stores, invocations
                )

            with (
                patch("workflow.service.resolve_diagnostics_root", diagnostic_root),
                patch("workflow.service.assert_project_approved", lambda _config: None),
                patch("workflow.adapters.DefaultWorkflowRuntime", runtime),
                patch("workflow.service.uuid.uuid4") as launcher_uuid,
                patch.dict(os.environ, environment, clear=False),
            ):
                launcher_uuid.return_value.hex = LAUNCHER_ID.removeprefix("launcher_")
                launched = launch_project(project_path=project_path)
                database = runtime_root / "formal" / "store.db"
                self.assertTrue(database.is_file())
                database.write_bytes(b"")

                status = status_launcher_run(launcher_run_id=LAUNCHER_ID)
                resumed = resume_launcher_run(launcher_run_id=LAUNCHER_ID)

            self.assertEqual(launched.payload.status, "awaiting_approval")
            self.assertEqual(status.exit_code, 3)
            self.assertEqual(resumed.exit_code, 3)
            self.assertEqual(
                status.payload.error.code, "launcher_runtime_locator_unavailable"
            )
            self.assertEqual(
                resumed.payload.error.code, "launcher_runtime_locator_unavailable"
            )
            self.assertEqual(runtime_calls, [False])
            self.assertEqual(invocations, ["research"])
            self.assertEqual(database.read_bytes(), b"")

    def test_default_commands_do_not_migrate_an_incomplete_original_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_path = root / "approved-project.json"
            project_path.write_text(
                json.dumps(dict(_context(root / "unused").config)), encoding="utf-8"
            )
            runtime_root = root / "runtime-a"
            environment = {
                "CYCPEP_DATA_DIR": str(runtime_root / "data"),
                "CYCPEP_EVIDENCE_DIR": str(runtime_root / "evidence"),
                "CYCPEP_DB_PATH": str(runtime_root / "formal" / "store.db"),
                "NP_DATA": str(runtime_root),
            }
            world = _World()
            stores = []
            invocations = []
            runtime_calls = []

            def diagnostic_root():
                return resolve_diagnostics_root(
                    env=os.environ, repository_root=root / "repository"
                )

            def runtime(context, launcher_run_id, **kwargs):
                runtime_calls.append(kwargs.get("read_only", False))
                return _StoreReceiptRuntime(
                    world, context, launcher_run_id, stores, invocations
                )

            with (
                patch("workflow.service.resolve_diagnostics_root", diagnostic_root),
                patch("workflow.service.assert_project_approved", lambda _config: None),
                patch("workflow.adapters.DefaultWorkflowRuntime", runtime),
                patch("workflow.service.uuid.uuid4") as launcher_uuid,
                patch.dict(os.environ, environment, clear=False),
            ):
                launcher_uuid.return_value.hex = LAUNCHER_ID.removeprefix("launcher_")
                launched = launch_project(project_path=project_path)
                database = runtime_root / "formal" / "store.db"
                connection = sqlite3.connect(database)
                try:
                    connection.execute("DROP INDEX idx_evidence_transaction")
                    connection.execute(
                        "ALTER TABLE evidence_events DROP COLUMN transaction_id"
                    )
                    connection.commit()
                finally:
                    connection.close()
                before = database.read_bytes()

                status = status_launcher_run(launcher_run_id=LAUNCHER_ID)
                resumed = resume_launcher_run(launcher_run_id=LAUNCHER_ID)

            self.assertEqual(launched.payload.status, "awaiting_approval")
            self.assertEqual(status.exit_code, 3)
            self.assertEqual(resumed.exit_code, 3)
            self.assertEqual(
                status.payload.error.code, "launcher_runtime_locator_unavailable"
            )
            self.assertEqual(
                resumed.payload.error.code, "launcher_runtime_locator_unavailable"
            )
            self.assertEqual(runtime_calls, [False])
            self.assertEqual(invocations, ["research"])
            self.assertEqual(database.read_bytes(), before)

    def test_real_store_receipt_stays_on_runtime_a_across_status_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_path = root / "approved-project.json"
            project_path.write_text(
                json.dumps(dict(_context(root / "unused").config)), encoding="utf-8"
            )
            runtime_a = root / "runtime-a"
            runtime_b = root / "runtime-b"
            environment_a = {
                "CYCPEP_DATA_DIR": str(runtime_a / "data"),
                "CYCPEP_EVIDENCE_DIR": str(runtime_a / "evidence"),
                "CYCPEP_DB_PATH": str(runtime_a / "formal" / "store.db"),
                "NP_DATA": str(runtime_a),
            }
            environment_b = {
                "CYCPEP_DATA_DIR": str(runtime_b / "data"),
                "CYCPEP_EVIDENCE_DIR": str(runtime_b / "evidence"),
                "CYCPEP_DB_PATH": str(runtime_b / "formal" / "store.db"),
                "NP_DATA": str(runtime_b),
            }
            world = _World()
            stores = []
            invocations = []
            dependencies = LauncherServiceDependencies(
                diagnostics=DiagnosticStore(root / "fixed-diagnostics"),
                load_context=lambda path: ProjectContext.from_runtime(path=path),
                validate_project=lambda _config: None,
                bind_context=bind_project_context,
                runtime_factory=lambda context, launcher_run_id: _StoreReceiptRuntime(
                    world, context, launcher_run_id, stores, invocations
                ),
                launcher_id=lambda: LAUNCHER_ID,
                restore_context=restore_project_context,
            )

            with patch.dict(os.environ, environment_a, clear=False):
                launched = launch_project(
                    project_path=project_path, dependencies=dependencies
                )
            with patch.dict(os.environ, environment_b, clear=False):
                status = status_launcher_run(
                    launcher_run_id=LAUNCHER_ID, dependencies=dependencies
                )
                resumed = resume_launcher_run(
                    launcher_run_id=LAUNCHER_ID, dependencies=dependencies
                )

            self.assertEqual(launched.payload.status, "awaiting_approval")
            self.assertEqual(status.payload.status, "awaiting_approval")
            self.assertEqual(resumed.payload.status, "awaiting_approval")
            self.assertEqual(invocations, ["research"])
            expected_database = (runtime_a / "formal" / "store.db").resolve()
            self.assertEqual([store.path for store in stores], [expected_database] * 3)
            receipts = stores[-1].query(
                project_id="project-1",
                agent="research",
                event_type="research_completion_receipt",
            )
            self.assertEqual(
                [item["event_id"] for item in receipts],
                ["runtime-locator-research-complete"],
            )
            self.assertTrue(expected_database.is_file())
            self.assertFalse((runtime_b / "formal" / "store.db").exists())
            self.assertFalse((runtime_b / "data").exists())
            self.assertFalse((runtime_b / "evidence").exists())

    def test_initial_create_failure_publishes_no_science_and_attempts_complete_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "runtime-a"
            world = _World()
            attempted = []
            runtimes = []

            def fail_create(_path, value):
                attempted.append(value)
                raise OSError("disk unavailable")

            deps = LauncherServiceDependencies(
                diagnostics=DiagnosticStore(Path(tmp) / "diagnostics", durable_writer=fail_create),
                load_context=lambda _path: _context(root),
                validate_project=lambda _config: None,
                bind_context=lambda _context: nullcontext(),
                runtime_factory=lambda *_args: runtimes.append(True),
                launcher_id=lambda: LAUNCHER_ID,
            )

            result = launch_project(project_path="approved.json", dependencies=deps)

            self.assertEqual(result.exit_code, 2)
            self.assertEqual(runtimes, [])
            self.assertEqual(world.calls, [])
            self.assertEqual(
                set(attempted[0]),
                {
                    "project_locator",
                    "data_dir",
                    "evidence_dir",
                    "database_path",
                    "execution_root",
                },
            )

    def test_status_and_resume_restore_original_locator_without_ambient_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context_a = _context(root / "runtime-a")
            context_b = _context(root / "runtime-b")
            world = _World()
            launch_loads = []
            restored = []
            runtime_contexts = []

            def launch_loader(_path):
                launch_loads.append(context_b if launch_loads else context_a)
                return launch_loads[-1]

            def restore_context(binding):
                restored.append(binding)
                return _restore(binding, dict(context_a.config))

            deps = LauncherServiceDependencies(
                diagnostics=DiagnosticStore(root / "diagnostics"),
                load_context=launch_loader,
                validate_project=lambda _config: None,
                bind_context=lambda _context: nullcontext(),
                runtime_factory=lambda context, _launcher_id: (
                    runtime_contexts.append(context) or _Runtime(world)
                ),
                launcher_id=lambda: LAUNCHER_ID,
                restore_context=restore_context,
            )

            launched = launch_project(project_path="approved.json", dependencies=deps)
            status = status_launcher_run(launcher_run_id=LAUNCHER_ID, dependencies=deps)
            resumed = resume_launcher_run(launcher_run_id=LAUNCHER_ID, dependencies=deps)

            self.assertEqual(launched.payload.status, "awaiting_approval")
            self.assertEqual(status.payload.status, "awaiting_approval")
            self.assertEqual(resumed.payload.status, "awaiting_approval")
            self.assertEqual(len(launch_loads), 1)
            self.assertEqual(len(restored), 3)
            for context in runtime_contexts:
                self.assertEqual(context.resolve_paths(), context_a.resolve_paths())
                self.assertNotEqual(context.resolve_paths(), context_b.resolve_paths())

    def test_missing_runtime_locator_fails_closed_before_runtime_construction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            world = _World()
            runtimes = []
            diagnostics = DiagnosticStore(root / "diagnostics")
            report = DiagnosticReport.initial(
                launcher_run_id=LAUNCHER_ID,
                project_id="project-1",
                approved_content_binding="approved-content",
                project_locator=str(root / "approved.json"),
                runtime_locator_binding=RuntimeLocatorBinding.from_context(
                    _context(root / "runtime-a"),
                    root / "approved.json",
                    execution_root=root / "runtime-a" / "execution",
                ),
            )
            diagnostics.create(report)
            path = diagnostics.root / f"{LAUNCHER_ID}.json"
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw.pop("runtime_locator_binding")
            path.write_text(json.dumps(raw), encoding="utf-8")
            deps = LauncherServiceDependencies(
                diagnostics=diagnostics,
                load_context=lambda _path: _context(root / "runtime-b"),
                validate_project=lambda _config: None,
                bind_context=lambda _context: nullcontext(),
                runtime_factory=lambda *_args: runtimes.append(True),
                launcher_id=lambda: LAUNCHER_ID,
            )

            result = resume_launcher_run(launcher_run_id=LAUNCHER_ID, dependencies=deps)

            self.assertEqual(result.exit_code, 2)
            self.assertEqual(result.payload.error.code, "launcher_runtime_locator_unavailable")
            self.assertEqual(runtimes, [])
            self.assertEqual(world.calls, [])

    def test_invalid_or_conflicting_locator_never_falls_back_to_current_paths(self):
        cases = (
            ("invalid", "launcher_diagnostic_invalid"),
            ("conflicting", "launcher_runtime_locator_conflict"),
        )
        for case, expected_code in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                world = _World()
                runtimes = []
                diagnostics = DiagnosticStore(root / "diagnostics")
                report = DiagnosticReport.initial(
                    launcher_run_id=LAUNCHER_ID,
                    project_id="project-1",
                    approved_content_binding="approved-content",
                    project_locator=str((root / "approved.json").resolve()),
                    runtime_locator_binding=RuntimeLocatorBinding.from_context(
                        _context(root / "runtime-a"),
                        root / "approved.json",
                        execution_root=root / "runtime-a" / "execution",
                    ),
                )
                diagnostics.create(report)
                path = diagnostics.root / f"{LAUNCHER_ID}.json"
                raw = json.loads(path.read_text(encoding="utf-8"))
                if case == "invalid":
                    raw["runtime_locator_binding"]["database_path"] = "relative/store.db"
                else:
                    raw["runtime_locator_binding"]["execution_root"] = str(
                        (root / "different-runtime" / "execution").resolve()
                    )
                path.write_text(json.dumps(raw), encoding="utf-8")
                deps = LauncherServiceDependencies(
                    diagnostics=diagnostics,
                    load_context=lambda _path: _context(root / "runtime-b"),
                    validate_project=lambda _config: None,
                    bind_context=lambda _context: nullcontext(),
                    runtime_factory=lambda *_args: runtimes.append(True),
                    launcher_id=lambda: LAUNCHER_ID,
                )

                result = status_launcher_run(
                    launcher_run_id=LAUNCHER_ID, dependencies=deps
                )

                self.assertEqual(result.exit_code, 2)
                self.assertEqual(result.payload.error.code, expected_code)
                self.assertEqual(runtimes, [])
                self.assertEqual(world.calls, [])

    def test_inaccessible_original_locator_is_structured_and_never_uses_runtime_b(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            world = _World()
            runtimes = []
            diagnostics = DiagnosticStore(root / "diagnostics")
            report = DiagnosticReport.initial(
                launcher_run_id=LAUNCHER_ID,
                project_id="project-1",
                approved_content_binding="approved-content",
                project_locator=str((root / "approved.json").resolve()),
                runtime_locator_binding=RuntimeLocatorBinding.from_context(
                    _context(root / "runtime-a"),
                    root / "approved.json",
                    execution_root=root / "runtime-a" / "execution",
                ),
            )
            diagnostics.create(report)
            deps = LauncherServiceDependencies(
                diagnostics=diagnostics,
                load_context=lambda _path: _context(root / "runtime-b"),
                validate_project=lambda _config: None,
                bind_context=lambda _context: nullcontext(),
                runtime_factory=lambda *_args: runtimes.append(True),
                launcher_id=lambda: LAUNCHER_ID,
                restore_context=lambda _binding: (_ for _ in ()).throw(
                    OSError("original store unavailable")
                ),
            )

            result = resume_launcher_run(
                launcher_run_id=LAUNCHER_ID, dependencies=deps
            )

            self.assertEqual(result.exit_code, 3)
            self.assertEqual(
                result.payload.error.code, "launcher_runtime_locator_unavailable"
            )
            self.assertEqual(runtimes, [])
            self.assertEqual(world.calls, [])


if __name__ == "__main__":
    unittest.main()
