"""PR40 P0 regression tests: resume protocol binding and legacy migration."""

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from prediction_pipeline.contracts import ContractError
from prediction_pipeline.protocol import PREDICTION_PROTOCOL, protocol_binding
from scripts.enrich_prediction_evidence import preflight_bundle_protocol
from scripts.migrate_legacy_prediction_protocol import (
    _prediction_output_dirs,
    _resolve_targets,
    migrate_bundle,
)
from scripts.run_prediction_predictors import (
    PROTOCOL_BINDING_FILENAME,
    _read_protocol_binding,
    _require_existing_bundle_protocol,
    _require_protocol_parameters,
    _run_one,
)


class _FakeResult:
    exit_code = 0
    stdout = "out"
    stderr = ""


class ResumeProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="resume-protocol-test-"))

    def _output_dir(self, *, with_binding: bool | None = None) -> Path:
        output_dir = self.tmp / "model_0_seed_0"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "metadata.json").write_text(
            json.dumps({"model_id": "model_0"}), encoding="utf-8"
        )
        if with_binding is True:
            (output_dir / PROTOCOL_BINDING_FILENAME).write_text(
                json.dumps(protocol_binding()), encoding="utf-8"
            )
        elif with_binding is False:
            (output_dir / PROTOCOL_BINDING_FILENAME).write_text(
                json.dumps({"name": "old", "version": "old", "sha256": "b" * 64}),
                encoding="utf-8",
            )
        return output_dir

    def _resume_args(self):
        return types.SimpleNamespace(resume=True, timeout=30, cuda_data_dir="")

    def test_resume_without_binding_refused(self):
        output_dir = self._output_dir()
        with self.assertRaises(ContractError) as ctx:
            _run_one(["echo"], output_dir, self._resume_args())
        self.assertEqual(ctx.exception.code, "resume_protocol_unrecorded")

    def test_resume_with_corrupt_metadata_refused(self):
        output_dir = self._output_dir(with_binding=True)
        (output_dir / "metadata.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ContractError) as ctx:
            _run_one(["echo"], output_dir, self._resume_args())
        self.assertEqual(ctx.exception.code, "predictor_metadata_corrupt")

    def test_resume_with_corrupt_binding_refused(self):
        output_dir = self._output_dir()
        (output_dir / PROTOCOL_BINDING_FILENAME).write_text(
            "{not json", encoding="utf-8"
        )
        with self.assertRaises(ContractError) as ctx:
            _run_one(["echo"], output_dir, self._resume_args())
        self.assertEqual(ctx.exception.code, "resume_protocol_corrupt")

    def test_resume_with_stale_binding_refused(self):
        output_dir = self._output_dir(with_binding=False)
        with self.assertRaises(ContractError) as ctx:
            _run_one(["echo"], output_dir, self._resume_args())
        self.assertEqual(ctx.exception.code, "resume_protocol_mismatch")

    def test_resume_with_matching_binding_reuses(self):
        output_dir = self._output_dir(with_binding=True)
        result = _run_one(["echo"], output_dir, self._resume_args())
        self.assertEqual(result["model_id"], "model_0")

    def test_read_binding_missing_returns_none(self):
        output_dir = self.tmp / "empty"
        output_dir.mkdir(parents=True, exist_ok=True)
        self.assertIsNone(_read_protocol_binding(output_dir))

    def test_fresh_run_records_binding(self):
        output_dir = self.tmp / "fresh"
        args = types.SimpleNamespace(resume=False, timeout=30, cuda_data_dir="")

        def _fake_run(command, timeout, cwd, env):
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "metadata.json").write_text(
                json.dumps({"model_id": "model_0"}), encoding="utf-8"
            )
            return _FakeResult()

        with patch(
            "scripts.run_prediction_predictors.run_command", side_effect=_fake_run
        ):
            _run_one(["echo"], output_dir, args)
        recorded = json.loads(
            (output_dir / PROTOCOL_BINDING_FILENAME).read_text(encoding="utf-8")
        )
        self.assertEqual(recorded, protocol_binding())


class ExistingBundleProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bundle-resume-test-"))

    def _bundle_path(self, protocol) -> Path:
        data = {"schema_version": 1, "candidate_id": "C0001"}
        if protocol is not None:
            data["protocol"] = protocol
        path = self.tmp / "artifacts.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_matching_bundle_allows_rewrite(self):
        _require_existing_bundle_protocol(self._bundle_path(protocol_binding()))

    def test_bundle_without_protocol_refused(self):
        path = self._bundle_path(None)
        with self.assertRaises(ContractError) as ctx:
            _require_existing_bundle_protocol(path)
        self.assertEqual(ctx.exception.code, "bundle_protocol_unrecorded")

    def test_corrupt_bundle_refused(self):
        path = self.tmp / "artifacts.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ContractError) as ctx:
            _require_existing_bundle_protocol(path)
        self.assertEqual(ctx.exception.code, "artifact_bundle_malformed")

    def test_bundle_with_stale_protocol_refused(self):
        path = self._bundle_path(
            {"name": "old", "version": "old", "sha256": "b" * 64}
        )
        with self.assertRaises(ContractError) as ctx:
            _require_existing_bundle_protocol(path)
        self.assertEqual(ctx.exception.code, "bundle_protocol_mismatch")


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="legacy-migrate-test-"))

    def _legacy_bundle(self) -> Path:
        pred_dir = self.tmp / "candidate" / "colabdesign_monomer" / "model_0_seed_0"
        pred_dir.mkdir(parents=True, exist_ok=True)
        metadata = pred_dir / "metadata.json"
        metadata.write_text("{}", encoding="utf-8")
        bundle = {
            "schema_version": 1,
            "candidate_id": "C0001",
            "sequence": "ACDE",
            "global": {
                "monomer_predictions": [{
                    "predictor": "ColabDesign",
                    "seed": 0,
                    "metadata": str(metadata),
                }]
            },
            "targets": {},
        }
        path = self.tmp / "candidate" / "artifacts.json"
        path.write_text(json.dumps(bundle), encoding="utf-8")
        return path

    def test_migrates_legacy_bundle_and_output_dirs(self):
        bundle_path = self._legacy_bundle()
        status = migrate_bundle(bundle_path)
        self.assertTrue(status.startswith("migrated:"), status)
        data = json.loads(bundle_path.read_text(encoding="utf-8"))
        self.assertEqual(data["protocol"], protocol_binding())
        binding_path = (
            self.tmp / "candidate" / "colabdesign_monomer"
            / "model_0_seed_0" / PROTOCOL_BINDING_FILENAME
        )
        self.assertTrue(binding_path.is_file())

    def test_dry_run_does_not_write(self):
        bundle_path = self._legacy_bundle()
        status = migrate_bundle(bundle_path, dry_run=True)
        self.assertTrue(status.startswith("would-migrate:"), status)
        data = json.loads(bundle_path.read_text(encoding="utf-8"))
        self.assertNotIn("protocol", data)

    def test_already_bound_bundle_skipped(self):
        bundle_path = self._legacy_bundle()
        migrate_bundle(bundle_path)
        status = migrate_bundle(bundle_path)
        self.assertTrue(status.startswith("skip:"), status)

    def test_differing_protocol_refused(self):
        bundle_path = self._legacy_bundle()
        data = json.loads(bundle_path.read_text(encoding="utf-8"))
        data["protocol"] = {"name": "old", "version": "old", "sha256": "b" * 64}
        bundle_path.write_text(json.dumps(data), encoding="utf-8")
        status = migrate_bundle(bundle_path)
        self.assertTrue(status.startswith("error:"), status)

    def test_non_bundle_dict_rejected(self):
        path = self.tmp / "artifacts.json"
        path.write_text(json.dumps({"state": "not-a-bundle"}), encoding="utf-8")
        status = migrate_bundle(path)
        self.assertTrue(status.startswith("error:"), status)
        self.assertIn("not an artifact bundle", status)

    def test_bundle_without_schema_version_rejected(self):
        path = self.tmp / "artifacts.json"
        path.write_text(
            json.dumps({"candidate_id": "C0001"}), encoding="utf-8"
        )
        status = migrate_bundle(path)
        self.assertTrue(status.startswith("error:"), status)

    def test_migrate_reports_missing_output_dirs(self):
        bundle_path = self._legacy_bundle()
        data = json.loads(bundle_path.read_text(encoding="utf-8"))
        data["global"]["monomer_predictions"] = [{
            "metadata": str(self.tmp / "candidate" / "ghost" / "metadata.json"),
        }]
        bundle_path.write_text(json.dumps(data), encoding="utf-8")
        status = migrate_bundle(bundle_path)
        self.assertTrue(status.startswith("migrated:"), status)
        self.assertIn("1 missing", status)
        self.assertFalse((self.tmp / "candidate" / "ghost").exists())

    def test_migrate_leaves_no_tmp_files(self):
        bundle_path = self._legacy_bundle()
        migrate_bundle(bundle_path)
        leftovers = [path for path in self.tmp.rglob("*.tmp")]
        self.assertEqual(leftovers, [])

    def test_already_bound_bundle_repairs_missing_binding(self):
        bundle_path = self._legacy_bundle()
        migrate_bundle(bundle_path)
        pred_dir = (
            self.tmp / "candidate" / "colabdesign_monomer" / "model_0_seed_0"
        )
        (pred_dir / PROTOCOL_BINDING_FILENAME).unlink()
        status = migrate_bundle(bundle_path)
        self.assertTrue(status.startswith("repaired:"), status)
        self.assertTrue((pred_dir / PROTOCOL_BINDING_FILENAME).is_file())

    def test_already_bound_bundle_with_stale_binding_repaired(self):
        bundle_path = self._legacy_bundle()
        migrate_bundle(bundle_path)
        pred_dir = (
            self.tmp / "candidate" / "colabdesign_monomer" / "model_0_seed_0"
        )
        (pred_dir / PROTOCOL_BINDING_FILENAME).write_text(
            json.dumps({"name": "old", "version": "old", "sha256": "b" * 64}),
            encoding="utf-8",
        )
        status = migrate_bundle(bundle_path)
        self.assertTrue(status.startswith("repaired:"), status)
        recorded = json.loads(
            (pred_dir / PROTOCOL_BINDING_FILENAME).read_text(encoding="utf-8")
        )
        self.assertEqual(recorded, protocol_binding())

    def test_repair_dry_run_does_not_write(self):
        bundle_path = self._legacy_bundle()
        migrate_bundle(bundle_path)
        pred_dir = (
            self.tmp / "candidate" / "colabdesign_monomer" / "model_0_seed_0"
        )
        (pred_dir / PROTOCOL_BINDING_FILENAME).unlink()
        status = migrate_bundle(bundle_path, dry_run=True)
        self.assertTrue(status.startswith("would-repair:"), status)
        self.assertFalse((pred_dir / PROTOCOL_BINDING_FILENAME).exists())



class ResolveTargetsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="resolve-targets-test-"))

    def test_explicit_non_bundle_filename_rejected(self):
        path = self.tmp / "state.json"
        path.write_text("{}", encoding="utf-8")
        with self.assertRaises(SystemExit):
            _resolve_targets([str(path)])

    def test_directory_scan_only_collects_artifacts_json(self):
        bundle = self.tmp / "candidate" / "artifacts.json"
        bundle.parent.mkdir(parents=True, exist_ok=True)
        bundle.write_text("{}", encoding="utf-8")
        (self.tmp / "candidate" / "state.json").write_text("{}", encoding="utf-8")
        targets = _resolve_targets([str(self.tmp)])
        self.assertEqual(targets, [bundle.resolve()])

    def test_missing_path_rejected(self):
        with self.assertRaises(SystemExit):
            _resolve_targets([str(self.tmp / "nope")])


class EnrichPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="enrich-preflight-test-"))
        self.bundle_path = self.tmp / "artifacts.json"
        self.bundle_path.write_text("{}", encoding="utf-8")

    def test_legacy_bundle_rejected_before_compute(self):
        with self.assertRaises(ContractError) as ctx:
            preflight_bundle_protocol(self.bundle_path, {})
        self.assertEqual(ctx.exception.code, "bundle_protocol_mismatch")

    def test_stale_bundle_rejected_before_compute(self):
        raw = {"protocol": {"name": "old", "version": "old", "sha256": "b" * 64}}
        with self.assertRaises(ContractError) as ctx:
            preflight_bundle_protocol(self.bundle_path, raw)
        self.assertEqual(ctx.exception.code, "bundle_protocol_mismatch")

    def test_matching_bundle_passes_preflight(self):
        preflight_bundle_protocol(self.bundle_path, {"protocol": protocol_binding()})


