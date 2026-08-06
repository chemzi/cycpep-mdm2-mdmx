"""PR7 protocol layer tests: shared loader, prediction binding, bundle checks."""

import json
import tempfile
import unittest
from pathlib import Path

from core.protocol import ProtocolError, load_protocol
from prediction_pipeline.adapters import load_artifact_bundle
from prediction_pipeline.contracts import ContractError
from prediction_pipeline.protocol import (
    PREDICTION_PROTOCOL,
    PREDICTION_PROTOCOL_SHA256,
    PREDICTOR_PROTOCOL,
    protocol_binding,
    reconcile_bundle_protocol,
)
from execution.contracts import ExecutionContractError, validate_task_parameters


class ProtocolLoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="protocol-loader-test-"))

    def _write(self, content: str) -> Path:
        path = self.tmp / "protocol.json"
        path.write_text(content, encoding="utf-8")
        return path

    def test_missing_file_raises(self):
        with self.assertRaises(ProtocolError):
            load_protocol(self.tmp / "nope.json")

    def test_invalid_json_raises(self):
        with self.assertRaises(ProtocolError):
            load_protocol(self._write("{not json"))

    def test_missing_version_raises(self):
        with self.assertRaises(ProtocolError):
            load_protocol(self._write("{}"))

    def test_missing_required_section_raises(self):
        path = self._write(json.dumps({"version": "v1"}))
        with self.assertRaises(ProtocolError):
            load_protocol(path, required_sections={"af2_prodigy": dict})

    def test_wrong_section_type_raises(self):
        path = self._write(json.dumps({"version": "v1", "af2_prodigy": "nope"}))
        with self.assertRaises(ProtocolError):
            load_protocol(path, required_sections={"af2_prodigy": dict})

    def test_repo_protocols_load_with_sections(self):
        data, sha = load_protocol(
            "protocols/design_v1.json",
            required_sections={"refold": dict, "rfdiff": dict},
        )
        self.assertEqual(data["version"], "design_v1")
        self.assertEqual(len(sha), 64)


class PredictionBindingTests(unittest.TestCase):
    def test_binding_matches_protocol(self):
        binding = protocol_binding()
        self.assertEqual(binding["name"], PREDICTOR_PROTOCOL)
        self.assertEqual(binding["version"], PREDICTION_PROTOCOL["version"])
        self.assertEqual(binding["sha256"], PREDICTION_PROTOCOL_SHA256)

    def test_reconcile_fills_missing(self):
        bundle: dict = {}
        reconcile_bundle_protocol(bundle)
        self.assertEqual(bundle["protocol"], protocol_binding())

    def test_reconcile_preserves_matching(self):
        bundle = {"protocol": protocol_binding()}
        reconcile_bundle_protocol(bundle)
        self.assertEqual(bundle["protocol"], protocol_binding())

    def test_reconcile_rejects_mismatch(self):
        bundle = {"protocol": {"name": "old", "version": "old", "sha256": "b" * 64}}
        with self.assertRaises(ProtocolError):
            reconcile_bundle_protocol(bundle)


class BundleProtocolValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bundle-protocol-test-"))

    def _bundle(self, extra=None) -> Path:
        data = {
            "schema_version": 1,
            "candidate_id": "C0001",
            "sequence": "ACDEFGHIK",
            "global": {},
            "targets": {},
        }
        data.update(extra or {})
        path = self.tmp / "artifacts.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_bundle_without_protocol_still_loads(self):
        result = load_artifact_bundle(
            self._bundle(), candidate_id="C0001", sequence="ACDEFGHIK",
            required_targets=(),
        )
        self.assertEqual(result.candidate_id, "C0001")

    def test_bundle_with_current_protocol_loads(self):
        result = load_artifact_bundle(
            self._bundle({"protocol": protocol_binding()}),
            candidate_id="C0001", sequence="ACDEFGHIK", required_targets=(),
        )
        self.assertEqual(result.candidate_id, "C0001")

    def test_bundle_with_stale_protocol_rejected(self):
        path = self._bundle(
            {"protocol": {"name": "old", "version": "old", "sha256": "b" * 64}}
        )
        with self.assertRaises(ContractError) as ctx:
            load_artifact_bundle(
                path, candidate_id="C0001", sequence="ACDEFGHIK",
                required_targets=(),
            )
        self.assertEqual(ctx.exception.code, "artifact_protocol_mismatch")


class TaskProtocolContractTests(unittest.TestCase):
    def _task(self, protocol: str) -> dict:
        return {
            "action": "evaluate_new_design_candidates",
            "parameters": {
                "reuse_complete_evidence": True,
                "evidence_mode": "reuse_or_generate_full",
                "predictor_protocol": protocol,
            },
            "resource_request": {
                "class": "gpu",
                "proposal_count": 1,
                "candidate_limit": 1,
            },
            "candidate_scope": {"candidate_ids": ["C0001"]},
            "outputs": ["prediction_handoff.json"],
        }

    def test_unknown_predictor_protocol_rejected(self):
        with self.assertRaises(ExecutionContractError) as ctx:
            validate_task_parameters(self._task("bogus_protocol"))
        self.assertEqual(ctx.exception.code, "prediction_protocol_invalid")

    def test_registered_predictor_protocol_accepted(self):
        normalized = validate_task_parameters(self._task(PREDICTOR_PROTOCOL))
        self.assertEqual(normalized["predictor_protocol"], PREDICTOR_PROTOCOL)


if __name__ == "__main__":
    unittest.main()
