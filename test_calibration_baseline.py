"""E1 CalibrationBaseline contract, publication, and consumption tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import data_layer
from _prediction_test_utils import justified_thresholds, project_config
from calibration_baseline import (
    CalibrationBaselineError,
    SCIENTIFIC_BINDING_KEYS,
    create_calibration_publication,
    validate_calibration_consumption,
)
from core.integrity import file_sha256, object_sha256
from prediction_pipeline.protocol import protocol_binding
from prediction_pipeline.contracts import ContractError, scoring_implementation_identity
from storage import SQLiteStore
from threshold_calibration import calibrate_thresholds


def simulation_dataset(project: dict, *, n_positive: int = 3, n_negative: int = 10) -> dict:
    controls = []
    for index in range(n_positive):
        controls.append({
            "control_id": f"sim-positive-{index}",
            "label": "positive",
            "role": "synthetic_positive_control",
            "source": {
                "pdb_id": f"SIM-P-{index}",
                "method": "deterministic simulation fixture",
                "synthetic": True,
            },
            "metrics": {"global": {"scrmsd": 0.5 + index * 0.1}},
        })
    for index in range(n_negative):
        controls.append({
            "control_id": f"sim-negative-{index}",
            "label": "negative",
            "role": "synthetic_negative_control",
            "source": {
                "pdb_id": f"SIM-N-{index}",
                "method": "deterministic simulation fixture",
                "synthetic": True,
            },
            "metrics": {"global": {"scrmsd": 3.0 + index * 0.1}},
        })
    return {
        "metadata": {
            "schema_version": 2,
            "project_id": project["project_id"],
            "approved_digest": project["review"]["approved_digest"],
            "protocol": protocol_binding(),
            "calibration_authority": "simulation_only",
        },
        "controls": controls,
    }


def calibrated_simulation(project: dict) -> tuple[dict, dict, dict]:
    dataset = simulation_dataset(project)
    thresholds, audit = calibrate_thresholds(
        controls=dataset,
        thresholds=justified_thresholds(),
        target_ids=("MDM2", "MDMX"),
        protocol=protocol_binding(),
        metric_keys=("L7_scrmsd",),
    )
    return dataset, thresholds, audit


class CalibrationBaselineContractTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="calibration-baseline-test-"))
        self.project = project_config(("MDM2", "MDMX"))

    def test_simulation_publication_is_deterministic_and_machine_distinct(self):
        dataset, thresholds, audit = calibrated_simulation(self.project)
        first = create_calibration_publication(
            dataset=dataset,
            thresholds=thresholds,
            audit=audit,
            project=self.project,
            calibration_authority="simulation_only",
            protocol_identity=protocol_binding(),
            scoring_implementation=scoring_implementation_identity(),
            artifact_path=self.root / "first.json",
        )
        second = create_calibration_publication(
            dataset=deepcopy(dataset),
            thresholds=deepcopy(thresholds),
            audit=deepcopy(audit),
            project=deepcopy(self.project),
            calibration_authority="simulation_only",
            protocol_identity=protocol_binding(),
            scoring_implementation=scoring_implementation_identity(),
            artifact_path=self.root / "elsewhere" / "second.json",
        )

        self.assertEqual(audit["metrics"]["L7_scrmsd"]["status"], "calibrated")
        self.assertEqual(first["binding"]["calibration_authority"], "simulation_only")
        self.assertEqual(first["binding"]["publication_id"], second["binding"]["publication_id"])
        self.assertEqual(first["binding"]["artifact_sha256"], second["binding"]["artifact_sha256"])
        artifact = json.loads(Path(first["artifact"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(artifact["scientific_binding"]["calibration_authority"], "simulation_only")
        self.assertNotIn("created_at", artifact)

    def test_synthetic_controls_cannot_be_published_as_approved_real(self):
        dataset, thresholds, audit = calibrated_simulation(self.project)
        with self.assertRaisesRegex(CalibrationBaselineError, "synthetic"):
            create_calibration_publication(
                dataset=dataset,
                thresholds=thresholds,
                audit=audit,
                project=self.project,
                calibration_authority="approved_real",
                protocol_identity=protocol_binding(),
                scoring_implementation=scoring_implementation_identity(),
                artifact_path=self.root / "invalid.json",
            )

    def test_approved_real_rejects_missing_real_control_provenance(self):
        dataset, thresholds, audit = calibrated_simulation(self.project)
        dataset["metadata"]["calibration_authority"] = "approved_real"
        for control in dataset["controls"]:
            control["role"] = "experimental_control"
            control["source"] = {}
        with self.assertRaisesRegex(CalibrationBaselineError, "provenance"):
            create_calibration_publication(
                dataset=dataset,
                thresholds=thresholds,
                audit=audit,
                project=self.project,
                calibration_authority="approved_real",
                protocol_identity=protocol_binding(),
                scoring_implementation=scoring_implementation_identity(),
                artifact_path=self.root / "forged-real.json",
            )

    def test_legacy_dataset_and_unaudited_calibrated_metric_cannot_publish(self):
        dataset, thresholds, audit = calibrated_simulation(self.project)
        dataset["metadata"]["schema_version"] = 1
        with self.assertRaisesRegex(CalibrationBaselineError, "schema_version 2"):
            create_calibration_publication(
                dataset=dataset, thresholds=thresholds, audit=audit,
                project=self.project, calibration_authority="simulation_only",
                protocol_identity=protocol_binding(),
                scoring_implementation=scoring_implementation_identity(),
                artifact_path=self.root / "legacy.json",
            )
        dataset["metadata"]["schema_version"] = 2
        thresholds["L1_plddt"]["calibration_status"] = "calibrated"
        with self.assertRaisesRegex(CalibrationBaselineError, "do not match"):
            create_calibration_publication(
                dataset=dataset, thresholds=thresholds, audit=audit,
                project=self.project, calibration_authority="simulation_only",
                protocol_identity=protocol_binding(),
                scoring_implementation=scoring_implementation_identity(),
                artifact_path=self.root / "extra-calibrated.json",
            )


class CalibrationPublicationStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="calibration-store-test-"))
        self.project = project_config(("MDM2", "MDMX"))
        self.store = SQLiteStore(
            self.root / "store.db", project_id=self.project["project_id"]
        )
        self.store.replace_state(self.project["project_id"], {
            "project_id": self.project["project_id"],
            "approved_digest": self.project["review"]["approved_digest"],
            "project_config": self.project,
            "thresholds": justified_thresholds(),
        })
        dataset, thresholds, audit = calibrated_simulation(self.project)
        self.publication = create_calibration_publication(
            dataset=dataset,
            thresholds=thresholds,
            audit=audit,
            project=self.project,
            calibration_authority="simulation_only",
            protocol_identity=protocol_binding(),
            scoring_implementation=scoring_implementation_identity(),
            artifact_path=self.root / "baseline.json",
        )

    def test_publication_is_atomic_formal_authority_and_exact_replay_is_idempotent(self):
        first = self.store.publish_calibration(**self.publication)
        dataset, thresholds, audit = calibrated_simulation(self.project)
        replay = create_calibration_publication(
            dataset=dataset,
            thresholds=thresholds,
            audit=audit,
            project=self.project,
            calibration_authority="simulation_only",
            protocol_identity=protocol_binding(),
            scoring_implementation=scoring_implementation_identity(),
            artifact_path=self.root / "replay-location.json",
        )
        second = self.store.publish_calibration(**replay)

        self.assertEqual(first["status"], "published")
        self.assertEqual(second["status"], "idempotent")
        self.assertEqual(first["publication_id"], second["publication_id"])
        state = self.store.get_state(self.project["project_id"])
        self.assertEqual(state["threshold_calibration_binding"], self.publication["binding"])
        self.assertEqual(state["thresholds"], self.publication["thresholds"])
        artifact = self.store.get_artifact(self.publication["binding"]["artifact_id"])
        self.assertEqual(artifact["sha256"], self.publication["binding"]["artifact_sha256"])
        self.assertEqual(artifact["path"], self.publication["artifact"]["path"])
        events = self.store.query(
            project_id=self.project["project_id"],
            event_type="threshold_calibration_published",
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["calibration_binding"], self.publication["binding"])

    def test_same_publication_identity_with_different_content_fails_closed(self):
        self.store.publish_calibration(**self.publication)
        changed = deepcopy(self.publication)
        changed["thresholds"]["L7_scrmsd"]["value"] += 0.25
        with self.assertRaisesRegex(ValueError, "threshold snapshot"):
            self.store.publish_calibration(**changed)
        self.assertEqual(
            self.store.get_state(self.project["project_id"])["thresholds"],
            self.publication["thresholds"],
        )

    def test_identical_publication_is_idempotent_after_another_active_baseline(self):
        self.store.publish_calibration(**self.publication)
        dataset = simulation_dataset(self.project)
        dataset["controls"][0]["metrics"]["global"]["scrmsd"] = 0.2
        thresholds, audit = calibrate_thresholds(
            controls=dataset,
            thresholds=justified_thresholds(),
            target_ids=("MDM2", "MDMX"),
            protocol=protocol_binding(),
            metric_keys=("L7_scrmsd",),
        )
        other = create_calibration_publication(
            dataset=dataset,
            thresholds=thresholds,
            audit=audit,
            project=self.project,
            calibration_authority="simulation_only",
            protocol_identity=protocol_binding(),
            scoring_implementation=scoring_implementation_identity(),
            artifact_path=self.root / "other.json",
        )
        self.store.publish_calibration(**other)

        replay = self.store.publish_calibration(**self.publication)

        self.assertEqual(replay["status"], "idempotent")
        state = self.store.get_state(self.project["project_id"])
        self.assertEqual(state["threshold_calibration_binding"], self.publication["binding"])
        events = self.store.query(
            project_id=self.project["project_id"],
            event_type="threshold_calibration_published",
        )
        self.assertEqual(len(events), 2)

    def test_evidence_collision_rolls_back_artifact_and_state(self):
        publication_id = self.publication["binding"]["publication_id"]
        self.store.append({
            "event_id": f"{publication_id}-published",
            "agent": "test",
            "event_type": "reserved_collision",
            "project_id": self.project["project_id"],
        })
        before = self.store.get_state(self.project["project_id"])
        with self.assertRaises(Exception):
            self.store.publish_calibration(**self.publication)
        self.assertEqual(self.store.get_state(self.project["project_id"]), before)
        self.assertIsNone(
            self.store.get_artifact(self.publication["binding"]["artifact_id"])
        )

    def test_consumption_validates_exact_store_artifact_and_detects_tamper(self):
        self.store.publish_calibration(**self.publication)
        artifact = self.store.get_artifact(self.publication["binding"]["artifact_id"])
        consumed = validate_calibration_consumption(
            binding=self.publication["binding"],
            thresholds=self.publication["thresholds"],
            project=self.project,
            artifact=artifact,
            protocol_identity=protocol_binding(),
            scoring_implementation=scoring_implementation_identity(),
        )
        self.assertEqual(consumed, self.publication["binding"])

        Path(artifact["path"]).write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(CalibrationBaselineError, "artifact content"):
            validate_calibration_consumption(
                binding=self.publication["binding"],
                thresholds=self.publication["thresholds"],
                project=self.project,
                artifact=artifact,
                protocol_identity=protocol_binding(),
                scoring_implementation=scoring_implementation_identity(),
            )

    def test_dataset_binding_tamper_fails_closed(self):
        self.store.publish_calibration(**self.publication)
        binding = deepcopy(self.publication["binding"])
        binding["dataset_sha256"] = "0" * 64
        with self.assertRaisesRegex(CalibrationBaselineError, "scientific binding"):
            validate_calibration_consumption(
                binding=binding,
                thresholds=self.publication["thresholds"],
                project=self.project,
                artifact=self.store.get_artifact(binding["artifact_id"]),
                protocol_identity=protocol_binding(),
                scoring_implementation=scoring_implementation_identity(),
            )

    def test_store_revalidates_artifact_semantics_without_builder(self):
        artifact_path = Path(self.publication["artifact"]["path"])
        content = json.loads(artifact_path.read_text())
        content["dataset"]["metadata"]["schema_version"] = 1
        forged = deepcopy(self.publication)
        forged["binding"]["dataset_sha256"] = object_sha256(content["dataset"])
        scientific = {
            key: forged["binding"].get(key) for key in SCIENTIFIC_BINDING_KEYS
        }
        identity = object_sha256(scientific)
        forged["binding"].update({
            "scientific_binding_sha256": identity,
            "publication_id": f"calibration-{identity}",
            "artifact_id": f"calibration-{identity}-artifact",
        })
        content["scientific_binding"] = scientific
        artifact_path.write_text(
            json.dumps(content, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        )
        forged["binding"]["artifact_sha256"] = file_sha256(artifact_path)
        forged["artifact"].update({
            "artifact_id": forged["binding"]["artifact_id"],
            "sha256": forged["binding"]["artifact_sha256"],
            "size_bytes": artifact_path.stat().st_size,
        })
        with self.assertRaisesRegex(CalibrationBaselineError, "schema_version 2"):
            self.store.publish_calibration(**forged)

    def test_prediction_agent_validates_and_passes_exact_store_binding(self):
        self.store.publish_calibration(**self.publication)
        state = self.store.get_state(self.project["project_id"])
        from agents import prediction

        with (
            patch.object(prediction, "get_storage_backend", return_value=self.store),
            patch.object(
                prediction.CandidateIndex,
                "load",
                return_value=[{"candidate_id": "C1"}],
            ),
            patch.object(prediction, "PredictionPipeline") as pipeline_type,
        ):
            pipeline_type.return_value.run.return_value = {"run_id": "prediction-test"}
            result = prediction.run(
                state=state,
                project_config=self.project,
                artifacts_root=self.root / "artifacts",
                run_root=self.root / "runs",
                run_id="prediction-test",
            )

        self.assertEqual(result["run_id"], "prediction-test")
        self.assertEqual(
            pipeline_type.call_args.kwargs["calibration_binding"],
            self.publication["binding"],
        )

        tampered = deepcopy(state)
        tampered["threshold_calibration_binding"]["protocol_identity"] = {
            "name": "mismatch",
            "version": "0",
        }
        self.store.replace_state(self.project["project_id"], tampered)
        with (
            patch.object(prediction, "get_storage_backend", return_value=self.store),
            self.assertRaises(ContractError) as raised,
        ):
            prediction.run(
                state=tampered,
                project_config=self.project,
                artifacts_root=self.root / "artifacts",
                run_root=self.root / "runs",
            )
        self.assertEqual(raised.exception.code, "calibration_binding_invalid")

    def test_prediction_rejects_superseded_store_binding(self):
        self.store.publish_calibration(**self.publication)
        stale_state = self.store.get_state(self.project["project_id"])
        dataset = simulation_dataset(self.project)
        dataset["controls"][0]["metrics"]["global"]["scrmsd"] = 0.2
        thresholds, audit = calibrate_thresholds(
            controls=dataset,
            thresholds=justified_thresholds(),
            target_ids=("MDM2", "MDMX"),
            protocol=protocol_binding(),
            metric_keys=("L7_scrmsd",),
        )
        replacement = create_calibration_publication(
            dataset=dataset,
            thresholds=thresholds,
            audit=audit,
            project=self.project,
            calibration_authority="simulation_only",
            protocol_identity=protocol_binding(),
            scoring_implementation=scoring_implementation_identity(),
            artifact_path=self.root / "replacement.json",
        )
        self.store.publish_calibration(**replacement)
        from agents import prediction
        with (
            patch.object(prediction, "get_storage_backend", return_value=self.store),
            patch.object(prediction.CandidateIndex, "load", return_value=[{"candidate_id": "C1"}]),
            patch.object(prediction, "PredictionPipeline") as pipeline_type,
        ):
            pipeline_type.return_value.run.return_value = {"run_id": "active"}
            prediction.run(
                state=stale_state,
                project_config=self.project,
                artifacts_root=self.root / "artifacts",
                run_root=self.root / "runs",
            )
        self.assertEqual(
            pipeline_type.call_args.kwargs["calibration_binding"], replacement["binding"]
        )

    def test_insufficient_simulation_controls_do_not_become_a_baseline(self):
        dataset = simulation_dataset(self.project, n_positive=2, n_negative=9)
        thresholds, audit = calibrate_thresholds(
            controls=dataset,
            thresholds=justified_thresholds(),
            target_ids=("MDM2", "MDMX"),
            protocol=protocol_binding(),
            metric_keys=("L7_scrmsd",),
        )
        self.assertEqual(audit["status"], "insufficient_controls")
        with self.assertRaisesRegex(CalibrationBaselineError, "calibrated metric"):
            create_calibration_publication(
                dataset=dataset,
                thresholds=thresholds,
                audit=audit,
                project=self.project,
                calibration_authority="simulation_only",
                protocol_identity=protocol_binding(),
                scoring_implementation=scoring_implementation_identity(),
                artifact_path=self.root / "insufficient.json",
            )


class CalibrationPredictionLifecycleTests(unittest.TestCase):
    def test_simulation_controls_publish_and_reach_prediction_evidence_and_cache(self):
        from agents import prediction
        from test_prediction_pipeline import PredictionPipelineTests

        harness = PredictionPipelineTests()
        harness.setUp()
        self.addCleanup(harness.tearDown)
        reference = harness._register_candidate()
        harness._write_complete_artifacts(reference)
        project = project_config()
        dataset, thresholds, audit = calibrated_simulation(project)
        publication = create_calibration_publication(
            dataset=dataset, thresholds=thresholds, audit=audit, project=project,
            calibration_authority="simulation_only",
            protocol_identity=protocol_binding(),
            scoring_implementation=scoring_implementation_identity(),
            artifact_path=harness.root / "simulation-baseline.json",
        )
        binding = publication["binding"]
        store = SQLiteStore(
            harness.root / "formal-store.db", project_id=project["project_id"]
        )
        store.replace_state(project["project_id"], {
            "project_id": project["project_id"],
            "approved_digest": project["review"]["approved_digest"],
            "project_config": project,
        })
        store.add_candidates(data_layer.CandidateIndex.load())
        store.publish_calibration(**publication)

        def run(*, resume=False):
            import candidate_index
            with (
                patch.object(prediction, "get_storage_backend", return_value=store),
                patch.object(data_layer, "get_storage_backend", return_value=store),
                patch.object(candidate_index, "get_storage_backend", return_value=store),
            ):
                return prediction.run(
                    state=store.get_state(project["project_id"]),
                    project_config=project,
                    artifacts_root=harness.artifacts_root,
                    run_root=harness.run_root,
                    run_id="test_run",
                    resume=resume,
                )

        summary = run()
        run_dir = harness.run_root / "test_run"
        record_path = run_dir / "records" / "C0001.json"
        record = json.loads(record_path.read_text())
        handoff = json.loads((run_dir / "prediction_handoff.json").read_text())
        self.assertEqual(summary["calibration_binding"], binding)
        self.assertEqual(record["calibration_binding"], binding)
        self.assertEqual(record["cache_key"]["calibration_binding"], binding)
        self.assertEqual(handoff["calibration_binding"], binding)
        events = store.query(agent="prediction")
        self.assertTrue(events)
        self.assertTrue(all(event["calibration_binding"] == binding for event in events))
        self.assertEqual(run(resume=True)["cache_hits"], 1)

        record["calibration_binding"]["calibration_authority"] = "approved_real"
        record_path.write_text(json.dumps(record))
        repaired = run(resume=True)
        self.assertEqual(repaired["cache_hits"], 0)
        self.assertEqual(
            json.loads(record_path.read_text())["calibration_binding"], binding
        )


if __name__ == "__main__":
    unittest.main()