class MigrationDefensiveTests(unittest.TestCase):
    def test_malformed_global_and_targets_do_not_crash(self):
        bundle = {"global": None, "targets": []}
        self.assertEqual(_prediction_output_dirs(bundle, Path("/tmp")), set())

    def test_non_dict_predictions_skipped(self):
        bundle = {
            "global": {"monomer_predictions": ["not-a-dict"]},
            "targets": {"T1": {"complex_predictions": [None, 42]}},
        }
        self.assertEqual(_prediction_output_dirs(bundle, Path("/tmp")), set())

    def test_relative_metadata_resolved_under_bundle(self):
        bundle = {
            "global": {"monomer_predictions": [{
                "metadata": "colabdesign_monomer/model_0_seed_0/metadata.json",
            }]},
            "targets": {},
        }
        dirs = _prediction_output_dirs(bundle, Path("/base"))
        self.assertEqual(
            dirs, {Path("/base/colabdesign_monomer/model_0_seed_0")}
        )

class CLIProtocolDefaultsTests(unittest.TestCase):
    def test_model_numbers_default_from_protocol(self):
        from scripts.run_prediction_predictors import build_parser
        args = build_parser().parse_args(["--artifacts-root", "."])
        self.assertEqual(
            args.model_numbers,
            ",".join(str(value) for value in PREDICTION_PROTOCOL["af2_prodigy"]["model_numbers"]),
        )

    def test_seeds_default_from_protocol(self):
        from scripts.run_prediction_predictors import build_parser
        args = build_parser().parse_args(["--artifacts-root", "."])
        self.assertEqual(
            args.seeds,
            ",".join(str(value) for value in PREDICTION_PROTOCOL["af2_prodigy"]["seeds"]),
        )


class ProtocolParameterGateTests(unittest.TestCase):
    """Core invariant: execution parameters must match the protocol file."""

    def _ensemble(self, seeds=(0, 1, 2), models=(0, 1, 2)):
        return list(zip(seeds, models))

    def test_protocol_parameters_accepted(self):
        _require_protocol_parameters(self._ensemble(), num_recycles=3)

    def test_non_protocol_seeds_rejected(self):
        with self.assertRaises(ContractError) as ctx:
            _require_protocol_parameters(self._ensemble(seeds=(5, 6, 7)), 3)
        self.assertEqual(ctx.exception.code, "protocol_parameter_mismatch")

    def test_non_protocol_models_rejected(self):
        with self.assertRaises(ContractError):
            _require_protocol_parameters(self._ensemble(models=(1, 1, 2)), 3)

    def test_non_protocol_recycles_rejected(self):
        with self.assertRaises(ContractError) as ctx:
            _require_protocol_parameters(self._ensemble(), num_recycles=5)
        self.assertEqual(ctx.exception.code, "protocol_parameter_mismatch")


