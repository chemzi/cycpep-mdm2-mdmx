"""Integration regression from calibration authority to exploration input."""

from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path

from _prediction_test_utils import justified_thresholds
from calibration_baseline import (
    create_calibration_publication,
    validate_calibration_consumption,
)
from contracts.transaction import TransactionStatus
from execution.contracts import ExecutionContractError
from execution.prediction_effects import load_prediction_transaction_effects
from prediction_pipeline.contracts import file_sha256, scoring_implementation_identity
from prediction_pipeline.pipeline import PredictionPipeline
from prediction_pipeline.protocol import protocol_binding
from storage import SQLiteStore
from test_calibration_baseline import simulation_dataset
import test_prediction_transactional as transactional_tests
from threshold_calibration import calibrate_thresholds


class CalibrationPredictionExplorationTests(unittest.TestCase):
    def setUp(self):
        self.harness = transactional_tests.PredictionTransactionalTests()
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    @staticmethod
    def _publication(dataset, project, path: Path) -> tuple[dict, dict]:
        thresholds, audit = calibrate_thresholds(
            controls=dataset,
            thresholds=justified_thresholds(),
            target_ids=("MDM2",),
            protocol=protocol_binding(),
            metric_keys=("L7_scrmsd",),
        )
        publication = create_calibration_publication(
            dataset=dataset,
            thresholds=thresholds,
            audit=audit,
            project=project,
            calibration_authority="simulation_only",
            protocol_identity=protocol_binding(),
            scoring_implementation=scoring_implementation_identity(),
            artifact_path=path,
        )
        return thresholds, publication

    def _assert_alternate_calibration_rejected(
        self,
        *,
        context,
        effects: dict,
        alternate_binding: dict,
        expected_binding: dict,
        prediction_run_id: str,
    ) -> None:
        forged = deepcopy(effects)
        forged_root = context.task_dir / "forged-calibration-authority"
        forged_root.mkdir()
        record = forged["record_artifacts"][0]
        record_path = forged_root / "record.json"
        record_document = json.loads(Path(record["path"]).read_text(encoding="utf-8"))
        record_document["calibration_binding"] = deepcopy(alternate_binding)
        record_document["cache_key"]["calibration_binding"] = deepcopy(
            alternate_binding
        )
        record_path.write_text(json.dumps(record_document), encoding="utf-8")
        record["path"] = str(record_path)
        record_sha = file_sha256(record_path)

        patch = json.loads(forged["candidate_patches"][0]["patch"]["metrics_json"])
        patch["prediction"]["calibration_binding"] = deepcopy(alternate_binding)
        patch["prediction"]["record_sha256"] = record_sha
        forged["candidate_patches"][0]["patch"]["metrics_json"] = json.dumps(patch)
        forged["state_updates"]["prediction"]["calibration_binding"] = deepcopy(
            alternate_binding
        )
        forged["state_appends"][0]["item"]["summary"] = deepcopy(
            forged["state_updates"]["prediction"]
        )
        for event in forged["evidence_events"]:
            event["calibration_binding"] = deepcopy(alternate_binding)

        handoff_path = forged_root / "handoff.json"
        handoff = json.loads(
            Path(forged["handoff_artifact"]["path"]).read_text(encoding="utf-8")
        )
        handoff["calibration_binding"] = deepcopy(alternate_binding)
        for category in handoff["categories"].values():
            for item in category:
                item["record_path"] = str(record_path)
                item["record_sha256"] = record_sha
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        forged["handoff_artifact"]["path"] = str(handoff_path)
        path = forged_root / "effects.json"
        path.write_text(json.dumps(forged), encoding="utf-8")

        with self.assertRaisesRegex(ExecutionContractError, "formal State"):
            load_prediction_transaction_effects(
                path=path,
                candidate_ids=["C0001"],
                run_id=prediction_run_id,
                transaction_id=context.transaction_id,
                expected_protocol=protocol_binding(),
                expected_calibration_binding=expected_binding,
            )

    def test_simulation_calibration_prediction_transaction_builds_e2_decision(self):
        project = self.harness.project
        dataset = simulation_dataset(project)
        thresholds, publication = self._publication(
            dataset, project, self.harness.root / "calibration-to-e2.json"
        )
        alternate_dataset = deepcopy(dataset)
        alternate_dataset["controls"][0]["metrics"]["global"]["scrmsd"] = 0.45
        _, alternate = self._publication(
            alternate_dataset,
            project,
            self.harness.root / "alternate-calibration.json",
        )
        runtime: dict[str, object] = {}

        def publish_and_validate(store: SQLiteStore) -> None:
            store.publish_calibration(**publication)
            artifact = store.get_artifact(publication["binding"]["artifact_id"])
            runtime["binding"] = validate_calibration_consumption(
                binding=publication["binding"],
                thresholds=thresholds,
                project=project,
                artifact=artifact,
                protocol_identity=protocol_binding(),
                scoring_implementation=scoring_implementation_identity(),
            )

        def calibrated_handler(context):
            prediction_run_id = f"prediction_{context.transaction_id[-12:]}"
            pipeline = PredictionPipeline(
                candidate_rows=[self.harness.row],
                project=project,
                thresholds=thresholds,
                calibration_binding=runtime["binding"],
                artifacts_root=self.harness.root / "missing-artifacts",
                run_root=context.task_dir / "prediction-runs",
                candidate_ids=["C0001"],
                run_id=prediction_run_id,
                defer_formal_writes=True,
                artifact_id_prefix=context.transaction_id,
                execution_identity=context.parameters["execution_identity"],
            )
            pipeline.run()
            effects = pipeline.transaction_effects()
            self._assert_alternate_calibration_rejected(
                context=context,
                effects=effects,
                alternate_binding=alternate["binding"],
                expected_binding=publication["binding"],
                prediction_run_id=prediction_run_id,
            )
            effects_path = context.task_dir / "calibration-effects.json"
            effects_path.write_text(json.dumps(effects), encoding="utf-8")
            validated = load_prediction_transaction_effects(
                path=effects_path,
                candidate_ids=["C0001"],
                run_id=prediction_run_id,
                transaction_id=context.transaction_id,
                expected_protocol=protocol_binding(),
                expected_calibration_binding=publication["binding"],
            )
            return self.harness._typed_result(validated, pipeline.handoff_path)

        store, _, context, _ = self.harness._run(
            self.harness.root / "calibration-prediction-to-e2",
            handler=calibrated_handler,
            store_setup=publish_and_validate,
        )
        self.assertEqual(context.status, TransactionStatus.COMMITTED)
        batteries = store.query(event_type="battery_evaluated")
        handoff = store.query(event_type="prediction_handoff_ready")
        self.assertEqual(len(batteries), len(handoff), 1)
        self.assertEqual(handoff[0]["candidate_ids"], ["C0001"])
        self.assertEqual(
            batteries[0]["prediction_run_id"], handoff[0]["prediction_run_id"]
        )
        self.assertEqual(batteries[0]["calibration_binding"], publication["binding"])
        self.assertEqual(handoff[0]["calibration_binding"], publication["binding"])

        prediction_run_id = handoff[0]["prediction_run_id"]
        record_artifact_id = f"{context.transaction_id}-prediction-record-C0001"
        record_artifact = store.get_artifact(record_artifact_id)
        record = json.loads(Path(record_artifact["path"]).read_text(encoding="utf-8"))
        self.assertEqual(record["run_id"], prediction_run_id)
        self.assertEqual(record["calibration_binding"], publication["binding"])
        candidate = store.get("C0001")
        metadata = json.loads(candidate["metrics_json"])["prediction"]
        self.assertEqual(metadata["run_id"], prediction_run_id)
        self.assertEqual(metadata["record_artifact_id"], record_artifact_id)
        self.assertEqual(metadata["calibration_binding"], publication["binding"])
        self.assertEqual(
            {event["candidate_id"] for event in batteries},
            {record["candidate"]["candidate_id"]},
        )

        decision = self.harness._e2_from_committed_prediction(
            store, thresholds=thresholds
        )
        evidence = decision.to_dict()["evidence_support"]
        self.assertEqual(
            evidence["source_evidence"][0]["calibration_binding"],
            publication["binding"],
        )
        self.assertEqual(
            evidence["prediction_handoff_evidence"]["calibration_binding"],
            publication["binding"],
        )


if __name__ == "__main__":
    unittest.main()
