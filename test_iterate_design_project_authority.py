"""Regression coverage for Store-backed iterate-Design project authority."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import data_layer
from agents.design import cli as design_cli
from contracts.candidate_update import (
    CANDIDATE_UPDATE_SCHEMA_VERSION,
    CandidateUpdate,
    CandidateUpdateBatch,
)
from contracts.transaction import TransactionContext, TransactionStatus
from core.context import ProjectContext
from data_layer import CandidateIndex, State
from execution.adapters import adapter_for
from execution.config import ExecutionConfig
from execution.contracts import ExecutionContractError
from execution.handlers import HandlerContext, iterate_design
from execution.worker import ExecutionWorker, _validate_action_result
from prediction_pipeline.contracts import object_sha256
from project_config import ProjectConfigError


class IterateDesignProjectAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="iterate-design-authority-"))
        self.original_paths = (
            data_layer.DATA_DIR,
            data_layer.EVIDENCE_DIR,
            data_layer.STATE_PATH,
            data_layer.LOG_PATH,
            data_layer.INDEX_PATH,
        )
        data_layer.DATA_DIR = self.root / "data"
        data_layer.EVIDENCE_DIR = self.root / "evidence"
        data_layer.STATE_PATH = data_layer.DATA_DIR / "state.json"
        data_layer.LOG_PATH = data_layer.EVIDENCE_DIR / "evidence_log.jsonl"
        data_layer.INDEX_PATH = data_layer.DATA_DIR / "candidate_index.csv"
        self.project = self._project("approved_project", coordinate_bound=True)
        State.save({**dict(State._default), "project_config": self.project})
        self.ambient_path = self.root / "ambient-project.json"
        self.ambient_path.write_text(
            json.dumps(self._project("ambient_project", coordinate_bound=False)),
            encoding="utf-8",
        )

    def tearDown(self):
        (
            data_layer.DATA_DIR,
            data_layer.EVIDENCE_DIR,
            data_layer.STATE_PATH,
            data_layer.LOG_PATH,
            data_layer.INDEX_PATH,
        ) = self.original_paths

    def _project(self, project_id: str, *, coordinate_bound: bool) -> dict:
        targets = []
        for target_id in ("MDM2", "MDMX"):
            target = {
                "id": target_id,
                "required": True,
                "design": {"lengths": [8]},
            }
            if coordinate_bound:
                coordinate = self.root / f"{target_id}.pdb"
                coordinate.write_text(f"MODEL {target_id}\n", encoding="utf-8")
                target["structure"] = {
                    "coordinate_path": str(coordinate),
                    "coordinate_sha256": "a" * 64,
                    "pdb_id": "1YCR" if target_id == "MDM2" else "3DAB",
                    "chain": "A",
                }
            targets.append(target)
        return {
            "schema_version": 1,
            "project_id": project_id,
            "modality": "head_to_tail_cyclic_peptide",
            "targets": targets,
            "review": {"status": "approved", "approved_digest": "b" * 64},
        }

    def _config(self) -> ExecutionConfig:
        return ExecutionConfig(
            repo_root=Path(__file__).resolve().parent,
            execution_root=self.root / "execution",
            core_python=Path(sys.executable),
            design_python=Path(sys.executable),
            prediction_python=Path(sys.executable),
            prediction_artifacts_root=self.root / "prediction-artifacts",
            prediction_runs_root=self.root / "prediction-runs",
            colabdesign_dir=self.root,
            colabdesign_params=self.root,
            cuda_data_dir=self.root,
            boltz_executable=None,
            boltz_cache=None,
            boltz_checkpoint=None,
            prodigy_executable=None,
            pyrosetta_python=None,
            control_data_path=None,
        )

    @staticmethod
    def _params(project: dict) -> dict:
        return {
            "project_config_digest": object_sha256(project),
            "design_jobs": [
                {
                    "route": "A", "target_id": "MDM2", "proposal_count": 1,
                    "lengths": [8], "seed": 11,
                },
                {
                    "route": "A", "target_id": "MDMX", "proposal_count": 1,
                    "lengths": [8], "seed": 12,
                },
            ],
        }

    @staticmethod
    def _packet() -> dict:
        return {
            "run_id": "run-authority",
            "task": {
                "task_id": "T001",
                "action": "iterate_design",
                "phase": "design",
                "resource_request": {"candidate_limit": 2, "class": "gpu"},
            },
        }

    def _task_dir(self) -> Path:
        task_dir = self._config().task_dir("run-authority", "T001", 1)
        task_dir.mkdir(parents=True, exist_ok=True)
        return task_dir

    def test_design_cli_explicit_project_context_and_legacy_omission(self):
        project_path = self.root / "approved-project.json"
        project_path.write_text(json.dumps(self.project), encoding="utf-8")
        contexts = []

        class FakeDesign:
            def __init__(self, context=None):
                contexts.append(context)

            def design_rfpeptides(self, target_spec=None, design_config=None):
                return []

        common = ["--route", "A", "--target", "MDM2", "--n", "1"]
        with patch("agents.design.cli.Design", FakeDesign), patch(
            "agents.design.cli.configure_candidate_updates"
        ), patch("agents.design.cli.flush_candidate_updates"):
            self.assertEqual(
                design_cli.main([*common, "--project-config", str(project_path)]), 0
            )
            self.assertEqual(design_cli.main(common), 0)

        self.assertIsInstance(contexts[0], ProjectContext)
        self.assertEqual(contexts[0].project_id, "approved_project")
        self.assertEqual(contexts[0].config["targets"][0]["structure"]["chain"], "A")
        self.assertIsNone(contexts[1])

    def test_design_cli_invalid_explicit_project_does_not_fallback(self):
        for explicit_path in (str(self.root / "missing.json"), ""):
            with self.subTest(explicit_path=explicit_path), patch(
                "agents.design.cli.Design"
            ) as design:
                with self.assertRaises(ProjectConfigError):
                    design_cli.main([
                        "--route", "A", "--target", "MDM2", "--n", "1",
                        "--project-config", explicit_path,
                    ])
                design.assert_not_called()

    def test_store_backed_project_snapshot_reaches_every_job_argv_and_environment(self):
        config = self._config()
        task_dir = self._task_dir()
        calls = []

        def fake_process(argv, **kwargs):
            snapshot = Path(argv[argv.index("--project-config") + 1])
            environment = kwargs["environment"]
            calls.append((snapshot, environment["CYCPEP_PROJECT_CONFIG"]))
            observed = json.loads(snapshot.read_text(encoding="utf-8"))
            self.assertEqual(observed, self.project)
            self.assertNotEqual(observed["project_id"], "ambient_project")
            updates_path = Path(argv[argv.index("--candidate-updates-path") + 1])
            updates_path.parent.mkdir(parents=True, exist_ok=True)
            updates_path.write_text(json.dumps(CandidateUpdateBatch(
                schema_version=CANDIDATE_UPDATE_SCHEMA_VERSION,
                emitter="design",
                job_id="empty",
                candidate_updates=(),
            ).to_dict()), encoding="utf-8")
            return {"elapsed_seconds": 0.01, "exit_code": 0}

        context = HandlerContext(
            packet=self._packet(), config=config, task_dir=task_dir,
            project_config=None,
        )
        with patch.dict(os.environ, {"CYCPEP_PROJECT_CONFIG": str(self.ambient_path)}), patch(
            "execution.handlers.validate_task_parameters",
            return_value=self._params(self.project),
        ), patch("execution.handlers.run_process", side_effect=fake_process):
            iterate_design(context)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], calls[1][0])
        self.assertEqual(calls[0][1], str(calls[0][0]))
        self.assertEqual(calls[1][1], str(calls[1][0]))

    def test_project_digest_drift_creates_no_snapshot_and_launches_no_process(self):
        config = self._config()
        task_dir = self._task_dir()
        params = self._params(self.project)
        params["project_config_digest"] = "0" * 64
        context = HandlerContext(
            packet=self._packet(), config=config, task_dir=task_dir,
            project_config=None,
        )
        with patch(
            "execution.handlers.validate_task_parameters", return_value=params
        ), patch("execution.handlers.run_process") as run:
            with self.assertRaises(ExecutionContractError) as error:
                iterate_design(context)
        self.assertEqual(error.exception.code, "project_config_drift")
        run.assert_not_called()
        self.assertEqual(list(task_dir.rglob("*project*.json")), [])

    def test_later_job_failure_does_not_publish_earlier_candidate(self):
        config = self._config()
        task_dir = self._task_dir()
        packet = self._packet()
        calls = 0

        def fake_process(argv, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ExecutionContractError(
                    "execution_process_failed", "second Design job failed"
                )
            candidate_dir = self.root / "candidate-C0001"
            candidate_dir.mkdir()
            pdb = candidate_dir / "design.pdb"
            manifest = candidate_dir / "manifest.json"
            pdb.write_text("MODEL\n", encoding="utf-8")
            manifest.write_text('{"candidate_id":"C0001"}', encoding="utf-8")
            updates_path = Path(argv[argv.index("--candidate-updates-path") + 1])
            updates_path.parent.mkdir(parents=True, exist_ok=True)
            updates_path.write_text(json.dumps(CandidateUpdateBatch(
                schema_version=CANDIDATE_UPDATE_SCHEMA_VERSION,
                emitter="design",
                job_id="job-one",
                candidate_updates=(CandidateUpdate({
                    "candidate_id": "C0001",
                    "sequence": "AAAAAAAA",
                    "source_route": "route_A_mdm2",
                    "source_batch": "batch-one",
                    "manifest_path": str(manifest),
                    "design_pdb_path": str(pdb),
                }),),
            ).to_dict()), encoding="utf-8")
            return {"elapsed_seconds": 0.01, "exit_code": 0}

        transaction = TransactionContext.create(
            workflow_id="workflow-authority",
            run_id="run-authority",
            task_id="T001",
            attempt_id="T001-A01",
            action="iterate_design",
        )
        store = data_layer.get_storage_backend()
        worker = ExecutionWorker(
            store, self.root / "staging", self.root / "formal-artifacts"
        )
        with patch(
            "execution.handlers.validate_task_parameters",
            return_value=self._params(self.project),
        ), patch("execution.handlers.run_process", side_effect=fake_process):
            with self.assertRaises(ExecutionContractError):
                worker.run(
                    transaction,
                    adapter_for(
                        "iterate_design", lambda _value: None, packet, config,
                        task_dir, None,
                    ),
                    validator=_validate_action_result,
                )

        self.assertEqual(transaction.status, TransactionStatus.FAILED)
        self.assertEqual(CandidateIndex.load(), [])
        self.assertEqual(
            [event for event in store.query(task_id="T001")
             if event.get("event_type") == "candidate_registered"],
            [],
        )
        self.assertFalse((task_dir / "outputs" / "design_task_result.json").exists())
        self.assertEqual(list((self.root / "formal-artifacts").rglob("*")), [])


if __name__ == "__main__":
    unittest.main()
