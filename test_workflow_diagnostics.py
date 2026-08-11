"""Focused tests for the Workflow Launcher diagnostic boundary."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow.diagnostics import DiagnosticStore, resolve_diagnostics_root
from workflow.errors import DiagnosticContractError, normalize_error
from workflow.models import (
    DiagnosticReport,
    FormalTrace,
    LauncherCommandResult,
    OpaqueReference,
    PredictionRunLocator,
    RuntimeLocatorBinding,
    StructuredError,
)


LAUNCHER_RUN_ID = "launcher_0123456789abcdef0123456789abcdef"


def _report() -> DiagnosticReport:
    return DiagnosticReport.initial(
        launcher_run_id=LAUNCHER_RUN_ID,
        project_id="project-1",
        approved_content_binding="approval-1",
        project_locator="C:/internal/projects/approved-project.json",
    )


def _bound_report(root: Path) -> DiagnosticReport:
    project = (root / "approved-project.json").resolve()
    return DiagnosticReport.initial(
        launcher_run_id=LAUNCHER_RUN_ID,
        project_id="project-1",
        approved_content_binding="approval-1",
        project_locator=str(project),
        runtime_locator_binding=RuntimeLocatorBinding(
            project_locator=str(project),
            data_dir=str((root / "runtime" / "data").resolve()),
            evidence_dir=str((root / "runtime" / "evidence").resolve()),
            database_path=str((root / "runtime" / "formal" / "store.db").resolve()),
            execution_root=str((root / "runtime" / "execution").resolve()),
        ),
    )


class DiagnosticModelTests(unittest.TestCase):
    def test_browser_projection_excludes_internal_locators_and_authority_claims(self):
        report = _report().with_observation(
            current_boundary="prediction",
            prediction_invocation_id="prediction_invocation_0123456789abcdef0123456789abcdef",
            prediction_run_id="prediction_0123456789abcdef0123456789abcdef",
            prediction_run_locator=PredictionRunLocator(
                root="C:/internal/prediction/root",
                run_id="prediction_0123456789abcdef0123456789abcdef",
            ),
            formal_trace=FormalTrace(workflow_id="workflow-1", plan_id="plan-1"),
            evidence_ids=("evidence-1",),
            artifact_ids=("artifact-1",),
            last_known_formal_status="awaiting_approval",
        )

        projection = report.browser_projection(status="awaiting_approval").to_dict()

        self.assertNotIn("project_locator", projection)
        self.assertNotIn("prediction_run_locator", projection)
        self.assertNotIn("C:/internal", json.dumps(projection))
        self.assertEqual(projection["formal_trace"]["run_id"], None)
        self.assertEqual(projection["prediction_run_id"], report.prediction_run_id)
        self.assertNotIn("task_status", projection)
        self.assertNotIn("transaction_status", projection)

    def test_model_round_trip_preserves_only_observations(self):
        report = _report().with_observation(
            current_boundary="research",
            last_completed_boundary="project_approval",
            input_refs=(OpaqueReference("project", "project-1"),),
        )

        restored = DiagnosticReport.from_dict(report.to_dict())

        self.assertEqual(restored, report)
        self.assertEqual(restored.input_refs[0].kind, "project")

    def test_structured_errors_are_sanitized_at_the_model_boundary(self):
        error = normalize_error(
            RuntimeError(
                "token=super-secret\nTraceback (most recent call last):\n"
                "  File C:/private/launcher.py, line 7"
            ),
            component="launcher",
        )

        self.assertEqual(error.code, "RuntimeError")
        self.assertNotIn("super-secret", error.message)
        self.assertNotIn("Traceback", error.message)
        self.assertNotIn("C:/private", error.message)

    def test_command_result_rejects_non_browser_result_payload(self):
        with self.assertRaises(TypeError):
            LauncherCommandResult(payload={"status": "failed"}, exit_code=2)  # type: ignore[arg-type]


class DiagnosticStoreTests(unittest.TestCase):
    def test_root_resolution_has_documented_precedence(self):
        repository_root = Path("C:/repo")
        self.assertEqual(
            resolve_diagnostics_root(
                env={"CYCPEP_LAUNCHER_DIAGNOSTICS": "D:/diagnostics", "NP_DATA": "E:/np"},
                repository_root=repository_root,
            ),
            Path("D:/diagnostics"),
        )
        self.assertEqual(
            resolve_diagnostics_root(env={"NP_DATA": "E:/np"}, repository_root=repository_root),
            repository_root / "data" / "launcher_diagnostics",
        )
        self.assertEqual(
            resolve_diagnostics_root(env={}, repository_root=repository_root),
            repository_root / "data" / "launcher_diagnostics",
        )

    def test_default_root_is_stable_across_formal_runtime_selector_drift(self):
        repository_root = Path("C:/repo")

        launch_root = resolve_diagnostics_root(
            env={"NP_DATA": "D:/runtime-a", "CYCPEP_DB_PATH": "D:/runtime-a/store.db"},
            repository_root=repository_root,
        )
        later_command_root = resolve_diagnostics_root(
            env={"NP_DATA": "E:/runtime-b", "CYCPEP_DB_PATH": "E:/runtime-b/store.db"},
            repository_root=repository_root,
        )

        self.assertEqual(launch_root, repository_root / "data" / "launcher_diagnostics")
        self.assertEqual(later_command_root, launch_root)

    def test_create_persists_write_once_runtime_locator_before_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            writes = []

            def record_write(path: Path, value: dict) -> None:
                writes.append((path.name, value))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value), encoding="utf-8")

            store = DiagnosticStore(root, durable_writer=record_write)
            report = _bound_report(root)

            store.create(report)

            self.assertEqual(
                [name for name, _value in writes],
                [
                    f"{LAUNCHER_RUN_ID}.runtime-locator.json",
                    f"{LAUNCHER_RUN_ID}.json",
                ],
            )
            self.assertEqual(writes[0][1], report.runtime_locator_binding.to_dict())
            self.assertEqual(store.read(LAUNCHER_RUN_ID), report)

    def test_missing_or_invalid_sidecar_fails_closed_for_bound_journal(self):
        for case in ("missing", "invalid"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = DiagnosticStore(root)
                report = _bound_report(root)
                store.create(report)
                sidecar = root / f"{LAUNCHER_RUN_ID}.runtime-locator.json"
                if case == "missing":
                    sidecar.unlink()
                else:
                    sidecar.write_text('{"database_path":"relative.db"}', encoding="utf-8")

                with self.assertRaises(DiagnosticContractError) as caught:
                    store.read(LAUNCHER_RUN_ID)

                self.assertEqual(caught.exception.code, "launcher_runtime_locator_unavailable")

    def test_journal_cannot_redirect_to_another_valid_absolute_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = DiagnosticStore(root)
            report = _bound_report(root)
            store.create(report)
            journal = root / f"{LAUNCHER_RUN_ID}.json"
            raw = json.loads(journal.read_text(encoding="utf-8"))
            raw["runtime_locator_binding"]["database_path"] = str(
                (root / "other-runtime" / "formal" / "store.db").resolve()
            )
            journal.write_text(json.dumps(raw), encoding="utf-8")

            with self.assertRaises(DiagnosticContractError) as caught:
                store.read(LAUNCHER_RUN_ID)

            self.assertEqual(caught.exception.code, "launcher_runtime_locator_conflict")

    def test_write_revalidates_sidecar_before_persisting_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = DiagnosticStore(root)
            report = _bound_report(root)
            store.create(report)
            sidecar = root / f"{LAUNCHER_RUN_ID}.runtime-locator.json"
            raw = json.loads(sidecar.read_text(encoding="utf-8"))
            raw["database_path"] = str((root / "other" / "store.db").resolve())
            sidecar.write_text(json.dumps(raw), encoding="utf-8")

            with self.assertRaises(DiagnosticContractError) as caught:
                store.write(report.with_observation(current_boundary="design"))

            self.assertEqual(caught.exception.code, "launcher_runtime_locator_conflict")

    def test_legacy_draft_without_locator_sidecar_remains_readable_but_not_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = DiagnosticStore(root)
            report = _report()
            store.create(report)

            self.assertEqual(store.read(LAUNCHER_RUN_ID), report)
            with self.assertRaises(DiagnosticContractError) as caught:
                store.write(report.with_observation(current_boundary="design"))
            self.assertEqual(caught.exception.code, "launcher_runtime_locator_unavailable")

    def test_direct_validated_lookup_and_durable_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DiagnosticStore(Path(tmp))
            created = _report()
            store.create(created)

            restored = store.read(LAUNCHER_RUN_ID)

            self.assertEqual(restored, created)
            self.assertEqual(list(Path(tmp).glob("*.json")), [Path(tmp) / f"{LAUNCHER_RUN_ID}.json"])

    def test_invalid_id_cannot_escape_or_trigger_enumeration(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DiagnosticStore(Path(tmp))

            for invalid in ("../launcher_bad", "launcher_bad", "", "launcher_ABC"):
                with self.subTest(invalid=invalid), self.assertRaises(DiagnosticContractError):
                    store.read(invalid)

    def test_missing_valid_id_is_looked_up_directly_without_enumeration(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DiagnosticStore(Path(tmp))
            with (
                patch("workflow.diagnostics.os.scandir", side_effect=AssertionError("enumerated")),
                patch.object(Path, "glob", side_effect=AssertionError("enumerated")),
                self.assertRaises(DiagnosticContractError) as caught,
            ):
                store.read(LAUNCHER_RUN_ID)

            self.assertEqual(caught.exception.code, "launcher_diagnostic_not_found")

    def test_initial_write_failure_does_not_publish_partial_report(self):
        def fail_write(path: Path, value: dict) -> None:
            raise OSError("disk unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            store = DiagnosticStore(Path(tmp), durable_writer=fail_write)

            with self.assertRaises(DiagnosticContractError) as caught:
                store.create(_report())

            self.assertEqual(caught.exception.code, "launcher_diagnostic_persistence_failed")
            self.assertFalse((Path(tmp) / f"{LAUNCHER_RUN_ID}.json").exists())

    def test_locked_session_updates_only_its_bound_run_without_relocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = DiagnosticStore(root)
            store.create(_bound_report(root))

            with store.locked(LAUNCHER_RUN_ID) as session:
                updated = session.read().with_observation(current_boundary="design")
                session.write(updated)

            self.assertEqual(store.read(LAUNCHER_RUN_ID).current_boundary, "design")

    def test_edited_diagnostic_cannot_claim_formal_completion(self):
        raw = _report().to_dict()
        raw["last_completed_boundary"] = "execution"
        raw["last_known_formal_status"] = "completed"

        restored = DiagnosticReport.from_dict(raw)

        # These are explicitly observations.  The model exposes no transition,
        # completion, task, or transaction mutation API.
        self.assertEqual(restored.last_completed_boundary, "execution")
        self.assertEqual(restored.last_known_formal_status, "completed")
        self.assertFalse(hasattr(restored, "authorize_transition"))
        self.assertFalse(hasattr(restored, "mark_task_complete"))


if __name__ == "__main__":
    unittest.main()
