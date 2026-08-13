"""Focused threshold Artifact regressions for bootstrap Prediction."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from calibration_baseline import unpublished_calibration_binding
from execution.contracts import ExecutionContractError
from execution.prediction_effects import load_prediction_transaction_effects
from prediction_pipeline.contracts import file_sha256
from prediction_pipeline.pipeline import PredictionPipeline
from prediction_pipeline.protocol import protocol_binding
from test_prediction_transactional import PredictionTransactionalTests
from threshold_contract import canonical_threshold_digest


class PredictionThresholdLocatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = PredictionTransactionalTests(methodName="runTest")
        self.fixture.setUp()

    def test_transaction_commits_threshold_as_additional_artifact(self) -> None:
        store, _, context, result = self.fixture._run(
            self.fixture.root / "threshold-success"
        )
        artifact_id = f"{context.transaction_id}-prediction-thresholds"
        artifact = store.get_artifact(artifact_id)
        path = Path(artifact["path"])
        handoff = next(
            item for item in store.query(task_id="T001")
            if item["event_type"] == "prediction_handoff_ready"
        )

        self.assertEqual([role for role, _ in result.outputs], ["prediction_handoff"])
        self.assertIn("prediction_thresholds", {
            item.artifact_type for item in result.artifacts
        })
        self.assertEqual(artifact["sha256"], file_sha256(path))
        self.assertEqual(handoff["thresholds_artifact_id"], artifact_id)
        self.assertEqual(handoff["thresholds_digest"], canonical_threshold_digest({}))
        self.assertNotIn("thresholds_artifact_path", handoff)
        self.assertNotIn("thresholds_artifact_sha256", handoff)

    def test_effects_reject_threshold_snapshot_digest_mismatch(self) -> None:
        root = self.fixture.root / "threshold-effects-mismatch"
        pipeline = PredictionPipeline(
            candidate_rows=[self.fixture.row], project=self.fixture.project,
            thresholds={}, artifacts_root=root / "missing-artifacts",
            run_root=root / "runs", run_id="prediction_threshold_mismatch",
            defer_formal_writes=True, artifact_id_prefix="tx-threshold-mismatch",
        )
        pipeline.run()
        effects = pipeline.transaction_effects()
        Path(effects["thresholds_artifact"]["path"]).write_text(
            '{"changed": true}', encoding="utf-8"
        )
        effects_path = root / "effects.json"
        effects_path.write_text(json.dumps(effects), encoding="utf-8")

        with self.assertRaises(ExecutionContractError) as captured:
            load_prediction_transaction_effects(
                path=effects_path, candidate_ids=["C0001"],
                run_id="prediction_threshold_mismatch",
                transaction_id="tx-threshold-mismatch",
                expected_protocol=protocol_binding(),
                expected_calibration_binding=unpublished_calibration_binding({}),
            )
        self.assertEqual(captured.exception.code, "prediction_effects_scope_mismatch")

    def test_effects_reject_missing_or_malformed_threshold_snapshot(self) -> None:
        for case in ("missing", "malformed"):
            with self.subTest(case=case):
                root = self.fixture.root / f"threshold-effects-{case}"
                pipeline = PredictionPipeline(
                    candidate_rows=[self.fixture.row], project=self.fixture.project,
                    thresholds={}, artifacts_root=root / "missing-artifacts",
                    run_root=root / "runs", run_id=f"prediction_threshold_{case}",
                    defer_formal_writes=True,
                    artifact_id_prefix=f"tx-threshold-{case}",
                )
                pipeline.run()
                effects = pipeline.transaction_effects()
                threshold_path = Path(effects["thresholds_artifact"]["path"])
                if case == "missing":
                    threshold_path.unlink()
                else:
                    threshold_path.write_text("{", encoding="utf-8")
                effects_path = root / "effects.json"
                effects_path.write_text(json.dumps(effects), encoding="utf-8")

                with self.assertRaises(ExecutionContractError) as captured:
                    load_prediction_transaction_effects(
                        path=effects_path, candidate_ids=["C0001"],
                        run_id=f"prediction_threshold_{case}",
                        transaction_id=f"tx-threshold-{case}",
                        expected_protocol=protocol_binding(),
                        expected_calibration_binding=unpublished_calibration_binding({}),
                    )
                self.assertEqual(captured.exception.code, "prediction_effects_invalid")


if __name__ == "__main__":
    unittest.main()
