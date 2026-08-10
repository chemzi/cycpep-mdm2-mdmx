"""Tests for the Launcher's process-scoped legacy Data Layer adapter."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import data_layer
from contracts.event import EvidenceEvent
from core.context import ProjectContext, ProjectPaths
from workflow.runtime_context import bind_project_context


def _project(project_id: str, root: Path) -> ProjectContext:
    config = {
        "project_id": project_id,
        "name": project_id,
        "targets": [{"id": "TARGET", "uniprot": "P00001"}],
        "review": {
            "status": "approved",
            "approved_digest": "approved-content",
            "content_digest": "approved-content",
        },
    }
    return ProjectContext.from_config(
        config,
        paths=ProjectPaths(
            data_dir=root / "data",
            evidence_dir=root / "evidence",
            output_dir=root / "output",
        ),
    )


class ProjectRuntimeBindingTests(unittest.TestCase):
    def test_arbitrary_project_receipt_uses_bound_store_and_context_is_restored(self):
        before = {
            name: vars(data_layer).get(name)
            for name in (
                "ACTIVE_PROJECT_CONFIG",
                "DATA_DIR",
                "EVIDENCE_DIR",
                "STATE_PATH",
                "LOG_PATH",
                "INDEX_PATH",
                "SQLITE_DB_PATH",
            )
        }
        before_keys = set(vars(data_layer))
        before_state_project = data_layer.State.__dict__["_project_config"]
        before_state_default = data_layer.State.__dict__["_default"]
        with tempfile.TemporaryDirectory() as tmp:
            context = _project("arbitrary_project", Path(tmp))
            with bind_project_context(context):
                store = data_layer.get_storage_backend()
                event = EvidenceEvent(
                    timestamp="2026-08-10T00:00:00+00:00",
                    event_id="formal-receipt-1",
                    agent="research",
                    event_type="research_invocation_started",
                    payload={
                        "project_id": context.project_id,
                        "launcher_run_id": "launcher-1",
                    },
                    phase="research",
                ).to_dict()
                store.append(event)

                self.assertEqual(store.project_id, "arbitrary_project")
                self.assertEqual(store.query(project_id="arbitrary_project")[0]["event_id"], "formal-receipt-1")
                self.assertEqual(Path(data_layer.SQLITE_DB_PATH), Path(tmp) / "data" / "store.db")

            self.assertTrue((Path(tmp) / "data" / "store.db").is_file())

        self.assertEqual(set(vars(data_layer)), before_keys)
        for name, value in before.items():
            self.assertIs(vars(data_layer).get(name), value)
        self.assertIs(data_layer.State.__dict__["_project_config"], before_state_project)
        self.assertIs(data_layer.State.__dict__["_default"], before_state_default)

    def test_binding_restores_context_after_exception(self):
        original_runtime_paths = data_layer._runtime_paths
        with tempfile.TemporaryDirectory() as tmp:
            context = _project("exception_project", Path(tmp))
            with self.assertRaisesRegex(RuntimeError, "stop"):
                with bind_project_context(context):
                    raise RuntimeError("stop")
        self.assertIs(data_layer._runtime_paths, original_runtime_paths)


if __name__ == "__main__":
    unittest.main()
