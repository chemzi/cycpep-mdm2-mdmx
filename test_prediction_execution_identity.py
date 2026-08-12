"""Contract tests for path-independent Prediction execution identity."""

from __future__ import annotations

import copy
import unittest

from prediction_pipeline.execution_identity import (
    build_prediction_execution_identity,
    validate_prediction_execution_identity,
)


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


if __name__ == "__main__":
    unittest.main()