class EnrichmentProtocolGateTests(unittest.TestCase):
    def _args(self, seed=101, post_relax_seed=20260802, repeats=3):
        return types.SimpleNamespace(
            seed=seed, post_relax_seed=post_relax_seed, post_relax_repeats=repeats,
        )

    def test_protocol_derived_args_accepted(self):
        from scripts.enrich_prediction_evidence import _require_enrichment_protocol
        _require_enrichment_protocol(self._args())

    def test_shifted_candidate_offset_accepted(self):
        from scripts.enrich_prediction_evidence import _require_enrichment_protocol
        _require_enrichment_protocol(self._args(seed=102, post_relax_seed=20260803))

    def test_non_protocol_repeats_rejected(self):
        from scripts.enrich_prediction_evidence import _require_enrichment_protocol
        with self.assertRaises(ContractError) as ctx:
            _require_enrichment_protocol(self._args(repeats=5))
        self.assertEqual(ctx.exception.code, "protocol_parameter_mismatch")

    def test_wrong_seed_base_difference_rejected(self):
        from scripts.enrich_prediction_evidence import _require_enrichment_protocol
        with self.assertRaises(ContractError) as ctx:
            _require_enrichment_protocol(self._args(seed=101, post_relax_seed=999))
        self.assertEqual(ctx.exception.code, "protocol_parameter_mismatch")

    def test_seed_below_base_rejected(self):
        # Same base difference, but the absolute seed is below the protocol
        # seed_base: must be refused so evidence can be traced to the base.
        from scripts.enrich_prediction_evidence import _require_enrichment_protocol
        with self.assertRaises(ContractError) as ctx:
            _require_enrichment_protocol(self._args(seed=100, post_relax_seed=20260801))
        self.assertEqual(ctx.exception.code, "protocol_parameter_mismatch")

    def test_post_relax_seed_below_base_rejected(self):
        from scripts.enrich_prediction_evidence import _require_enrichment_protocol
        with self.assertRaises(ContractError) as ctx:
            _require_enrichment_protocol(self._args(seed=101, post_relax_seed=100))
        self.assertEqual(ctx.exception.code, "protocol_parameter_mismatch")


