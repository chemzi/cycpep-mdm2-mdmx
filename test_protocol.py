"""PR7 protocol layer tests: shared loader, envelope schema, identity SHA, contracts."""

import json
import tempfile
import unittest
from pathlib import Path

from core.protocol import (
    ProtocolError,
    canonical_parameters_sha256,
    load_protocol,
    protocol_identity_sha256,
)
from prediction_pipeline.adapters import load_artifact_bundle
from prediction_pipeline.contracts import ContractError, SCHEMA_VERSION
from prediction_pipeline.execution_identity import build_prediction_execution_identity
from prediction_pipeline.protocol import (
    ACTIVE_PREDICTOR_PROTOCOL,
    PREDICTION_PROTOCOL,
    PREDICTION_PROTOCOL_PARAMETERS_SHA256,
    PREDICTION_PROTOCOL_SHA256,
    PREDICTOR_PROTOCOL,
    PROTOCOL_REGISTRY,
    PredictionProtocol,
    protocol_binding,
    validate_bundle_protocol,
    validate_execution_compatibility,
)
from execution.contracts import ExecutionContractError, validate_task_parameters


def _valid_envelope(**overrides) -> dict:
    """A minimal envelope that passes the shared loader schema."""
    data = {
        "name": "test",
        "version": "1.0",
        "parameters": {"af2_prodigy": {"seeds": [0]}},
        "metadata": {"description": "hello"},
    }
    data.update(overrides)
    return data


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

    def test_missing_name_raises(self):
        with self.assertRaises(ProtocolError):
            load_protocol(self._write(json.dumps({"version": "1.0"})))

    def test_missing_version_raises(self):
        with self.assertRaises(ProtocolError):
            load_protocol(self._write(json.dumps({"name": "test"})))

    def test_bad_version_format_raises(self):
        path = self._write(json.dumps(_valid_envelope(version="hello")))
        with self.assertRaises(ProtocolError):
            load_protocol(path)

    def test_version_format_accepts_major_minor(self):
        data, sha = load_protocol(
            self._write(json.dumps(_valid_envelope(version="2.3")))
        )
        self.assertEqual(data["version"], "2.3")
        self.assertEqual(len(sha), 64)

    def test_missing_parameters_raises(self):
        path = self._write(json.dumps({"name": "test", "version": "1.0"}))
        with self.assertRaises(ProtocolError):
            load_protocol(path)

    def test_unknown_top_level_key_rejected(self):
        path = self._write(
            json.dumps(_valid_envelope(protocol_name="smuggled"))
        )
        with self.assertRaises(ProtocolError):
            load_protocol(path)

    def test_missing_required_section_raises(self):
        path = self._write(json.dumps(_valid_envelope(parameters={})))
        with self.assertRaises(ProtocolError):
            load_protocol(path, required_sections={"af2_prodigy": dict})

    def test_wrong_section_type_raises(self):
        path = self._write(
            json.dumps(_valid_envelope(parameters={"af2_prodigy": "nope"}))
        )
        with self.assertRaises(ProtocolError):
            load_protocol(path, required_sections={"af2_prodigy": dict})

    def test_repo_protocols_load_with_sections(self):
        data, sha = load_protocol(
            "protocols/design_v1.json",
            required_sections={"refold": dict, "rfdiff": dict},
        )
        self.assertEqual(data["name"], "design")
        self.assertEqual(data["version"], "1.0")
        self.assertEqual(len(sha), 64)

    def test_metadata_change_does_not_change_hash(self):
        # P0 canonicalization: metadata edits must NOT invalidate evidence.
        base = _valid_envelope()
        edited = _valid_envelope(
            metadata={"description": "better explanation", "author": "lead"}
        )
        sha_base = load_protocol(
            self._write(json.dumps(base))
        )[1]
        sha_edited = load_protocol(
            self._write(json.dumps(edited))
        )[1]
        self.assertEqual(sha_base, sha_edited)

    def test_parameter_change_changes_hash(self):
        base = _valid_envelope()
        edited = _valid_envelope(
            parameters={"af2_prodigy": {"seeds": [0, 1, 2]}}
        )
        sha_base = load_protocol(
            self._write(json.dumps(base))
        )[1]
        sha_edited = load_protocol(
            self._write(json.dumps(edited))
        )[1]
        self.assertNotEqual(sha_base, sha_edited)

    def test_hash_is_key_order_independent(self):
        params_a = {"a": [1, 2], "b": {"c": 3}}
        params_b = {"b": {"c": 3}, "a": [1, 2]}
        self.assertEqual(
            canonical_parameters_sha256(params_a),
            canonical_parameters_sha256(params_b),
        )

    def test_identity_hash_binds_name_and_version(self):
        # Two protocols with identical parameters but different name/version
        # must produce different identity digests.
        base = _valid_envelope()
        renamed = _valid_envelope(name="prediction2")
        reversioned = _valid_envelope(version="2.0")
        sha_base = load_protocol(self._write(json.dumps(base)))[1]
        sha_renamed = load_protocol(self._write(json.dumps(renamed)))[1]
        sha_reversioned = load_protocol(self._write(json.dumps(reversioned)))[1]
        self.assertNotEqual(sha_base, sha_renamed)
        self.assertNotEqual(sha_base, sha_reversioned)

    def test_identity_hash_differs_from_parameters_hash(self):
        data, identity_sha = load_protocol(
            self._write(json.dumps(_valid_envelope()))
        )
        params_sha = canonical_parameters_sha256(data["parameters"])
        self.assertEqual(len(identity_sha), 64)
        self.assertEqual(len(params_sha), 64)
        self.assertNotEqual(identity_sha, params_sha)

    def test_identity_hash_function_matches_loader(self):
        data, identity_sha = load_protocol(
            self._write(json.dumps(_valid_envelope()))
        )
        self.assertEqual(
            identity_sha,
            protocol_identity_sha256(
                data["name"], data["version"], data["parameters"]
            ),
        )

    def test_file_declared_required_sections_enforced(self):
        data = _valid_envelope()
        data["metadata"]["required_sections"] = ["af2_prodigy", "ligandmpnn"]
        with self.assertRaises(ProtocolError):
            load_protocol(self._write(json.dumps(data)))

    def test_file_declared_required_sections_bad_type_rejected(self):
        data = _valid_envelope()
        data["metadata"]["required_sections"] = "af2_prodigy"
        with self.assertRaises(ProtocolError):
            load_protocol(self._write(json.dumps(data)))

    def test_file_declared_required_sections_pass(self):
        data = _valid_envelope()
        data["metadata"]["required_sections"] = ["af2_prodigy"]
        data["parameters"]["ligandmpnn"] = {"n_seq_per_backbone": 8}
        _, sha = load_protocol(self._write(json.dumps(data)))
        self.assertEqual(len(sha), 64)


