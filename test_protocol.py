"""PR7 protocol layer tests: shared loader, prediction binding, bundle checks."""

import json
import tempfile
import unittest
from pathlib import Path

from core.protocol import ProtocolError, load_protocol
from prediction_pipeline.adapters import load_artifact_bundle
from prediction_pipeline.contracts import ContractError
from prediction_pipeline.protocol import (
    ACTIVE_PREDICTOR_PROTOCOL,
    PREDICTION_PROTOCOL,
    PREDICTION_PROTOCOL_SHA256,
    PREDICTOR_PROTOCOL,
    PROTOCOL_REGISTRY,
    PredictionProtocol,
    protocol_binding,
    validate_bundle_protocol,
    validate_execution_compatibility,
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

    def test_validate_rejects_missing_protocol(self):
        bundle: dict = {}
        with self.assertRaises(ProtocolError):
            validate_bundle_protocol(bundle)

    def test_validate_preserves_matching(self):
        bundle = {"protocol": protocol_binding()}
        validate_bundle_protocol(bundle)
        self.assertEqual(bundle["protocol"], protocol_binding())

    def test_validate_rejects_mismatch(self):
        bundle = {"protocol": {"name": "old", "version": "old", "sha256": "b" * 64}}
        with self.assertRaises(ProtocolError):
            validate_bundle_protocol(bundle)


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

    def test_bundle_with_stale_protocol_still_readable(self):
        # A well-formed protocol binding for an older protocol is valid
        # history: reading it must not require it to equal the active one.
        result = load_artifact_bundle(
            self._bundle(
                {"protocol": {"name": "old", "version": "old", "sha256": "b" * 64}}
            ),
            candidate_id="C0001", sequence="ACDEFGHIK", required_targets=(),
        )
        self.assertEqual(result.candidate_id, "C0001")

    def test_bundle_with_incomplete_protocol_rejected(self):
        path = self._bundle({"protocol": {"name": "only-name"}})
        with self.assertRaises(ContractError) as ctx:
            load_artifact_bundle(
                path, candidate_id="C0001", sequence="ACDEFGHIK",
                required_targets=(),
            )
        self.assertEqual(ctx.exception.code, "artifact_protocol_incomplete")

    def test_bundle_with_non_string_protocol_fields_rejected(self):
        path = self._bundle(
            {"protocol": {"name": "old", "version": 1, "sha256": "b" * 64}}
        )
        with self.assertRaises(ContractError) as ctx:
            load_artifact_bundle(
                path, candidate_id="C0001", sequence="ACDEFGHIK",
                required_targets=(),
            )
        self.assertEqual(ctx.exception.code, "artifact_protocol_type")


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


class ProtocolSchemaTests(unittest.TestCase):
    """PredictionProtocol.from_data enforces the full scientific schema."""

    def _data(self, **overrides) -> dict:
        data = {
            "version": "prediction_v1",
            "protocol_name": "af2_boltz2_prodigy_rosetta_postrelax_v1",
            "af2_prodigy": {
                "seeds": [0, 1, 2],
                "model_numbers": [0, 1, 2],
                "num_recycles": 3,
            },
            "enrichment": {
                "seed_base": 101,
                "post_relax_seed_base": 20260802,
                "post_relax_repeats": 3,
            },
        }
        data.update(overrides)
        return data

    def test_valid_protocol_loads(self):
        protocol = PredictionProtocol.from_data(self._data())
        self.assertEqual(protocol.protocol_name, PREDICTOR_PROTOCOL)
        self.assertEqual(protocol.af2_seeds, (0, 1, 2))
        self.assertEqual(protocol.num_recycles, 3)

    def test_duplicate_seeds_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(af2_prodigy={**self._data()["af2_prodigy"], "seeds": [0, 0, 1]})
            )

    def test_duplicate_models_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(af2_prodigy={**self._data()["af2_prodigy"], "model_numbers": [0, 0, 1]})
            )

    def test_model_out_of_range_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(af2_prodigy={**self._data()["af2_prodigy"], "model_numbers": [0, 1, 5]})
            )

    def test_seed_model_length_mismatch_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(af2_prodigy={**self._data()["af2_prodigy"], "seeds": [0, 1]})
            )

    def test_nested_section_missing_model_numbers_rejected(self):
        # Missing nested key inside af2_prodigy must fail at import time.
        af2 = {**self._data()["af2_prodigy"]}
        del af2["model_numbers"]
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(self._data(af2_prodigy=af2))

    def test_nested_section_missing_num_recycles_rejected(self):
        af2 = {**self._data()["af2_prodigy"]}
        del af2["num_recycles"]
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(self._data(af2_prodigy=af2))

    def test_non_sequential_model_numbers_accepted(self):
        # The protocol pins the exact model set; order is data, not a
        # 0..n-1 assumption.  A permuted set must load and stay authoritative.
        protocol = PredictionProtocol.from_data(
            self._data(af2_prodigy={**self._data()["af2_prodigy"], "model_numbers": [2, 0, 1]})
        )
        self.assertEqual(protocol.af2_model_numbers, (2, 0, 1))

    def test_non_positive_recycles_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(af2_prodigy={**self._data()["af2_prodigy"], "num_recycles": 0})
            )

    def test_empty_seeds_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(af2_prodigy={**self._data()["af2_prodigy"], "seeds": []})
            )

    def test_non_integer_enrichment_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(enrichment={**self._data()["enrichment"], "post_relax_repeats": "3"})
            )

    def test_non_positive_repeats_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(enrichment={**self._data()["enrichment"], "post_relax_repeats": 0})
            )

    def test_negative_seed_base_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(enrichment={**self._data()["enrichment"], "seed_base": -1})
            )

    def test_negative_post_relax_seed_base_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(
                    enrichment={
                        **self._data()["enrichment"],
                        "post_relax_seed_base": -20260802,
                    }
                )
            )


class ProtocolRegistryTests(unittest.TestCase):
    def test_registry_contains_active_protocol(self):
        self.assertIn(ACTIVE_PREDICTOR_PROTOCOL, PROTOCOL_REGISTRY)
        self.assertIsInstance(
            PROTOCOL_REGISTRY[ACTIVE_PREDICTOR_PROTOCOL], PredictionProtocol
        )

    def test_predictor_protocols_derived_from_registry(self):
        from prediction_pipeline.protocol import PREDICTOR_PROTOCOLS
        self.assertEqual(PREDICTOR_PROTOCOLS, frozenset(PROTOCOL_REGISTRY))

    def test_registry_matches_loaded_raw_protocol(self):
        typed = PROTOCOL_REGISTRY[ACTIVE_PREDICTOR_PROTOCOL]
        self.assertEqual(typed.af2_seeds, tuple(PREDICTION_PROTOCOL["af2_prodigy"]["seeds"]))
        self.assertEqual(
            typed.af2_model_numbers,
            tuple(PREDICTION_PROTOCOL["af2_prodigy"]["model_numbers"]),
        )


class ExecutionCompatibilityTests(unittest.TestCase):
    def test_matching_binding_executable(self):
        validate_execution_compatibility({"protocol": protocol_binding()})

    def test_legacy_bundle_not_executable(self):
        with self.assertRaises(ProtocolError):
            validate_execution_compatibility({})

    def test_stale_binding_not_executable(self):
        with self.assertRaises(ProtocolError):
            validate_execution_compatibility(
                {"protocol": {"name": "old", "version": "old", "sha256": "b" * 64}}
            )

if __name__ == "__main__":
    unittest.main()