class ResumeParameterMismatchTests(unittest.TestCase):
    """P1-1: --resume must prove the recorded execution parameters match."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="resume-param-test-"))

    def _output_dir(self, metadata: dict) -> Path:
        output_dir = self.tmp / "model_0_seed_0"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "metadata.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )
        (output_dir / PROTOCOL_BINDING_FILENAME).write_text(
            json.dumps(protocol_binding()), encoding="utf-8"
        )
        return output_dir

    def _args(self):
        return types.SimpleNamespace(resume=True, timeout=30, cuda_data_dir="")

    def _recorded(self, **overrides):
        data = {
            "requested_sequence": "ACDE",
            "observed_sequence": "ACDE",
            "seed": 0,
            "model_number": 0,
            "num_recycles": 3,
        }
        data.update(overrides)
        return data

    def _expected(self, **overrides):
        data = {
            "requested_sequence": "ACDE",
            "seed": 0,
            "model_number": 0,
            "num_recycles": 3,
        }
        data.update(overrides)
        return data

    def test_matching_parameters_reuse(self):
        output_dir = self._output_dir(self._recorded())
        result = _run_one(
            ["echo"], output_dir, self._args(), expected=self._expected()
        )
        self.assertEqual(result["requested_sequence"], "ACDE")

    def test_different_sequence_refused(self):
        output_dir = self._output_dir(self._recorded(requested_sequence="ACDE"))
        with self.assertRaises(ContractError) as ctx:
            _run_one(
                ["echo"], output_dir, self._args(),
                expected=self._expected(requested_sequence="EFGH"),
            )
        self.assertEqual(ctx.exception.code, "resume_parameter_mismatch")
        self.assertIn("requested_sequence", str(ctx.exception))

    def test_different_seed_refused(self):
        output_dir = self._output_dir(self._recorded(seed=0))
        with self.assertRaises(ContractError) as ctx:
            _run_one(
                ["echo"], output_dir, self._args(),
                expected=self._expected(seed=1),
            )
        self.assertEqual(ctx.exception.code, "resume_parameter_mismatch")

    def test_different_model_and_recycles_refused(self):
        output_dir = self._output_dir(
            self._recorded(model_number=0, num_recycles=3)
        )
        with self.assertRaises(ContractError) as ctx:
            _run_one(
                ["echo"], output_dir, self._args(),
                expected=self._expected(model_number=2, num_recycles=5),
            )
        self.assertEqual(ctx.exception.code, "resume_parameter_mismatch")

    def test_missing_parameter_field_refused(self):
        # Fail closed: legacy metadata without the parameters must not resume.
        output_dir = self._output_dir({"model_id": "model_0"})
        with self.assertRaises(ContractError) as ctx:
            _run_one(
                ["echo"], output_dir, self._args(), expected=self._expected()
            )
        self.assertEqual(ctx.exception.code, "resume_parameter_mismatch")

    def test_non_dict_metadata_refused(self):
        # Valid JSON but not an object: must surface as a clean error code.
        output_dir = self._output_dir({})
        (output_dir / "metadata.json").write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertRaises(ContractError) as ctx:
            _run_one(
                ["echo"], output_dir, self._args(), expected=self._expected()
            )
        self.assertEqual(ctx.exception.code, "predictor_metadata_corrupt")

    def test_corrupt_binding_encoding_refused(self):
        # Non-UTF-8 binding file must not raise a raw UnicodeDecodeError.
        output_dir = self._output_dir(self._recorded())
        (output_dir / PROTOCOL_BINDING_FILENAME).write_bytes(b"\xff\xfe\x00{")
        with self.assertRaises(ContractError) as ctx:
            _run_one(["echo"], output_dir, self._args(), expected=self._expected())
        self.assertEqual(ctx.exception.code, "resume_protocol_corrupt")


class MigrationParameterWarningTests(unittest.TestCase):
    """P2-4: migration warns when recorded params differ from the protocol."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="migrate-warn-test-"))

    def _bundle_with_metadata(self, metadata: dict) -> Path:
        pred_dir = self.tmp / "candidate" / "colabdesign_monomer" / "model_0_seed_0"
        pred_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = pred_dir / "metadata.json"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        bundle = {
            "schema_version": 1,
            "candidate_id": "C0001",
            "sequence": "ACDE",
            "global": {
                "monomer_predictions": [{
                    "predictor": "ColabDesign",
                    "seed": 0,
                    "metadata": str(metadata_path),
                }]
            },
            "targets": {},
        }
        path = self.tmp / "candidate" / "artifacts.json"
        path.write_text(json.dumps(bundle), encoding="utf-8")
        return path

    def test_dry_run_warns_on_recycles_difference(self):
        bundle_path = self._bundle_with_metadata(
            {"num_recycles": 5, "model_number": 0, "seed": 0}
        )
        status = migrate_bundle(bundle_path, dry_run=True)
        self.assertTrue(status.startswith("would-migrate:"), status)
        self.assertIn("warning:", status)
        self.assertIn("num_recycles=5", status)

    def test_migrate_warns_on_model_number_difference(self):
        bundle_path = self._bundle_with_metadata(
            {"num_recycles": 3, "model_number": 4, "seed": 0}
        )
        status = migrate_bundle(bundle_path)
        self.assertTrue(status.startswith("migrated:"), status)
        self.assertIn("warning:", status)
        self.assertIn("model_number=4", status)

    def test_matching_parameters_no_warning(self):
        bundle_path = self._bundle_with_metadata(
            {"num_recycles": 3, "model_number": 0, "seed": 0}
        )
        status = migrate_bundle(bundle_path, dry_run=True)
        self.assertTrue(status.startswith("would-migrate:"), status)
        self.assertNotIn("warning:", status)


if __name__ == "__main__":
    unittest.main()