class PredictionBindingTests(unittest.TestCase):
    def test_binding_matches_protocol(self):
        binding = protocol_binding()
        self.assertEqual(binding["name"], PREDICTOR_PROTOCOL["name"])
        self.assertEqual(binding["name"], ACTIVE_PREDICTOR_PROTOCOL)
        self.assertEqual(binding["version"], PREDICTION_PROTOCOL["version"])
        self.assertEqual(binding["sha256"], PREDICTION_PROTOCOL_SHA256)
        self.assertEqual(binding, PREDICTOR_PROTOCOL)

    def test_identity_and_parameters_hashes_separated(self):
        from prediction_pipeline.protocol import (
            PREDICTION_PROTOCOL_IDENTITY_SHA256,
            PREDICTION_PROTOCOL_PARAMETERS_SHA256,
        )
        self.assertNotEqual(
            PREDICTION_PROTOCOL_IDENTITY_SHA256,
            PREDICTION_PROTOCOL_PARAMETERS_SHA256,
        )
        self.assertEqual(
            PREDICTION_PROTOCOL_SHA256, PREDICTION_PROTOCOL_IDENTITY_SHA256
        )

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
            "schema_version": SCHEMA_VERSION,
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
    """The Action Contract carries a protocol identity object, not a string."""

    def _task(self, protocol) -> dict:
        return {
            "action": "evaluate_new_design_candidates",
            "parameters": {
                "reuse_complete_evidence": True,
                "evidence_mode": "reuse_or_generate_full",
                "predictor_protocol": protocol,
                "execution_identity": build_prediction_execution_identity(),
            },
            "resource_request": {
                "class": "gpu",
                "proposal_count": 1,
                "candidate_limit": 1,
            },
            "candidate_scope": {"candidate_ids": ["C0001"]},
            "outputs": ["prediction_handoff.json"],
        }

    def test_string_protocol_rejected(self):
        # 必修2: a bare protocol name no longer pins exact parameters.
        with self.assertRaises(ExecutionContractError) as ctx:
            validate_task_parameters(self._task("af2_boltz2_prodigy_rosetta_postrelax_v1"))
        self.assertEqual(ctx.exception.code, "prediction_protocol_invalid")

    def test_unknown_predictor_protocol_rejected(self):
        identity = {"name": "bogus", "version": "1.0", "sha256": "b" * 64}
        with self.assertRaises(ExecutionContractError) as ctx:
            validate_task_parameters(self._task(identity))
        self.assertEqual(ctx.exception.code, "prediction_protocol_invalid")

    def test_mismatched_sha_rejected(self):
        # Same name but different sha: not the protocol execution will run.
        identity = {"name": ACTIVE_PREDICTOR_PROTOCOL, "version": "1.0", "sha256": "b" * 64}
        with self.assertRaises(ExecutionContractError) as ctx:
            validate_task_parameters(self._task(identity))
        self.assertEqual(ctx.exception.code, "prediction_protocol_invalid")

    def test_non_hex_sha_rejected(self):
        identity = {"name": ACTIVE_PREDICTOR_PROTOCOL, "version": "1.0", "sha256": "not-hex"}
        with self.assertRaises(ExecutionContractError) as ctx:
            validate_task_parameters(self._task(identity))
        self.assertEqual(ctx.exception.code, "prediction_protocol_invalid")

    def test_registered_predictor_protocol_accepted(self):
        normalized = validate_task_parameters(self._task(PREDICTOR_PROTOCOL))
        self.assertEqual(normalized["predictor_protocol"], PREDICTOR_PROTOCOL)

    def test_historical_task_without_execution_identity_fails_before_execution(self):
        task = self._task(PREDICTOR_PROTOCOL)
        task["parameters"].pop("execution_identity")
        with self.assertRaises(ExecutionContractError) as ctx:
            validate_task_parameters(task)
        self.assertEqual(ctx.exception.code, "execution_parameters_invalid")

    def test_task_identity_must_match_active_protocol(self):
        # A registered-but-older identity must be refused: execution can only
        # run the active protocol.
        stale = dict(PREDICTOR_PROTOCOL)
        stale["version"] = "0.9"
        stale["sha256"] = "c" * 64
        with self.assertRaises(ExecutionContractError) as ctx:
            validate_task_parameters(self._task(stale))
        self.assertEqual(ctx.exception.code, "prediction_protocol_invalid")


