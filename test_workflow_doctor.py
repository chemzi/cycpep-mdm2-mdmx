"""Focused contracts for the read-only workflow runtime doctor."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from execution.config import ExecutionConfig
from prediction_pipeline.boltz_worker import BOLTZ2_CHECKPOINT_SHA256, BOLTZ_VERSION
from prediction_pipeline.contracts import PredictionConfig
from prediction_pipeline.execution_identity import PRODIGY_VERSION
from prediction_pipeline.rosetta_worker import PYROSETTA_VERSION
from project_config import normalize_project_config
from storage import SQLiteStore
from target_bootstrap import config_digest
from workflow.cli import CommandHandlers, main


class FakeHostProbes:
    def __init__(self, *, environment=None, unavailable=()):
        self.environment = dict(environment or {})
        self.unavailable = {str(Path(value)) for value in unavailable}

    def getenv(self, name):
        return self.environment.get(name)

    def path_metadata(self, path):
        value = Path(path)
        exists = str(value) not in self.unavailable and value.exists()
        return {"path": str(value), "exists": exists, "is_file": exists and value.is_file(),
                "is_dir": exists and value.is_dir()}

    def writable_parent(self, path):
        value = Path(path)
        while not value.exists() and value != value.parent:
            value = value.parent
        return {"path": str(value), "writable": str(value) not in self.unavailable}

    def gpu_observation(self, timeout):
        return {"model": "Test GPU", "driver": "999.0", "memory_mb": 40960}


def approved_project(root: Path, *, structure_plan=None) -> Path:
    coordinate = root / "target.pdb"
    coordinate.write_text("ATOM\n", encoding="utf-8")
    target = {
        "id": "TARGET",
        "required": True,
        "structure": {
            "pdb_id": "1ABC",
            "coordinate_path": str(coordinate),
            "coordinate_sha256": hashlib.sha256(coordinate.read_bytes()).hexdigest(),
        },
    }
    if structure_plan is not None:
        target["structure_plan"] = structure_plan
    project = normalize_project_config({"project_id": "doctor-test", "targets": [target]})
    project["review"] = {"status": "approved", "approved_digest": config_digest(project)}
    path = root / "approved.json"
    path.write_text(json.dumps(project), encoding="utf-8")
    return path


def execution_config(root: Path) -> ExecutionConfig:
    tool = root / "tool"
    tool.write_text("", encoding="utf-8")
    checkpoint = root / "checkpoint"
    checkpoint.write_text("checkpoint", encoding="utf-8")
    return ExecutionConfig(
        repo_root=root,
        execution_root=root / "execution",
        core_python=tool,
        design_python=tool,
        prediction_python=tool,
        prediction_artifacts_root=root / "artifacts",
        prediction_runs_root=root / "prediction-runs",
        colabdesign_dir=root,
        colabdesign_params=root,
        cuda_data_dir=root,
        boltz_executable=tool,
        boltz_cache=root,
        boltz_checkpoint=checkpoint,
        prodigy_executable=tool,
        pyrosetta_python=tool,
        control_data_path=None,
    )


class WorkflowDoctorTests(unittest.TestCase):
    def test_cli_text_json_exit_codes_and_three_field_handlers(self):
        from workflow.doctor import DoctorCheck, DoctorReport

        ready = DoctorReport("fresh_full_launcher", "approved.json", (
            DoctorCheck("project.approval", "project", "required", "pass", "approved", None),
        ))
        not_ready = DoctorReport("fresh_full_launcher", "bad.json", (
            DoctorCheck("project.approval", "project", "required", "fail", "invalid", "Project owner: approve it"),
        ))
        handlers = CommandHandlers(lambda **_: None, lambda **_: None, lambda **_: None)
        text = io.StringIO()
        code = main(["doctor", "--project", "approved.json"], handlers=handlers,
                    doctor_handler=lambda **_: ready, stdout=text)
        self.assertEqual(code, 0)
        self.assertTrue(text.getvalue().rstrip().endswith("READY"))
        self.assertIn("category=project", text.getvalue())
        self.assertIn("requirement=required", text.getvalue())
        self.assertIn("[PASS] project.approval", text.getvalue())

        machine = io.StringIO()
        code = main(["doctor", "--project", "bad.json", "--json"], handlers=handlers,
                    doctor_handler=lambda **_: not_ready, stdout=machine)
        payload = json.loads(machine.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["checks"][0]["id"], "project.approval")
        self.assertEqual(payload["checks"][0]["category"], "project")
        self.assertEqual(payload["checks"][0]["requirement"], "required")

    def test_doctor_invalid_input_is_normalized_without_traceback(self):
        output = io.StringIO()
        code = main(["doctor", "--json"], stdout=output)
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["checks"][0]["id"], "doctor.input")
        self.assertNotIn("Traceback", output.getvalue())

    def test_doctor_internal_failure_is_not_misreported_as_invalid_input(self):
        output = io.StringIO()

        def fail_service(**_):
            raise RuntimeError("sensitive runtime detail")

        code = main(
            ["doctor", "--project", "approved.json", "--json"],
            doctor_handler=fail_service,
            stdout=output,
        )
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["checks"][0]["id"], "doctor.runtime")
        self.assertNotIn("doctor.input", output.getvalue())
        self.assertNotIn("sensitive runtime detail", output.getvalue())

    def test_service_accepts_legacy_target_and_does_not_create_fresh_store(self):
        from workflow.doctor import run_doctor

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = approved_project(root, structure_plan=None)
            project_before = project.read_bytes()
            database = root / "fresh" / "store.db"
            config = execution_config(root)
            probes = FakeHostProbes(environment={"OPENAI_API_KEY": "super-secret"})
            with patch.dict(os.environ, {"CYCPEP_DB_PATH": str(database)}, clear=False), patch(
                "workflow.doctor.ExecutionConfig.from_environment", return_value=config
            ), patch("workflow.doctor.validate_colabdesign_runtime", return_value="commit"), patch(
                "workflow.doctor.validate_boltz_runtime",
                return_value={"version": "2.2.1", "checkpoint_sha256": "a" * 64},
            ), patch("workflow.doctor.validate_pyrosetta_runtime", return_value="pyrosetta"), patch(
                "workflow.doctor.validate_prodigy_runtime", return_value="2.4.0"
            ):
                report = run_doctor(project, probes=probes)

            checks = {item.id: item for item in report.checks}
            self.assertEqual(checks["project.coordinates.target"].status, "pass")
            self.assertEqual(checks["project.approval"].status, "pass")
            self.assertEqual(checks["store.formal"].status, "pass")
            self.assertIn("store_will_initialize_on_launch", checks["store.formal"].observation)
            self.assertFalse(database.exists())
            self.assertFalse(database.parent.exists())
            self.assertEqual(project.read_bytes(), project_before)
            self.assertNotIn("super-secret", json.dumps(report.to_dict()))

    def test_service_accumulates_independent_runtime_failures_and_redacts_key(self):
        from workflow.doctor import run_doctor

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = approved_project(root)
            config = execution_config(root)
            config.boltz_executable.unlink()
            probes = FakeHostProbes(environment={"OPENAI_API_KEY": "leaked-value"})
            probes.gpu_observation = lambda timeout: (_ for _ in ()).throw(RuntimeError("no GPU leaked-value"))
            with patch("workflow.doctor.ExecutionConfig.from_environment", return_value=config), patch(
                "workflow.doctor.validate_colabdesign_runtime", side_effect=ValueError("wrong commit leaked-value")
            ), patch("workflow.doctor.validate_boltz_runtime", side_effect=ValueError("wrong checkpoint")), patch(
                "workflow.doctor.validate_pyrosetta_runtime", return_value="pyrosetta"
            ), patch("workflow.doctor.validate_prodigy_runtime", return_value="2.4.0"):
                report = run_doctor(project, probes=probes)

            checks = {item.id: item for item in report.checks}
            self.assertEqual(checks["runtime.colabdesign"].status, "fail")
            self.assertEqual(checks["runtime.boltz"].status, "fail")
            self.assertEqual(checks["runtime.gpu"].status, "fail")
            self.assertFalse(report.ready)
            self.assertNotIn("leaked-value", json.dumps(report.to_dict()))

    def test_explicit_project_wins_and_coordinate_hash_mismatch_fails(self):
        from workflow.doctor import run_doctor

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = approved_project(root)
            ambient = root / "ambient.json"
            ambient.write_text('{"targets": []}', encoding="utf-8")
            selected_before = selected.read_bytes()
            config = execution_config(root)
            Path(json.loads(selected.read_text(encoding="utf-8"))["targets"][0]["structure"]["coordinate_path"]).write_text(
                "CHANGED\n", encoding="utf-8"
            )
            probes = FakeHostProbes(environment={"OPENAI_API_KEY": "present"})
            with patch.dict(os.environ, {"CYCPEP_PROJECT_CONFIG": str(ambient)}, clear=False), patch(
                "workflow.doctor.ExecutionConfig.from_environment", return_value=config
            ), patch("workflow.doctor.validate_colabdesign_runtime", return_value="commit"), patch(
                "workflow.doctor.validate_boltz_runtime", return_value={"version": BOLTZ_VERSION, "checkpoint_sha256": BOLTZ2_CHECKPOINT_SHA256}
            ), patch("workflow.doctor.validate_pyrosetta_runtime", return_value=PYROSETTA_VERSION), patch(
                "workflow.doctor.validate_prodigy_runtime", return_value=PRODIGY_VERSION
            ):
                report = run_doctor(selected, probes=probes)

            checks = {item.id: item for item in report.checks}
            self.assertEqual(checks["project.approval"].status, "pass")
            self.assertEqual(checks["project.coordinates.target"].status, "fail")
            self.assertEqual(selected.read_bytes(), selected_before)

    def test_existing_store_is_validated_read_only_and_runtime_authorities_are_called(self):
        from workflow.doctor import run_doctor

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = approved_project(root)
            database = root / "store.db"
            SQLiteStore(database, project_id="doctor-test")
            database_before = database.read_bytes()
            config = execution_config(root)
            (root / "scripts").mkdir()
            (root / "scripts" / "run_inference.py").write_text("", encoding="utf-8")
            (root / "run.py").write_text("", encoding="utf-8")
            rfdiff_conda = root / "rfdiff-conda"
            (rfdiff_conda / "lib" / "python3.10" / "site-packages").mkdir(parents=True)
            design_cuda = root / "design-cuda"
            design_cuda.mkdir()
            probes = FakeHostProbes(environment={"OPENAI_API_KEY": "present"})
            with patch.dict(os.environ, {"CYCPEP_DB_PATH": str(database)}, clear=False), patch.multiple(
                "workflow.doctor.design_config",
                DEFAULT_OUTPUT_DIR=root,
                RFDIFF_CONDA=str(rfdiff_conda),
                RFDIFF_PYTHON=str(config.design_python),
                RFDIFF_DIR=str(root),
                LIGANDMPNN_DIR=str(root),
                LIGANDMPNN_CHECKPOINT=str(config.boltz_checkpoint),
                SE3_ROOT=str(root),
                CYCPEP_PYTHON=str(config.design_python),
                CUDA_DATA_DIR=str(design_cuda),
            ), patch(
                "workflow.doctor.ExecutionConfig.from_environment", return_value=config
            ), patch("workflow.doctor.validate_colabdesign_runtime", return_value="observed") as colab, patch(
                "workflow.doctor.validate_boltz_runtime",
                return_value={"version": BOLTZ_VERSION, "checkpoint_sha256": BOLTZ2_CHECKPOINT_SHA256},
            ) as boltz, patch(
                "workflow.doctor.validate_pyrosetta_runtime", return_value=PYROSETTA_VERSION
            ) as pyrosetta, patch(
                "workflow.doctor.validate_prodigy_runtime", return_value=PRODIGY_VERSION
            ) as prodigy:
                report = run_doctor(selected, probes=probes)
                blocked = run_doctor(
                    selected,
                    probes=FakeHostProbes(
                        environment={"OPENAI_API_KEY": "present"},
                        unavailable=(design_cuda,),
                    ),
                )

            checks = {item.id: item for item in report.checks}
            self.assertEqual(checks["store.formal"].status, "pass")
            self.assertIn("read-only", checks["store.formal"].observation)
            self.assertEqual(database.read_bytes(), database_before)
            self.assertEqual(checks["credential.openai_api_key"].status, "pass")
            self.assertTrue(report.ready)
            blocked_checks = {item.id: item for item in blocked.checks}
            self.assertFalse(blocked.ready)
            self.assertEqual(blocked_checks["runtime.design.cuda"].status, "fail")
            colab.assert_called_with(
                config.colabdesign_dir,
                expected_commit=PredictionConfig().colabdesign_commit,
            )
            boltz.assert_called_with(
                config.boltz_executable,
                config.boltz_checkpoint,
                timeout=60,
            )
            pyrosetta.assert_called_with(config.pyrosetta_python)
            prodigy.assert_called_with(config.prodigy_executable, PRODIGY_VERSION)
            self.assertIn("availability verified", checks["runtime.design.rfdiffusion_repository"].observation)
            self.assertIn("availability verified", checks["runtime.design.ligandmpnn_repository"].observation)
            for check_id in (
                "runtime.python.design_refold",
                "runtime.design.rfdiffusion_environment",
                "runtime.design.rfdiffusion_site_packages",
                "runtime.design.rfdiffusion_entrypoint",
                "runtime.design.ligandmpnn_entrypoint",
                "runtime.design.se3_root",
                "runtime.design.cuda",
            ):
                with self.subTest(check_id=check_id):
                    self.assertEqual(checks[check_id].status, "pass")

    def test_execution_public_required_path_validator_is_pure(self):
        from execution.contracts import ExecutionContractError
        from execution.prediction_runtime import validate_required_prediction_tool_paths

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = execution_config(root)
            before = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
            result = validate_required_prediction_tool_paths(config)
            self.assertIn("boltz_checkpoint", result)
            self.assertEqual(before, sorted(str(path.relative_to(root)) for path in root.rglob("*")))
            config.boltz_checkpoint.unlink()
            with self.assertRaisesRegex(ExecutionContractError, "boltz_checkpoint"):
                validate_required_prediction_tool_paths(config)

    def test_independent_host_checks_survive_invalid_execution_environment(self):
        from workflow.doctor import run_doctor

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = approved_project(root)
            probes = FakeHostProbes(environment={})
            with patch(
                "workflow.doctor.ExecutionConfig.from_environment",
                side_effect=ValueError("invalid timeout"),
            ):
                report = run_doctor(project, probes=probes)

            checks = {item.id: item for item in report.checks}
            self.assertEqual(checks["runtime.execution_config"].status, "fail")
            self.assertEqual(checks["runtime.gpu"].status, "pass")
            self.assertEqual(checks["credential.openai_api_key"].status, "fail")
            self.assertIn("runtime.root.data", checks)
            self.assertIn("runtime.root.evidence", checks)
            self.assertIn("runtime.root.diagnostics", checks)


if __name__ == "__main__":
    unittest.main()
