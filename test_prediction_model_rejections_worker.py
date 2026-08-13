"""Execution-boundary regressions for model-rejection bundles."""

from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from execution.contracts import ExecutionContractError
from execution.handlers import (
    HandlerContext,
    _artifact_bundle_complete,
    evaluate_new_design_candidates,
)
from prediction_pipeline.adapters import ContractError
from prediction_pipeline.contracts import SCHEMA_VERSION
from prediction_pipeline.protocol import protocol_binding
from test_prediction_model_rejections_artifacts import (
    PredictionModelRejectionArtifactTests,
)


class PredictionModelRejectionWorkerTests(unittest.TestCase):
    def test_completion_gate_loads_real_valid_mixed_xor_bundle(self):
        fixture = PredictionModelRejectionArtifactTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.predictions[2]["predictor"] = "Boltz"
        target = fixture._target()
        target["rosetta_outputs"] = [
            fixture._output(fixture.predictions[0]),
            fixture._output(fixture.predictions[2]),
        ]
        target["rosetta_rejections"] = [fixture._rejection(fixture.predictions[1])]
        fixture._load(target)
        bundle = fixture.root / "artifacts.json"
        raw = json.loads(bundle.read_text(encoding="utf-8"))
        source = fixture.root / fixture.predictions[0]["pdb"]
        metadata = fixture.root / fixture.predictions[0]["metadata"]
        raw["protocol"] = protocol_binding()
        raw["global"] = {
            "monomer_predictions": [{
                "predictor": "ColabDesign", "seed": 0,
                "pdb": source.name,
                "pdb_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "metadata": metadata.name,
                "metadata_sha256": hashlib.sha256(metadata.read_bytes()).hexdigest(),
            }],
            "post_relax_pdb": source.name,
            "post_relax_pdb_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "post_relax_metadata": metadata.name,
            "post_relax_metadata_sha256": hashlib.sha256(metadata.read_bytes()).hexdigest(),
        }
        bundle.write_text(json.dumps(raw), encoding="utf-8")

        from execution import prediction_artifact_gate as gate_module
        protocol = dict(gate_module.PREDICTION_PROTOCOL["parameters"]["af2_prodigy"])
        protocol["seeds"] = [100, 101]
        with patch.dict(
            gate_module.PREDICTION_PROTOCOL["parameters"],
            {"af2_prodigy": protocol},
        ):
            self.assertTrue(_artifact_bundle_complete(bundle, ["MDM2"]))

    def test_invalid_xor_fails_real_completion_gate_before_prediction_ingest(self):
        root = Path(tempfile.mkdtemp(prefix="prediction-rejection-handler-"))
        existing = root / "existing" / "C0001"
        existing.mkdir(parents=True)
        (existing / "artifacts.json").write_text(json.dumps({
            "schema_version": SCHEMA_VERSION,
            "candidate_id": "C0001",
            "sequence": "ACDEFGHI",
            "protocol": {},
            "global": {},
            "targets": {},
        }), encoding="utf-8")
        (existing / "execution_identity.json").write_text("{}", encoding="utf-8")
        packet = {
            "run_id": "orchestrator_rejection_test",
            "task_attempt": 1,
            "task": {
                "task_id": "T001",
                "action": "evaluate_new_design_candidates",
                "phase": "evaluate",
                "parameters": {
                    "reuse_complete_evidence": True,
                    "evidence_mode": "ingest_existing",
                    "predictor_protocol": {},
                    "execution_identity": {},
                },
                "candidate_scope": {"candidate_ids": ["C0001"]},
                "resource_request": {"candidate_limit": 1},
            },
        }
        context = HandlerContext(
            packet=packet,
            config=type("Config", (), {"prediction_artifacts_root": root / "existing"})(),
            task_dir=root,
            project_config={"targets": [{"id": "MDM2", "required": True}]},
            transaction_managed=True,
            transaction_id="tx-rejection-test",
        )

        with (
            patch("execution.handlers.validate_task_parameters", return_value=packet["task"]["parameters"]),
            patch("execution.handlers.State.load", return_value={"thresholds": {}}),
            patch(
                "execution.prediction_artifact_gate.validate_execution_compatibility"
            ),
            patch(
                "execution.prediction_artifact_gate.load_artifact_bundle",
                side_effect=ContractError(
                    "rosetta_coverage_mismatch", "mixed bundle has overlap"
                ),
            ) as loader,
            patch("execution.handlers.run_process") as ingest,
            self.assertRaises(ExecutionContractError) as raised,
        ):
            evaluate_new_design_candidates(context)

        self.assertEqual(raised.exception.code, "prediction_artifacts_missing")
        loader.assert_called_once()
        ingest.assert_not_called()
        self.assertFalse((root / "prediction_transaction_effects.json").exists())
        self.assertFalse((root / "prediction_runs").exists())


if __name__ == "__main__":
    unittest.main()
