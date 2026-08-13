"""Launcher-scoped Workbench read isolation tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import data_layer
from core.context import ProjectContext, ProjectPaths
from workflow.diagnostics import DiagnosticStore
from workflow.errors import DiagnosticContractError
from workflow.models import DiagnosticReport, RuntimeLocatorBinding
from workflow.runtime_context import bind_project_context
from workflow.runtime_locator import require_formal_store
from workflow.service import LauncherServiceDependencies
from web_api.scoped_workbench import read_launcher_workbench


RUN_A = "launcher_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
RUN_B = "launcher_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
BINDING = "c" * 64


def _context(root: Path, project_id: str) -> ProjectContext:
    return ProjectContext(
        project_id=project_id,
        config={
            "project_id": project_id,
            "name": project_id,
            "targets": [{"id": project_id.upper()}],
            "review": {
                "status": "approved",
                "approved_digest": BINDING,
                "content_digest": BINDING,
            },
        },
        paths=ProjectPaths(
            data_dir=root / project_id / "data",
            evidence_dir=root / project_id / "evidence",
            database_path=root / project_id / "data" / "store.db",
        ),
    )


def _seed(context: ProjectContext) -> None:
    with bind_project_context(context):
        store = data_layer.get_storage_backend()
        store.replace_state(context.project_id, {
            "project_id": context.project_id,
            "project": context.project_id,
            "targets": {context.project_id.upper(): {}},
        })
        store.upsert({
            "candidate_id": f"candidate-{context.project_id}",
            "project_id": context.project_id,
            "sequence": "ACDEFGHI",
        })


def _dependencies(
    root: Path,
    contexts: dict[str, ProjectContext],
    *,
    restore_override=None,
) -> LauncherServiceDependencies:
    diagnostics = DiagnosticStore(root / "diagnostics")
    by_locator = {}
    for run_id, context in contexts.items():
        project_path = (root / f"{context.project_id}.json").resolve()
        locator = RuntimeLocatorBinding.from_context(
            context, project_path, execution_root=root / "execution"
        )
        by_locator[str(project_path)] = context
        diagnostics.create(DiagnosticReport.initial(
            launcher_run_id=run_id,
            project_id=context.project_id,
            approved_content_binding=BINDING,
            project_locator=str(project_path),
            runtime_locator_binding=locator,
        ))
    restore = restore_override or (lambda binding: by_locator[binding.project_locator])
    return LauncherServiceDependencies(
        diagnostics=diagnostics,
        load_context=lambda path: by_locator[str(Path(path).resolve())],
        validate_project=lambda _config: None,
        bind_context=bind_project_context,
        runtime_factory=lambda *_args: None,
        launcher_id=lambda: RUN_A,
        restore_context=restore,
        validate_formal_store=require_formal_store,
    )


class ScopedWorkbenchTests(unittest.TestCase):
    def test_alternating_launcher_projects_never_leak_and_restore_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context_a = _context(root, "project-a")
            context_b = _context(root, "project-b")
            startup = _context(root, "startup-project")
            for context in (context_a, context_b, startup):
                _seed(context)
            deps = _dependencies(root, {RUN_A: context_a, RUN_B: context_b})

            with bind_project_context(startup):
                original_project = data_layer.ACTIVE_PROJECT_CONFIG["project_id"]
                first = read_launcher_workbench(
                    launcher_run_id=RUN_A, launcher_dependencies=deps
                )
                self.assertEqual(
                    data_layer.ACTIVE_PROJECT_CONFIG["project_id"], original_project
                )
                second = read_launcher_workbench(
                    launcher_run_id=RUN_B, launcher_dependencies=deps
                )
                third = read_launcher_workbench(
                    launcher_run_id=RUN_A, launcher_dependencies=deps
                )
                self.assertEqual(
                    data_layer.ACTIVE_PROJECT_CONFIG["project_id"], original_project
                )

            self.assertEqual(first["project"]["project_id"], "project-a")
            self.assertEqual(second["project"]["project_id"], "project-b")
            self.assertEqual(third["project"]["project_id"], "project-a")
            self.assertEqual(
                {item["candidate_id"] for item in first["candidates"]["items"]},
                {"candidate-project-a"},
            )
            self.assertEqual(
                {item["candidate_id"] for item in second["candidates"]["items"]},
                {"candidate-project-b"},
            )
            first["candidates"]["items"][0]["sequence"] = "MUTATED"
            self.assertEqual(
                third["candidates"]["items"][0]["sequence"], "ACDEFGHI"
            )

    def test_missing_corrupt_and_conflicting_binding_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = _context(root, "project-a")
            other = _context(root, "project-b")
            _seed(context)
            _seed(other)
            deps = _dependencies(root, {RUN_A: context})

            with self.assertRaises(DiagnosticContractError) as missing:
                read_launcher_workbench(
                    launcher_run_id=RUN_B, launcher_dependencies=deps
                )
            self.assertEqual(missing.exception.code, "launcher_diagnostic_not_found")

            sidecar = root / "diagnostics" / f"{RUN_A}.runtime-locator.json"
            sidecar.write_text("{invalid", encoding="utf-8")
            with self.assertRaises(DiagnosticContractError) as corrupt:
                read_launcher_workbench(
                    launcher_run_id=RUN_A, launcher_dependencies=deps
                )
            self.assertEqual(corrupt.exception.code, "launcher_runtime_locator_unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = _context(root, "project-a")
            other = _context(root, "project-b")
            _seed(context)
            _seed(other)
            deps = _dependencies(
                root,
                {RUN_A: context},
                restore_override=lambda _binding: other,
            )
            with self.assertRaises(DiagnosticContractError) as conflict:
                read_launcher_workbench(
                    launcher_run_id=RUN_A, launcher_dependencies=deps
                )
            self.assertEqual(conflict.exception.code, "control_binding_conflict")


if __name__ == "__main__":
    unittest.main()
