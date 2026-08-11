"""RED characterization for the official runtime path contracts.

``ProjectContext`` owns explicit data and Evidence directories without reading
the environment.  Data Layer owns selection of the formal SQLite database via
its public ``SQLITE_DB_PATH`` runtime setting, with ``<data>/store.db`` only as
the fallback.  The Launcher compatibility binding must compose those seams;
it must not replace an explicitly selected formal database.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import data_layer
from agents import research
from core.context import ProjectContext, ProjectPaths
from workflow.runtime_context import bind_project_context


def _context(root: Path) -> ProjectContext:
    return ProjectContext.from_config(
        {
            "project_id": "runtime_path_characterization",
            "name": "runtime path characterization",
            "targets": [{"id": "TARGET", "uniprot": "P00001"}],
            "review": {
                "status": "approved",
                "approved_digest": "approved-content",
                "content_digest": "approved-content",
            },
        },
        paths=ProjectPaths(
            data_dir=root / "custom-data",
            evidence_dir=root / "custom-evidence",
            output_dir=root / "custom-output",
            database_path=root / "formal-database" / "project.sqlite3",
        ),
    )


class WorkflowRuntimePathCharacterizationTests(unittest.TestCase):
    def test_binding_composes_context_directories_with_official_database_path(self):
        """An explicit Data Layer database remains the sole formal Store path."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = _context(root)
            resolved = context.resolve_paths()
            database_path = resolved.database_path

            with patch.object(
                data_layer,
                "SQLITE_DB_PATH",
                root / "ambient-database" / "wrong.sqlite3",
            ):
                with bind_project_context(context):
                    store = data_layer.get_storage_backend()

                    self.assertEqual(Path(data_layer.DATA_DIR), resolved.data_dir)
                    self.assertEqual(Path(data_layer.EVIDENCE_DIR), resolved.evidence_dir)
                    self.assertEqual(Path(data_layer.STATE_PATH), resolved.data_dir / "state.json")
                    self.assertEqual(
                        Path(data_layer.LOG_PATH),
                        resolved.evidence_dir / "evidence_log.jsonl",
                    )
                    self.assertEqual(Path(data_layer.SQLITE_DB_PATH), database_path)
                    self.assertEqual(store.path, database_path)

    def test_official_runtime_context_freezes_documented_environment_paths(self):
        """Environment is resolved by ProjectContext once, before coordination."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database_path = root / "environment-database" / "project.sqlite3"
            data_path = root / "environment-data"
            evidence_path = root / "environment-evidence"
            with patch.dict(os.environ, {
                "CYCPEP_DATA_DIR": str(data_path),
                "CYCPEP_EVIDENCE_DIR": str(evidence_path),
                "CYCPEP_DB_PATH": str(database_path),
            }):
                context = ProjectContext.from_runtime_config(dict(_context(root).config))

            with patch.dict(os.environ, {
                "CYCPEP_DATA_DIR": str(root / "drifted-data"),
                "CYCPEP_EVIDENCE_DIR": str(root / "drifted-evidence"),
                "CYCPEP_DB_PATH": str(root / "drifted.sqlite3"),
            }):
                with bind_project_context(context):
                    self.assertEqual(Path(data_layer.DATA_DIR), data_path)
                    self.assertEqual(Path(data_layer.EVIDENCE_DIR), evidence_path)
                    self.assertEqual(Path(data_layer.SQLITE_DB_PATH), database_path)
                    self.assertEqual(data_layer.get_storage_backend().path, database_path)

    def test_exception_restores_all_official_bindings_and_store_selection(self):
        """Failure restores the prior Data Layer and Research runtime context."""

        path_names = (
            "ACTIVE_PROJECT_CONFIG",
            "DATA_DIR",
            "EVIDENCE_DIR",
            "STATE_PATH",
            "LOG_PATH",
            "INDEX_PATH",
            "SQLITE_DB_PATH",
        )
        research_names = (
            "PROJECT_CONFIG",
            "DATA_DIR",
            "EVIDENCE_DIR",
            "CACHE_PATH",
            "THRESHOLDS_CACHE",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prior_database = root / "prior-runtime" / "prior.sqlite3"
            context = _context(root)

            with patch.object(data_layer, "SQLITE_DB_PATH", prior_database):
                prior_data = {name: getattr(data_layer, name) for name in path_names}
                prior_research = {name: getattr(research, name) for name in research_names}
                prior_runtime_paths = data_layer._runtime_paths
                prior_state_project = data_layer.State.__dict__["_project_config"]
                prior_state_default = data_layer.State.__dict__["_default"]

                with self.assertRaisesRegex(RuntimeError, "characterized failure"):
                    with bind_project_context(context):
                        self.assertNotEqual(
                            data_layer.get_storage_backend().project_id,
                            prior_data["ACTIVE_PROJECT_CONFIG"]["project_id"],
                        )
                        raise RuntimeError("characterized failure")

                for name, value in prior_data.items():
                    self.assertEqual(getattr(data_layer, name), value, name)
                for name, value in prior_research.items():
                    self.assertEqual(getattr(research, name), value, name)
                self.assertIs(data_layer._runtime_paths, prior_runtime_paths)
                self.assertIs(
                    data_layer.State.__dict__["_project_config"], prior_state_project
                )
                self.assertIs(data_layer.State.__dict__["_default"], prior_state_default)
                self.assertEqual(data_layer.get_storage_backend().path, prior_database)


if __name__ == "__main__":
    unittest.main()
