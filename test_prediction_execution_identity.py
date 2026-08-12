"""Contract tests for path-independent Prediction execution identity."""

from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from prediction_pipeline.execution_identity import (
    build_prediction_execution_identity,
    validate_prediction_execution_identity,
)
from prediction_pipeline.contracts import PredictionConfig
from prediction_pipeline.rosetta_worker import (
    PYROSETTA_VERSION,
    validate_pyrosetta_runtime,
)
from prediction_pipeline.adapters import validate_prodigy_runtime
from prediction_pipeline.execution_identity import PRODIGY_VERSION
from execution.config import ExecutionConfig


class PredictionExecutionIdentityTests(unittest.TestCase):
    def test_identity_is_path_independent(self):
        left = build_prediction_execution_identity()
        right = build_prediction_execution_identity()

        self.assertEqual(left, right)
        rendered = str(left)
        self.assertNotIn("/root/", rendered)
        self.assertNotIn("C:\\", rendered)
        self.assertNotIn("executable", rendered)
        self.assertNotIn("cache", rendered)

    def test_each_scientific_binding_changes_identity(self):
        identity = build_prediction_execution_identity()
        mutations = (
            ("prediction_protocol", "version", "different"),
            ("colabdesign", "commit", "0" * 40),
            ("af2", "model_family", "different"),
            ("boltz", "version", "different"),
            ("boltz", "checkpoint_sha256", "0" * 64),
            ("pyrosetta", "version", "different"),
            ("prodigy", "version", "different"),
        )
        for section, key, value in mutations:
            with self.subTest(section=section, key=key):
                changed = copy.deepcopy(identity)
                changed[section][key] = value
                with self.assertRaisesRegex(ValueError, "configuration digest"):
                    validate_prediction_execution_identity(changed)

    def test_unknown_or_mismatched_identity_fails_closed(self):
        identity = build_prediction_execution_identity()
        identity["runtime_path"] = "/root/ambient"
        with self.assertRaisesRegex(ValueError, "unsupported"):
            validate_prediction_execution_identity(identity)

        observed = build_prediction_execution_identity()
        observed["configuration_digest"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "configuration digest"):
            validate_prediction_execution_identity(observed)

        runtime_observed = build_prediction_execution_identity(
            observations={"prodigy_version": "2.3"}
        )
        with self.assertRaisesRegex(ValueError, "differs from the approved"):
            validate_prediction_execution_identity(
                runtime_observed, expected=build_prediction_execution_identity()
            )

    def test_every_prediction_config_scientific_field_changes_digest(self):
        baseline = PredictionConfig()
        mutations = {
            "ipsae_pae_cutoff": 11.0,
            "interface_distance_angstrom": 5.0,
            "seed_cluster_rmsd_angstrom": 2.5,
            "minimum_predictions_per_target": 4,
            "minimum_predictors_per_target": 3,
            "colabdesign_commit": "0" * 40,
        }
        original = build_prediction_execution_identity(baseline)
        for field, value in mutations.items():
            with self.subTest(field=field):
                values = baseline.to_dict()
                values[field] = value
                changed = build_prediction_execution_identity(
                    PredictionConfig.from_dict(values)
                )
                self.assertNotEqual(
                    changed["configuration_digest"],
                    original["configuration_digest"],
                )

    def test_pyrosetta_preflight_keeps_venv_symlink_entrypoint_and_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base-python"
            base.write_text("", encoding="utf-8")
            venv = root / "venv" / "bin"
            venv.mkdir(parents=True)
            entrypoint = venv / "python"
            try:
                entrypoint.symlink_to(base)
            except OSError:
                self.skipTest("symlink creation is unavailable")
            observed = SimpleNamespace(
                exit_code=0, stdout=PYROSETTA_VERSION + "\n", stderr=""
            )
            with patch(
                "prediction_pipeline.rosetta_worker.run_command",
                return_value=observed,
            ) as run:
                self.assertEqual(
                    validate_pyrosetta_runtime(entrypoint), PYROSETTA_VERSION
                )
            argv = run.call_args.args[0]
            self.assertEqual(Path(argv[0]), entrypoint.absolute())
            self.assertIn("import pyrosetta", argv[2])

            with patch.dict(os.environ, {
                "CYCPEP_PREDICTION_PYTHON": str(entrypoint),
                "CYCPEP_PYROSETTA_PYTHON": str(entrypoint),
            }, clear=False):
                config = ExecutionConfig.from_environment()
            self.assertEqual(config.prediction_python, entrypoint.absolute())
            self.assertEqual(config.pyrosetta_python, entrypoint.absolute())

    def test_prodigy_installed_version_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "prodigy"
            python = root / "python"
            executable.write_text("", encoding="utf-8")
            python.write_text("", encoding="utf-8")
            observed = SimpleNamespace(exit_code=0, stdout="2.3\n", stderr="")
            with patch(
                "prediction_pipeline.adapters.run_command", return_value=observed
            ), self.assertRaisesRegex(ValueError, "2.4.0 is required"):
                validate_prodigy_runtime(executable, PRODIGY_VERSION)

    def test_prodigy_installed_package_version_exactly_matches_canonical_identity(self):
        self.assertEqual(PRODIGY_VERSION, "2.4.0")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "prodigy"
            python = root / "python"
            executable.write_text("", encoding="utf-8")
            python.write_text("", encoding="utf-8")
            observed = SimpleNamespace(exit_code=0, stdout="2.4.0\n", stderr="")
            with patch(
                "prediction_pipeline.adapters.run_command", return_value=observed
            ):
                self.assertEqual(
                    validate_prodigy_runtime(executable, PRODIGY_VERSION), "2.4.0"
                )

    def test_inaccessible_prediction_python_fallback_uses_current_interpreter(self):
        hardcoded = "/root/damodel-tmp/envs/cycpep-prediction/bin/python"
        original = Path.is_file

        def inaccessible(path):
            if str(path) == hardcoded:
                raise PermissionError("inaccessible deployment fallback")
            return original(path)

        with patch.dict(os.environ, {}, clear=True), patch.object(
            Path, "is_file", inaccessible
        ):
            config = ExecutionConfig.from_environment()
        self.assertEqual(config.prediction_python, Path(sys.executable).absolute())


if __name__ == "__main__":
    unittest.main()
