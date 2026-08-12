"""E2 contract tests: scoped Evidence -> constrained ExplorationDecision."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import shutil
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

import data_layer
import experience
from contracts.exploration_decision import (
    ExplorationDecision,
    ExplorationDecisionContractError,
)
from contracts.trace import TraceContext
from data_layer import EvidenceLogger, get_storage_backend
from experience import (
    LengthPreferencePolicy,
    LengthPreferencePolicyDefinition,
    suggest_length_preference,
)
from exploration import exploration_shortlist
from exploration_decision import (
    EVENT_DECISION,
    build_exploration_decision,
    record_exploration_decision,
)
from target_bootstrap import config_digest
from threshold_contract import canonical_threshold_digest


PROJECT_ID = "mdm2_mdmx_reference"
WORKFLOW_ID = "workflow_e2"
RUN_ID = "orchestrator_e2"
PREDICTION_RUN_ID = "prediction_e2"
TARGETS = ("MDM2", "MDMX")
PROTOCOL = {"name": "prediction", "version": "1", "sha256": "1" * 64}
THRESHOLDS = {
    "L2_ipsae": {"value": 0.7, "operator": ">=", "calibration_status": "pending"}
}
THRESHOLD_DIGEST = canonical_threshold_digest(THRESHOLDS)
PROJECT = {
    "project_id": PROJECT_ID,
    "targets": [
        {"id": "MDM2", "design": {"lengths": [8, 10, 12]}},
        {"id": "MDMX", "design": {"lengths": [8, 10, 12]}},
    ],
    "review": {
        "status": "approved",
        "approved_digest": "a" * 64,
        "content_digest": "a" * 64,
    },
}
PROJECT["review"]["approved_digest"] = config_digest(PROJECT)
PROJECT["review"]["content_digest"] = config_digest(PROJECT)


def decision_battery(candidate_id, length, passed, event_id):
    return {
        "event_id": event_id,
        "event_type": "battery_evaluated",
        "agent": "prediction",
        "phase": "evaluate",
        "project_id": PROJECT_ID,
        "workflow_id": WORKFLOW_ID,
        "run_id": RUN_ID,
        "prediction_run_id": PREDICTION_RUN_ID,
        "candidate_id": candidate_id,
        "targets": list(TARGETS),
        "length": length,
        "route": "route_A",
        "passed": passed,
        "competition_clearance": passed,
        "failed_layers": [] if passed else ["l2_pass"],
        "hard_failures": [],
        "missing_thresholds": [],
        "triage_status": "clear" if passed else "needs_optimization",
        "layer_values": {
            "L2_ipsae_mdm2": 0.9 if passed else 0.2,
            "L2_ipsae_mdmx": 0.9 if passed else 0.2,
        },
        "target_pass": {target: passed for target in TARGETS},
        "protocol_identity": dict(PROTOCOL),
        "thresholds_digest": THRESHOLD_DIGEST,
    }


def evidence_batch(*, sufficient=True):
    count = 5 if sufficient else 4
    rows = []
    for index in range(count):
        rows.append(decision_battery(f"C8{index:02d}", 8, False, f"battery-8-{index}"))
    for index in range(count):
        rows.append(decision_battery(f"C12{index:02d}", 12, sufficient, f"battery-12-{index}"))
    return rows


def shortlist_row(rows, event_id="shortlist-e2"):
    result = exploration_shortlist(
        rows, targets=list(TARGETS), thresholds=THRESHOLDS
    )
    return {
        "event_id": event_id,
        "event_type": "exploration_shortlist",
        "agent": "critic",
        "phase": "critic",
        "project_id": PROJECT_ID,
        "workflow_id": WORKFLOW_ID,
        "run_id": RUN_ID,
        "round": 1,
        "targets": list(TARGETS),
        **result,
    }


def handoff_row(rows, *, event_id="handoff-e2", prediction_run_id=PREDICTION_RUN_ID):
    return {
        "event_id": event_id,
        "event_type": "prediction_handoff_ready",
        "agent": "prediction",
        "phase": "evaluate",
        "project_id": PROJECT_ID,
        "workflow_id": WORKFLOW_ID,
        "run_id": RUN_ID,
        "prediction_run_id": prediction_run_id,
        "targets": list(TARGETS),
        "candidate_ids": sorted(row["candidate_id"] for row in rows),
        "protocol_identity": dict(PROTOCOL),
        "thresholds_digest": THRESHOLD_DIGEST,
        "handoff_artifact_id": "artifact-handoff-e2",
    }


def build_decision(rows, **overrides):
    handoff = overrides.pop("prediction_handoff_event", handoff_row(rows))
    values = {
        "battery_events": rows,
        "shortlist_event": shortlist_row(rows),
        "prediction_handoff_event": handoff,
        "project_config": PROJECT,
        "thresholds": THRESHOLDS,
        "project_id": PROJECT_ID,
        "workflow_id": WORKFLOW_ID,
        "run_id": RUN_ID,
        "target_ids": TARGETS,
        "source_round": 1,
    }
    values.update(overrides)
    with patch("exploration_decision._require_formal_handoff", return_value=handoff):
        return build_exploration_decision(**values)


class ExistingPolicyCharacterizationTests(unittest.TestCase):
    def test_zero_pass_scoped_evidence_still_yields_false_shortlist(self):
        events = [
            {
                "event_id": f"battery-{index}",
                "event_type": "battery_evaluated",
                "candidate_id": f"C{index:04d}",
                "targets": ["MDM2"],
                "passed": False,
                "failed_layers": ["l2_pass"],
                "layer_values": {"L2_ipsae_mdm2": 0.2 + index / 100},
            }
            for index in range(6)
        ]

        result = exploration_shortlist(
            events,
            targets=["MDM2"],
            k=3,
            thresholds={"L2_ipsae": {"value": 0.7, "operator": ">="}},
        )

        self.assertEqual(result["n_passed"], 0)
        self.assertEqual(len(result["shortlist"]), 3)
        self.assertTrue(all(item["passed"] is False for item in result["shortlist"]))

    def test_existing_length_policy_is_five_samples_seventy_thirty(self):
        insufficient = {
            "lengths": {
                "8": {"n": 4, "failed": 4},
                "12": {"n": 5, "failed": 0},
            }
        }
        boundary = {
            "lengths": {
                "8": {"n": 10, "failed": 7},
                "12": {"n": 10, "failed": 3},
            }
        }

        self.assertIsNone(suggest_length_preference(insufficient))
        self.assertEqual(suggest_length_preference(boundary)["lengths"], [12])


class ExplorationDecisionBuilderTests(unittest.TestCase):
    def test_contract_is_deeply_immutable_and_round_trips(self):
        decision = build_decision(evidence_batch(sufficient=False))
        restored = ExplorationDecision.from_dict(decision.to_dict())

        self.assertEqual(restored, decision)
        with self.assertRaises(TypeError):
            decision.adjustment["knob"] = "other"
        with self.assertRaises(TypeError):
            decision.adjustment["baseline_policy_weights"][0]["weight"] = 2

    def test_insufficient_evidence_is_deterministic_no_adjustment(self):
        rows = evidence_batch(sufficient=False)
        shortlist = shortlist_row(rows)
        first = build_decision(rows, shortlist_event=shortlist)
        second = build_decision(list(reversed(rows)), shortlist_event=shortlist)

        self.assertEqual(first.decision_status, "no_adjustment")
        self.assertEqual(first.decision_id, second.decision_id)
        self.assertEqual(
            first.adjustment["baseline_policy_weights"],
            first.adjustment["proposed_policy_weights"],
        )
        self.assertEqual(first.adjustment["preferred_lengths"], ())

    def test_sufficient_evidence_narrows_relative_weights_to_length_twelve(self):
        decision = build_decision(evidence_batch())

        self.assertEqual(decision.decision_status, "adjustment")
        self.assertEqual(
            [item["length"] for item in decision.adjustment["baseline_policy_weights"]],
            [8, 10, 12],
        )
        self.assertEqual(
            [item["length"] for item in decision.adjustment["proposed_policy_weights"]],
            [12],
        )
        self.assertEqual(decision.adjustment["preferred_lengths"], (12,))
        self.assertNotIn("proposal_count", decision.to_dict()["adjustment"])

    def test_source_semantics_change_changes_identity(self):
        rows = evidence_batch()
        first = build_decision(rows)
        changed = deepcopy(rows)
        changed[0]["layer_values"]["L2_ipsae_mdm2"] = 0.21
        second = build_decision(changed, shortlist_event=shortlist_row(changed))

        self.assertNotEqual(first.decision_input_digest, second.decision_input_digest)
        self.assertNotEqual(first.decision_id, second.decision_id)

    def test_builder_never_queries_unrelated_historical_evidence(self):
        with patch(
            "exploration_decision.EvidenceLogger.get_all",
            side_effect=AssertionError("history queried"),
        ):
            decision = build_decision(evidence_batch())
        self.assertEqual(decision.decision_status, "adjustment")

    def test_length_outside_approved_envelope_fails_closed(self):
        rows = evidence_batch()
        rows[0]["length"] = 14
        with self.assertRaisesRegex(
            ExplorationDecisionContractError, "outside policy envelope"
        ):
            build_decision(rows, shortlist_event=shortlist_row(rows))

    def test_stale_project_approval_binding_fails_closed(self):
        project = deepcopy(PROJECT)
        project["targets"][0]["design"]["lengths"].append(14)
        with self.assertRaisesRegex(
            ExplorationDecisionContractError, "not approved"
        ):
            build_decision(evidence_batch(), project_config=project)

    def test_contract_rejects_tampered_output_and_policy_digest(self):
        decision = build_decision(evidence_batch())
        for field in ("reason", "failure_summary", "policy_envelope_digest"):
            payload = decision.to_dict()
            if field == "reason":
                payload[field] = "different reason"
            elif field == "failure_summary":
                payload[field]["n_failed"] = 0
            else:
                payload[field] = "f" * 64
            with self.subTest(field=field):
                with self.assertRaises(ExplorationDecisionContractError):
                    ExplorationDecision.from_dict(payload)

    def test_contract_rejects_unsupported_adjustment_and_extra_knob(self):
        decision = build_decision(evidence_batch())
        unsupported = decision.to_dict()
        unsupported["adjustment"]["proposed_policy_weights"] = [
            {"length": 10, "weight": 1}
        ]
        unsupported["adjustment"]["preferred_lengths"] = [10]
        with self.assertRaisesRegex(
            ExplorationDecisionContractError, "lacks conservative support|source-derived"
        ):
            ExplorationDecision.from_dict(unsupported)

        extra = decision.to_dict()
        extra["adjustment"]["planner_budget"] = 99
        with self.assertRaisesRegex(
            ExplorationDecisionContractError, "unsupported fields"
        ):
            ExplorationDecision.from_dict(extra)

    def test_contract_rejects_noncanonical_relative_weights(self):
        decision = build_decision(evidence_batch())
        for field in ("baseline_policy_weights", "proposed_policy_weights"):
            payload = decision.to_dict()
            payload["adjustment"][field][0]["weight"] = 99
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    ExplorationDecisionContractError, "canonical relative weight 1"
                ):
                    ExplorationDecision.from_dict(payload)

    def test_candidate_set_missing_extra_duplicate_and_run_mismatch_fail_closed(self):
        rows = evidence_batch()
        cases = []
        cases.append((rows[:-1], handoff_row(rows)))
        extra = deepcopy(rows)
        extra.append(decision_battery("C999", 12, True, "battery-extra"))
        cases.append((extra, handoff_row(rows)))
        duplicate = deepcopy(rows)
        duplicate[-1]["candidate_id"] = duplicate[0]["candidate_id"]
        cases.append((duplicate, handoff_row(rows)))
        wrong_run = deepcopy(rows)
        wrong_run[0]["prediction_run_id"] = "prediction_other"
        cases.append((wrong_run, handoff_row(wrong_run)))

        for scoped_rows, handoff in cases:
            with self.subTest(size=len(scoped_rows)):
                with self.assertRaises(ExplorationDecisionContractError):
                    build_decision(
                        scoped_rows,
                        shortlist_event=shortlist_row(scoped_rows),
                        prediction_handoff_event=handoff,
                    )

    def test_fake_or_cross_run_handoff_fails_closed(self):
        rows = evidence_batch()
        cross_run = handoff_row(rows, prediction_run_id="prediction_other")
        with self.assertRaisesRegex(
            ExplorationDecisionContractError, "prediction_run_id mismatch"
        ):
            build_decision(rows, prediction_handoff_event=cross_run)

    def test_threshold_mismatch_fails_and_authoritative_identity_matches(self):
        rows = evidence_batch()
        decision = build_decision(rows)
        self.assertEqual(decision.threshold_digest, THRESHOLD_DIGEST)

        changed = deepcopy(THRESHOLDS)
        changed["L2_ipsae"]["value"] = 0.8
        with self.assertRaisesRegex(
            ExplorationDecisionContractError, "differs from Prediction authority"
        ):
            build_decision(rows, thresholds=changed)

    def test_wrong_source_owner_or_phase_fails_closed(self):
        rows = evidence_batch()
        cases = []
        wrong_battery = deepcopy(rows)
        wrong_battery[0]["phase"] = "critic"
        cases.append((wrong_battery, shortlist_row(wrong_battery), handoff_row(wrong_battery)))
        wrong_battery_owner = deepcopy(rows)
        wrong_battery_owner[0]["agent"] = "critic"
        cases.append((
            wrong_battery_owner,
            shortlist_row(wrong_battery_owner),
            handoff_row(wrong_battery_owner),
        ))
        wrong_shortlist = shortlist_row(rows)
        wrong_shortlist["agent"] = "prediction"
        cases.append((rows, wrong_shortlist, handoff_row(rows)))
        wrong_shortlist_phase = shortlist_row(rows)
        wrong_shortlist_phase["phase"] = "evaluate"
        cases.append((rows, wrong_shortlist_phase, handoff_row(rows)))
        wrong_handoff = handoff_row(rows)
        wrong_handoff["phase"] = "critic"
        cases.append((rows, shortlist_row(rows), wrong_handoff))
        wrong_handoff_owner = handoff_row(rows)
        wrong_handoff_owner["agent"] = "critic"
        cases.append((rows, shortlist_row(rows), wrong_handoff_owner))
        for source_rows, shortlist, handoff in cases:
            with self.subTest(event_type=handoff["event_type"]):
                with self.assertRaises(ExplorationDecisionContractError):
                    build_decision(
                        source_rows,
                        shortlist_event=shortlist,
                        prediction_handoff_event=handoff,
                    )

    def test_policy_identity_changes_decision_digest(self):
        rows = evidence_batch()
        first = build_decision(rows)
        revised = LengthPreferencePolicy(version="2")
        registry = MappingProxyType({
            (experience.LENGTH_PREFERENCE_POLICY_V1.name,
             experience.LENGTH_PREFERENCE_POLICY_V1.version):
                experience.LENGTH_PREFERENCE_POLICY_DEFINITIONS[
                    (experience.LENGTH_PREFERENCE_POLICY_V1.name,
                     experience.LENGTH_PREFERENCE_POLICY_V1.version)
                ],
            (revised.name, revised.version): LengthPreferencePolicyDefinition(
                revised, experience._suggest_length_preference_v1
            ),
        })
        with patch("experience.LENGTH_PREFERENCE_POLICY_DEFINITIONS", registry), patch(
            "experience.LENGTH_PREFERENCE_POLICY", revised
        ), patch(
            "exploration_decision.LENGTH_PREFERENCE_POLICY", revised
        ):
            second = build_decision(rows)
        self.assertNotEqual(first.decision_input_digest, second.decision_input_digest)

    def test_builder_preserves_inputs_and_calls_no_downstream_agent(self):
        from execution.action_registry import ACTION_REGISTRY

        rows = evidence_batch()
        shortlist = shortlist_row(rows)
        thresholds = deepcopy(THRESHOLDS)
        before = deepcopy((rows, shortlist, thresholds))
        registered_before = dict(ACTION_REGISTRY)
        with patch("agents.design.design_rfpeptides", side_effect=AssertionError), patch(
            "agents.planner.run", side_effect=AssertionError
        ), patch("agents.orchestrator.service.initialize", side_effect=AssertionError), patch(
            "execution.worker.execute_task", side_effect=AssertionError
        ):
            build_decision(rows, shortlist_event=shortlist, thresholds=thresholds)

        self.assertEqual((rows, shortlist, thresholds), before)
        self.assertEqual(ACTION_REGISTRY, registered_before)


class ExplorationDecisionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="exploration-decision-test-"))
        self.original_paths = (
            data_layer.DATA_DIR,
            data_layer.EVIDENCE_DIR,
            data_layer.STATE_PATH,
            data_layer.LOG_PATH,
            data_layer.INDEX_PATH,
        )
        data_layer.DATA_DIR = self.root / "data"
        data_layer.EVIDENCE_DIR = self.root / "evidence"
        data_layer.STATE_PATH = data_layer.DATA_DIR / "state.json"
        data_layer.LOG_PATH = data_layer.EVIDENCE_DIR / "evidence_log.jsonl"
        data_layer.INDEX_PATH = data_layer.DATA_DIR / "candidate_index.csv"

    def tearDown(self):
        (
            data_layer.DATA_DIR,
            data_layer.EVIDENCE_DIR,
            data_layer.STATE_PATH,
            data_layer.LOG_PATH,
            data_layer.INDEX_PATH,
        ) = self.original_paths
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _trace():
        return TraceContext(PROJECT_ID, WORKFLOW_ID, RUN_ID)

    def _formal_decision(self):
        rows = []
        for source in evidence_batch():
            payload = {
                key: deepcopy(value)
                for key, value in source.items()
                if key not in {
                    "event_id", "event_type", "project_id", "workflow_id",
                    "run_id", "targets",
                }
            }
            event_id = EvidenceLogger.log(
                "prediction",
                "battery_evaluated",
                payload,
                targets=list(TARGETS),
                phase="evaluate",
                trace_context=self._trace(),
            )
            rows.append(next(row for row in EvidenceLogger.get_all() if row["event_id"] == event_id))
        result = exploration_shortlist(rows, targets=list(TARGETS), thresholds=THRESHOLDS)
        shortlist_id = EvidenceLogger.log(
            "critic",
            "exploration_shortlist",
            result,
            targets=list(TARGETS),
            phase="critic",
            round_num=1,
            trace_context=self._trace(),
        )
        shortlist = next(
            row for row in EvidenceLogger.get_all() if row["event_id"] == shortlist_id
        )
        handoff_id = EvidenceLogger.log(
            "prediction",
            "prediction_handoff_ready",
            {
                "prediction_run_id": PREDICTION_RUN_ID,
                "candidate_ids": sorted(row["candidate_id"] for row in rows),
                "protocol_identity": dict(PROTOCOL),
                "thresholds_digest": THRESHOLD_DIGEST,
                "handoff_artifact_id": "artifact-handoff-formal",
            },
            targets=list(TARGETS),
            phase="evaluate",
            trace_context=self._trace(),
        )
        handoff = next(
            row for row in EvidenceLogger.get_all() if row["event_id"] == handoff_id
        )
        decision = build_exploration_decision(
            battery_events=rows,
            shortlist_event=shortlist,
            prediction_handoff_event=handoff,
            project_config=PROJECT,
            thresholds=THRESHOLDS,
            project_id=PROJECT_ID,
            workflow_id=WORKFLOW_ID,
            run_id=RUN_ID,
            target_ids=TARGETS,
            source_round=1,
        )
        return decision

    def test_formal_evidence_round_trip_and_sequential_idempotency(self):
        decision = self._formal_decision()
        with patch.object(data_layer.State, "save", side_effect=AssertionError), patch.object(
            data_layer.State, "update", side_effect=AssertionError
        ), patch.object(
            data_layer.CandidateIndex, "update_score", side_effect=AssertionError
        ), patch.object(
            data_layer.CandidateIndex, "add", side_effect=AssertionError
        ):
            first_id = record_exploration_decision(decision)
            second_id = record_exploration_decision(decision)
        rows = [
            row for row in EvidenceLogger.get_all()
            if row.get("event_type") == EVENT_DECISION
        ]

        self.assertEqual(first_id, second_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(ExplorationDecision.from_dict(rows[0]), decision)
        formal_ids = {row["event_id"] for row in EvidenceLogger.get_all()}
        self.assertTrue(set(decision.source_event_ids).issubset(formal_ids))
        self.assertIn(decision.shortlist_event_id, formal_ids)

    def test_v1_restore_and_retry_remain_valid_after_v2_default(self):
        decision = self._formal_decision()
        event_id = record_exploration_decision(decision)
        revised = LengthPreferencePolicy(version="2")
        def v2_algorithm(_summary, _minimum, _policy):
            return None

        registry = MappingProxyType({
            (experience.LENGTH_PREFERENCE_POLICY_V1.name,
             experience.LENGTH_PREFERENCE_POLICY_V1.version):
                experience.LENGTH_PREFERENCE_POLICY_DEFINITIONS[
                    (experience.LENGTH_PREFERENCE_POLICY_V1.name,
                     experience.LENGTH_PREFERENCE_POLICY_V1.version)
                ],
            (revised.name, revised.version): LengthPreferencePolicyDefinition(
                revised, v2_algorithm
            ),
        })
        with patch("experience.LENGTH_PREFERENCE_POLICY_DEFINITIONS", registry), patch(
            "experience.LENGTH_PREFERENCE_POLICY", revised
        ), patch("exploration_decision.LENGTH_PREFERENCE_POLICY", revised):
            restored = ExplorationDecision.from_dict(decision.to_dict())
            self.assertEqual(record_exploration_decision(restored), event_id)
            v2 = build_exploration_decision(
                battery_events=decision.to_dict()["evidence_support"]["source_evidence"],
                shortlist_event=decision.to_dict()["evidence_support"]["shortlist_evidence"],
                prediction_handoff_event=decision.to_dict()["evidence_support"]["prediction_handoff_evidence"],
                project_config=PROJECT,
                thresholds=THRESHOLDS,
                project_id=PROJECT_ID,
                workflow_id=WORKFLOW_ID,
                run_id=RUN_ID,
                target_ids=TARGETS,
                source_round=1,
            )
            self.assertEqual(v2.decision_status, "no_adjustment")
        self.assertNotEqual(v2.decision_input_digest, decision.decision_input_digest)

    def test_post_build_owner_phase_tamper_fails_formal_append(self):
        decision = self._formal_decision()
        rows = EvidenceLogger.get_all()
        tampered = deepcopy(rows)
        source_id = decision.source_event_ids[0]
        next(row for row in tampered if row["event_id"] == source_id)["phase"] = "critic"
        with patch.object(EvidenceLogger, "get_all", return_value=tampered):
            with self.assertRaisesRegex(
                ExplorationDecisionContractError, "formal source Evidence mismatch"
            ):
                record_exploration_decision(decision)

    def test_nonexistent_formal_handoff_fails_before_decision(self):
        rows = evidence_batch()
        with self.assertRaisesRegex(
            ExplorationDecisionContractError, "formal Prediction handoff Evidence mismatch"
        ):
            build_exploration_decision(
                battery_events=rows,
                shortlist_event=shortlist_row(rows),
                prediction_handoff_event=handoff_row(rows),
                project_config=PROJECT,
                thresholds=THRESHOLDS,
                project_id=PROJECT_ID,
                workflow_id=WORKFLOW_ID,
                run_id=RUN_ID,
                target_ids=TARGETS,
                source_round=1,
            )

    def test_same_decision_id_with_different_payload_fails_closed(self):
        decision = self._formal_decision()
        conflicting = decision.to_dict()
        conflicting.pop("reason")
        # Simulate a corrupt legacy/low-level row. Supported EvidenceLogger
        # writes reject this payload before it reaches the Store.
        get_storage_backend().append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_id": "legacy-conflict",
            "agent": "critic",
            "event_type": EVENT_DECISION,
            "phase": "critic",
            "round": 1,
            "targets": list(TARGETS),
            "project_id": PROJECT_ID,
            "workflow_id": WORKFLOW_ID,
            "run_id": RUN_ID,
            **conflicting,
        })
        before = len(EvidenceLogger.get_all())

        with self.assertRaisesRegex(
            ExplorationDecisionContractError, "invalid formal payload"
        ):
            record_exploration_decision(decision)
        self.assertEqual(len(EvidenceLogger.get_all()), before)

    def test_generic_supported_writer_rejects_invalid_decision_payload(self):
        before = len(EvidenceLogger.get_all())
        with self.assertRaisesRegex(ValueError, "dedicated source-validating writer"):
            EvidenceLogger.log(
                "critic",
                EVENT_DECISION,
                {"decision_id": "not-a-decision"},
                targets=list(TARGETS),
                phase="critic",
                round_num=1,
                trace_context=self._trace(),
            )
        self.assertEqual(len(EvidenceLogger.get_all()), before)

    def test_generic_append_cannot_bypass_missing_formal_sources(self):
        decision = build_decision(evidence_batch())
        before = len(EvidenceLogger.get_all())
        with self.assertRaisesRegex(ValueError, "dedicated source-validating writer"):
            EvidenceLogger.log(
                "critic",
                EVENT_DECISION,
                decision.to_dict(),
                targets=list(TARGETS),
                phase="critic",
                round_num=1,
                trace_context=self._trace(),
            )
        self.assertEqual(len(EvidenceLogger.get_all()), before)

    def test_missing_source_or_store_failure_creates_no_decision_claim(self):
        pure = build_decision(evidence_batch())
        with self.assertRaisesRegex(
            ExplorationDecisionContractError, "formal source Evidence mismatch"
        ):
            record_exploration_decision(pure)
        self.assertFalse(any(
            row.get("event_type") == EVENT_DECISION for row in EvidenceLogger.get_all()
        ))

        decision = self._formal_decision()
        with patch.object(EvidenceLogger, "_write", side_effect=RuntimeError("store down")):
            with self.assertRaisesRegex(RuntimeError, "store down"):
                record_exploration_decision(decision)
        self.assertFalse(any(
            row.get("event_type") == EVENT_DECISION for row in EvidenceLogger.get_all()
        ))


if __name__ == "__main__":
    unittest.main()
