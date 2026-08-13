"""Focused regressions for frozen ExplorationDecision job materialization."""

from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from agents.planner.errors import PlannerContractError
from agents.planner.task_builder import _materialize_design_jobs


def _state(*, lengths_by_target: dict[str, list[int] | None]) -> dict:
    targets = []
    for target_id, lengths in lengths_by_target.items():
        design = {} if lengths is None else {"lengths": list(lengths)}
        targets.append({"id": target_id, "required": True, "design": design})
    return {
        "round": 3,
        "project_config": {
            "project_id": "decision_materialization_test",
            "protocol": {"name": "approved", "version": "1"},
            "thresholds": {"l4_pass": 2.5},
            "targets": targets,
        },
    }


def _decision(
    *,
    target_ids: list[str],
    status: str = "adjustment",
    proposed_lengths: list[int] | None = None,
    baseline_lengths: list[int] | None = None,
) -> dict:
    baseline = list(baseline_lengths or [8, 10, 12])
    proposed = list(proposed_lengths or ([12] if status == "adjustment" else baseline))
    return {
        "decision_status": status,
        "target_ids": list(target_ids),
        "adjustment": {
            "knob": "peptide_length_policy_weights",
            "baseline_policy_weights": [
                {"length": length, "weight": 1} for length in baseline
            ],
            "proposed_policy_weights": [
                {"length": length, "weight": 1} for length in proposed
            ],
            "preferred_lengths": proposed if status == "adjustment" else [],
        },
    }


def _jobs(state: dict) -> list[dict]:
    return _materialize_design_jobs(
        state=state,
        required_targets=["MDM2", "MDMX"],
        budgets={"route_A_mdm2": 2, "route_A_mdmx": 2},
        requested=3,
        seed_material="stable-seed-material",
    )


class PlannerDecisionMaterializationTests(unittest.TestCase):
    def test_no_decision_preserves_explicit_lengths_and_fixed_fallback_deterministically(self):
        state = _state(lengths_by_target={"MDM2": [12, 8, 12], "MDMX": None})
        original_state = copy.deepcopy(state)

        first = _jobs(state)
        second = _jobs(state)

        self.assertEqual([job["lengths"] for job in first], [[8, 12], [8, 10, 12]])
        self.assertEqual(first, second)
        self.assertEqual(state, original_state)

    def test_adjustment_narrows_lengths_without_changing_job_policy(self):
        baseline_state = _state(
            lengths_by_target={"MDM2": [8, 10, 12], "MDMX": [8, 10, 12]}
        )
        adjusted_state = copy.deepcopy(baseline_state)
        adjusted_state["_frozen_exploration_decision"] = _decision(
            target_ids=["MDM2", "MDMX"]
        )
        original_adjusted_state = copy.deepcopy(adjusted_state)

        baseline = _jobs(baseline_state)
        adjusted = _jobs(adjusted_state)
        repeated = _jobs(adjusted_state)

        self.assertEqual([job["lengths"] for job in adjusted], [[12], [12]])
        self.assertEqual(
            [{key: value for key, value in job.items() if key != "lengths"} for job in adjusted],
            [{key: value for key, value in job.items() if key != "lengths"} for job in baseline],
        )
        self.assertEqual(adjusted, repeated)
        self.assertEqual(adjusted_state, original_adjusted_state)

    def test_adjustment_outside_approved_envelope_fails_closed(self):
        state = _state(lengths_by_target={"MDM2": [8, 10, 12], "MDMX": [8, 10, 12]})
        state["_frozen_exploration_decision"] = _decision(
            target_ids=["MDM2", "MDMX"], proposed_lengths=[14]
        )

        with self.assertRaisesRegex(PlannerContractError, "approved length envelope"):
            _jobs(state)

    def test_decision_target_mismatch_fails_closed(self):
        state = _state(lengths_by_target={"MDM2": [8, 10, 12], "MDMX": [8, 10, 12]})
        state["_frozen_exploration_decision"] = _decision(target_ids=["MDM2"])

        with self.assertRaisesRegex(PlannerContractError, "target scope"):
            _jobs(state)

    def test_zero_request_returns_empty_before_decision_scope_validation(self):
        state = _state(lengths_by_target={"MDM2": [8, 10, 12]})
        state["_frozen_exploration_decision"] = _decision(target_ids=["MDMX"])

        jobs = _materialize_design_jobs(
            state=state,
            required_targets=["MDM2"],
            budgets={"route_A_mdm2": 2},
            requested=0,
            seed_material="stable-seed-material",
        )

        self.assertEqual(jobs, [])

    def test_empty_targets_returns_empty_before_decision_scope_validation(self):
        state = _state(lengths_by_target={})
        state["_frozen_exploration_decision"] = _decision(target_ids=["MDM2"])

        jobs = _materialize_design_jobs(
            state=state,
            required_targets=[],
            budgets={},
            requested=3,
            seed_material="stable-seed-material",
        )

        self.assertEqual(jobs, [])

    def test_no_adjustment_preserves_each_targets_approved_lengths(self):
        state = _state(
            lengths_by_target={"MDM2": [8, 10, 12], "MDMX": [10, 12]}
        )
        state["_frozen_exploration_decision"] = _decision(
            target_ids=["MDM2", "MDMX"],
            status="no_adjustment",
            proposed_lengths=[10, 12],
            baseline_lengths=[10, 12],
        )

        self.assertEqual(
            [job["lengths"] for job in _jobs(state)],
            [[8, 10, 12], [10, 12]],
        )

    def test_missing_lengths_use_fallback_without_ambient_experience_access(self):
        state = _state(lengths_by_target={"MDM2": None, "MDMX": None})

        with patch(
            "experience.consume_experience_preference",
            side_effect=AssertionError("ambient experience read"),
        ) as consume, patch(
            "experience.record_applied_preference",
            side_effect=AssertionError("ambient experience write"),
        ) as record, patch(
            "data_layer.EvidenceLogger.get_all",
            side_effect=AssertionError("ambient Evidence read"),
        ) as evidence:
            jobs = _jobs(state)

        self.assertEqual([job["lengths"] for job in jobs], [[8, 10, 12], [8, 10, 12]])
        consume.assert_not_called()
        record.assert_not_called()
        evidence.assert_not_called()

if __name__ == "__main__":
    unittest.main()