class ProtocolSchemaTests(unittest.TestCase):
    """PredictionProtocol.from_data enforces the full scientific schema."""

    def _data(self, **overrides) -> dict:
        data = {
            "name": "prediction",
            "version": "1.0",
            "parameters": {
                "af2_prodigy": {
                    "seeds": [0, 1, 2],
                    "model_numbers": [0, 1, 2],
                    "num_recycles": 3,
                },
                "enrichment": {
                    "seed_base": 101,
                    "post_relax_seed_base": 20260802,
                    "post_relax_repeats": 3,
                    "post_relax_coordinate_stdev": 0.5,
                },
                "boltz": {
                    "diffusion_samples": 1,
                },
                "rosetta_interface": {
                    "maximum_terminal_c_to_n_distance_angstrom": 2.0,
                },
            },
            "metadata": {"description": "x"},
        }
        parameters = data["parameters"]
        for key, value in overrides.items():
            if key in {"name", "version", "metadata"}:
                data[key] = value
            else:
                parameters[key] = value
        return data

    def test_valid_protocol_loads(self):
        protocol = PredictionProtocol.from_data(self._data())
        self.assertEqual(protocol.name, PREDICTOR_PROTOCOL["name"])
        self.assertEqual(protocol.af2_seeds, (0, 1, 2))
        self.assertEqual(protocol.num_recycles, 3)

    def test_duplicate_seeds_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(af2_prodigy={**self._data()["parameters"]["af2_prodigy"], "seeds": [0, 0, 1]})
            )

    def test_duplicate_models_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(af2_prodigy={**self._data()["parameters"]["af2_prodigy"], "model_numbers": [0, 0, 1]})
            )

    def test_model_out_of_range_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(af2_prodigy={**self._data()["parameters"]["af2_prodigy"], "model_numbers": [0, 1, 5]})
            )

    def test_seed_model_length_mismatch_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(af2_prodigy={**self._data()["parameters"]["af2_prodigy"], "seeds": [0, 1]})
            )

    def test_nested_section_missing_model_numbers_rejected(self):
        af2 = {**self._data()["parameters"]["af2_prodigy"]}
        del af2["model_numbers"]
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(self._data(af2_prodigy=af2))

    def test_nested_section_missing_num_recycles_rejected(self):
        af2 = {**self._data()["parameters"]["af2_prodigy"]}
        del af2["num_recycles"]
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(self._data(af2_prodigy=af2))

    def test_unknown_af2_key_rejected(self):
        # 必修3: a typo like "recycels" must fail at import time.
        af2 = {**self._data()["parameters"]["af2_prodigy"], "recycels": 3}
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(self._data(af2_prodigy=af2))

    def test_unknown_enrichment_key_rejected(self):
        enrichment = {
            **self._data()["parameters"]["enrichment"],
            "seed_baze": 101,
        }
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(self._data(enrichment=enrichment))

    def test_unknown_boltz_key_rejected(self):
        boltz = {**self._data()["parameters"]["boltz"], "diffusion_sampls": 1}
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(self._data(boltz=boltz))

    def test_non_sequential_model_numbers_accepted(self):
        # The protocol pins the exact model set; order is data, not a
        # 0..n-1 assumption.  A permuted set must load and stay authoritative.
        protocol = PredictionProtocol.from_data(
            self._data(af2_prodigy={**self._data()["parameters"]["af2_prodigy"], "model_numbers": [2, 0, 1]})
        )
        self.assertEqual(protocol.af2_model_numbers, (2, 0, 1))

    def test_non_positive_recycles_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(af2_prodigy={**self._data()["parameters"]["af2_prodigy"], "num_recycles": 0})
            )

    def test_empty_seeds_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(af2_prodigy={**self._data()["parameters"]["af2_prodigy"], "seeds": []})
            )

    def test_non_integer_enrichment_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(enrichment={**self._data()["parameters"]["enrichment"], "post_relax_repeats": "3"})
            )

    def test_non_positive_repeats_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(enrichment={**self._data()["parameters"]["enrichment"], "post_relax_repeats": 0})
            )

    def test_negative_seed_base_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(enrichment={**self._data()["parameters"]["enrichment"], "seed_base": -1})
            )

    def test_negative_post_relax_seed_base_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(
                    enrichment={
                        **self._data()["parameters"]["enrichment"],
                        "post_relax_seed_base": -20260802,
                    }
                )
            )

    def test_missing_boltz_section_rejected(self):
        data = self._data()
        del data["parameters"]["boltz"]
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(data)

    def test_non_positive_diffusion_samples_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(boltz={"diffusion_samples": 0})
            )

    def test_non_positive_coordinate_stdev_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(
                    enrichment={
                        **self._data()["parameters"]["enrichment"],
                        "post_relax_coordinate_stdev": 0.0,
                    }
                )
            )

    def test_typed_values_load(self):
        protocol = PredictionProtocol.from_data(self._data())
        self.assertEqual(protocol.boltz_diffusion_samples, 1)
        self.assertEqual(protocol.post_relax_coordinate_stdev, 0.5)
        self.assertEqual(
            protocol.rosetta_maximum_terminal_c_to_n_distance_angstrom, 2.0
        )

    def test_non_positive_rosetta_terminal_distance_rejected(self):
        with self.assertRaises(ProtocolError):
            PredictionProtocol.from_data(
                self._data(rosetta_interface={
                    "maximum_terminal_c_to_n_distance_angstrom": 0.0,
                })
            )

    def test_multi_protocol_coexistence(self):
        # Two parameter sets produce two distinct identities; a registry keyed
        # by name can hold both.
        v1 = PredictionProtocol.from_data(self._data())
        v2 = PredictionProtocol.from_data(
            self._data(
                version="2.0",
                af2_prodigy={**self._data()["parameters"]["af2_prodigy"], "seeds": [4, 5, 6]},
            )
        )
        self.assertEqual(v1.name, v2.name)
        self.assertNotEqual(v1.version, v2.version)
        self.assertNotEqual(v1.af2_seeds, v2.af2_seeds)
        # Identities differ when parameters differ:
        self.assertNotEqual(
            canonical_parameters_sha256(
                self._data()["parameters"]
            ),
            canonical_parameters_sha256(
                self._data(
                    af2_prodigy={**self._data()["parameters"]["af2_prodigy"], "seeds": [4, 5, 6]}
                )["parameters"]
            ),
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
        self.assertEqual(
            typed.af2_seeds,
            tuple(PREDICTION_PROTOCOL["parameters"]["af2_prodigy"]["seeds"]),
        )
        self.assertEqual(
            typed.af2_model_numbers,
            tuple(PREDICTION_PROTOCOL["parameters"]["af2_prodigy"]["model_numbers"]),
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

    def test_tampered_parameter_invalidates_reuse(self):
        # P2 CI gap: changing a scientific parameter (seed) changes the
        # identity SHA, so a bundle bound to the tampered protocol must be
        # refused on the execution path.
        tampered = dict(PREDICTION_PROTOCOL)
        tampered["parameters"] = {
            "af2_prodigy": {
                "seeds": [9, 9, 9],
                "model_numbers": [0, 1, 2],
                "num_recycles": 3,
            },
            "enrichment": PREDICTION_PROTOCOL["parameters"]["enrichment"],
            "boltz": PREDICTION_PROTOCOL["parameters"]["boltz"],
        }
        tampered_binding = {
            "name": ACTIVE_PREDICTOR_PROTOCOL,
            "version": PREDICTION_PROTOCOL["version"],
            "sha256": canonical_parameters_sha256(tampered["parameters"]),
        }
        self.assertNotEqual(tampered_binding["sha256"], PREDICTION_PROTOCOL_SHA256)
        with self.assertRaises(ProtocolError):
            validate_execution_compatibility({"protocol": tampered_binding})

    def test_metadata_edit_keeps_reuse_valid(self):
        # P0: a description edit must NOT invalidate recorded evidence.
        edited = dict(PREDICTION_PROTOCOL)
        edited["metadata"] = {
            "description": "better explanation",
            "author": "team lead",
        }
        # A metadata edit must not change the parameters SHA nor the
        # identity SHA: recorded evidence stays valid.
        self.assertEqual(
            canonical_parameters_sha256(edited["parameters"]),
            PREDICTION_PROTOCOL_PARAMETERS_SHA256,
        )
        self.assertEqual(
            PREDICTION_PROTOCOL_PARAMETERS_SHA256,
            canonical_parameters_sha256(PREDICTION_PROTOCOL["parameters"]),
        )
        self.assertEqual(PREDICTION_PROTOCOL_SHA256, protocol_binding()["sha256"])
        validate_execution_compatibility({"protocol": protocol_binding()})


if __name__ == "__main__":
    unittest.main()
