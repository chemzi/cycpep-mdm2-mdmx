"""Focused contract tests for the formal workflow Decision resolver."""

from __future__ import annotations

from copy import deepcopy
import json
import tempfile
import unittest
from pathlib import Path

from storage import SQLiteStore
from test_exploration_decision import (
    PREDICTION_RUN_ID,
    PROJECT_ID,
    WORKFLOW_ID,
    build_decision,
    evidence_batch,
    handoff_row,
)
from workflow.exploration_decision_handoff import (
    ExplorationDecisionHandoffError,
    resolve_exploration_decision_handoff,
)


class ExplorationDecisionHandoffResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="workflow-e3c-resolver-")
        self.root = Path(self.temp.name)
        self.store = SQLiteStore(self.root / "store.db", project_id=PROJECT_ID)
        self.decision = build_decision(evidence_batch()).to_dict()
        self.state = {"project_id": PROJECT_ID, "round": 1}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _report(self, action: str = "iterate_interface_design") -> Path:
        path = self.root / "critic.json"
        path.write_text(
            json.dumps({
                "source": {
                    "project_id": PROJECT_ID,
                    "prediction_run_id": PREDICTION_RUN_ID,
                },
                "recommendations": [{"action": action}],
            }),
            encoding="utf-8",
        )
        return path

    def _publish_prediction_identity(self, **overrides) -> None:
        event = handoff_row(evidence_batch())
        event.update(overrides)
        self.store.append(event)

    def _publish_decision(self, decision: dict | None = None, **overrides) -> None:
        event = deepcopy(self.decision if decision is None else decision)
        event.update({
            "event_id": overrides.pop("event_id", "decision-e3c"),
            "agent": "critic",
            "event_type": "exploration_decision",
            "phase": "critic",
            "round": event["source_round"],
        })
        event.update(overrides)
        self.store.append(event)

    def _resolve(self, *, action: str = "iterate_interface_design"):
        return resolve_exploration_decision_handoff(
            store=self.store,
            critic_report_path=self._report(action),
            project_id=PROJECT_ID,
            state=self.state,
        )

    def test_unique_formal_prediction_identity_and_decision_are_resolved(self):
        self._publish_prediction_identity()
        self._publish_decision()

        handoff = self._resolve()

        self.assertEqual(handoff.workflow_id, WORKFLOW_ID)
        self.assertTrue(handoff.required)
        self.assertEqual(handoff.exploration_decision, self.decision)

    def test_required_decision_missing_fails_closed(self):
        self._publish_prediction_identity()

        with self.assertRaises(ExplorationDecisionHandoffError) as caught:
            self._resolve()

        self.assertEqual(caught.exception.code, "exploration_decision_required")

    def test_prediction_workflow_identity_must_be_unique_and_valid(self):
        cases = {
            "missing": (
                (),
                "prediction_workflow_identity_missing",
            ),
            "ambiguous": (
                ({}, {"event_id": "handoff-e3c-second"}),
                "prediction_workflow_identity_ambiguous",
            ),
            "invalid": (
                ({"workflow_id": "invalid workflow"},),
                "prediction_workflow_identity_invalid",
            ),
        }
        for label, (publications, expected_code) in cases.items():
            with self.subTest(label=label):
                isolated = SQLiteStore(
                    self.root / f"prediction-{label}.db", project_id=PROJECT_ID
                )
                original = self.store
                self.store = isolated
                try:
                    for overrides in publications:
                        self._publish_prediction_identity(**overrides)
                    with self.assertRaises(ExplorationDecisionHandoffError) as caught:
                        self._resolve()
                    self.assertEqual(caught.exception.code, expected_code)
                finally:
                    self.store = original

    def test_matching_decision_must_be_unique_and_contract_valid(self):
        cases = {
            "ambiguous": "exploration_decision_ambiguous",
            "invalid": "exploration_decision_invalid",
        }
        for label, expected_code in cases.items():
            with self.subTest(label=label):
                isolated = SQLiteStore(
                    self.root / f"decision-{label}.db", project_id=PROJECT_ID
                )
                original = self.store
                self.store = isolated
                try:
                    self._publish_prediction_identity()
                    self._publish_decision()
                    if label == "ambiguous":
                        self._publish_decision(event_id="decision-e3c-second")
                    else:
                        invalid = deepcopy(self.decision)
                        invalid["decision_id"] = "exploration_decision_" + "0" * 64
                        # Replace the one valid publication with the malformed one.
                        self.store = SQLiteStore(
                            self.root / "decision-invalid-only.db",
                            project_id=PROJECT_ID,
                        )
                        self._publish_prediction_identity()
                        self._publish_decision(invalid)
                    with self.assertRaises(ExplorationDecisionHandoffError) as caught:
                        self._resolve()
                    self.assertEqual(caught.exception.code, expected_code)
                finally:
                    self.store = original

    def test_non_design_recommendation_is_explicit_no_decision_path(self):
        self._publish_prediction_identity()
        original_state = deepcopy(self.state)

        handoff = self._resolve(action="complete_prediction_evidence")

        self.assertEqual(handoff.workflow_id, WORKFLOW_ID)
        self.assertFalse(handoff.required)
        self.assertIsNone(handoff.exploration_decision)
        self.assertEqual(self.state, original_state)

    def test_only_current_prediction_run_and_source_round_are_selected(self):
        self._publish_prediction_identity()
        unrelated_run = deepcopy(self.decision)
        unrelated_run["prediction_run_id"] = "prediction_other"
        unrelated_round = deepcopy(self.decision)
        unrelated_round["source_round"] = 2
        unrelated_round["applies_to_round"] = 3
        self._publish_decision(unrelated_run, event_id="decision-other-run")
        self._publish_decision(unrelated_round, event_id="decision-other-round")
        self._publish_decision()

        handoff = self._resolve()

        self.assertEqual(handoff.exploration_decision, self.decision)

    def test_out_of_scope_decisions_do_not_satisfy_required_current_scope(self):
        for label, overrides in (
            ("prediction_run", {"prediction_run_id": "prediction_other"}),
            ("source_round", {"source_round": 2, "applies_to_round": 3}),
        ):
            with self.subTest(label=label):
                isolated = SQLiteStore(
                    self.root / f"out-of-scope-{label}.db", project_id=PROJECT_ID
                )
                original = self.store
                self.store = isolated
                try:
                    self._publish_prediction_identity()
                    decision = deepcopy(self.decision)
                    decision.update(overrides)
                    self._publish_decision(decision)
                    with self.assertRaises(ExplorationDecisionHandoffError) as caught:
                        self._resolve()
                    self.assertEqual(
                        caught.exception.code, "exploration_decision_required"
                    )
                finally:
                    self.store = original

    def test_repeated_resolution_is_deterministic_and_returns_detached_mappings(self):
        self._publish_prediction_identity()
        self._publish_decision()

        first = self._resolve()
        first_mapping = first.exploration_decision
        first_mapping["adjustment"]["preferred_lengths"] = [10, 12]
        replay = self._resolve()

        self.assertEqual(first, replay)
        self.assertEqual(replay.exploration_decision, self.decision)


if __name__ == "__main__":
    unittest.main()
