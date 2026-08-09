import unittest

import math

import data_layer
from battery_evaluation import evaluate_battery
from soft_desirability import soft_desirability


def _candidate(**overrides):
    candidate = {
        "candidate_id": "T001",
        "sequence": "GDEETGE",
        "metrics": {
            "global": {
                "plddt": 0.85,
                "nc_distance_pre": 1.4,
                "nc_distance_post": 1.5,
                "scrmsd": 1.8,
            },
            "targets": {
                "KEAP1": {
                    "ipsae": 0.82,
                    "dg": -16.0,
                    "sc": 0.8,
                    "dsasa": 500.0,
                    "hotspot_cov": 0.9,
                    "pose_rmsd": 1.6,
                }
            },
        },
    }
    candidate.update(overrides)
    return candidate


class SoftDesirabilityTests(unittest.TestCase):
    def test_uncalibrated_metric_is_soft_only(self):
        thresholds = {
            "L7_scrmsd": {
                "value": 2.0, "operator": "<=", "source": "same-protocol positive_control calibration",
                "calibration_status": "calibrated",
            },
            "L1_plddt": {
                "value": 0.7, "operator": ">=", "source": "team",
                "evidence_grade": "team_provisional", "calibration_status": "pending",
            },
        }
        view = soft_desirability(_candidate(), thresholds, target_ids=("KEAP1",))
        self.assertTrue(view["metrics"]["L7_scrmsd"]["hard_eligible"])
        self.assertIn("L7_scrmsd", view["hard_eligible_metrics"])
        self.assertFalse(view["metrics"]["L1_plddt"]["hard_eligible"])
        self.assertIn("L1_plddt", view["soft_only_metrics"])
        self.assertEqual(
            view["metrics"]["L1_plddt"]["calibration_status"], "pending"
        )

    def test_desirability_normalizes_closeness_to_gate(self):
        thresholds = {
            "L7_scrmsd": {"value": 2.0, "operator": "<=", "source": "team"},
        }
        close = soft_desirability(
            _candidate(metrics={**_candidate()["metrics"], "global": {
                **_candidate()["metrics"]["global"], "scrmsd": 2.5,
            }}),
            thresholds,
        )["metrics"]["L7_scrmsd"]
        far = soft_desirability(
            _candidate(metrics={**_candidate()["metrics"], "global": {
                **_candidate()["metrics"]["global"], "scrmsd": 4.0,
            }}),
            thresholds,
        )["metrics"]["L7_scrmsd"]
        self.assertLess(far["desirability"], close["desirability"])
        self.assertLessEqual(close["desirability"], 1.0)
        self.assertGreaterEqual(far["desirability"], 0.0)

    def test_soft_view_does_not_change_hard_clearance(self):
        thresholds = {
            "L7_scrmsd": {
                "value": 2.0, "operator": "<=", "source": "team",
                "evidence_grade": "team_provisional", "calibration_status": "pending",
            },
        }
        candidate = _candidate()
        battery = evaluate_battery(candidate, thresholds=thresholds, required_targets=("KEAP1",))
        view = soft_desirability(candidate, thresholds, target_ids=("KEAP1",))
        self.assertFalse(battery["competition_clearance"])
        self.assertIn("L7_scrmsd", view["soft_only_metrics"])
        after = evaluate_battery(candidate, thresholds=thresholds, required_targets=("KEAP1",))
        self.assertEqual(battery, after)


if __name__ == "__main__":
    unittest.main()

    def test_non_finite_values_never_fabricate_scores(self):
        thresholds = {
            "L7_scrmsd": {"value": 2.0, "operator": "<=", "source": "team"},
            "L1_plddt": {
                "value": 0.7, "operator": ">=", "source": "team",
                "evidence_grade": "paper_explicit",
            },
        }
        candidate = _candidate(metrics={**_candidate()["metrics"], "global": {
            **_candidate()["metrics"]["global"],
            "plddt": float("nan"),
            "scrmsd": float("inf"),
        }})
        view = soft_desirability(candidate, thresholds, target_ids=("KEAP1",))
        self.assertIsNone(view["metrics"]["L1_plddt"]["desirability"])
        self.assertIsNone(view["metrics"]["L7_scrmsd"]["desirability"])
        self.assertNotEqual(view["metrics"]["L1_plddt"]["desirability"], 1.0)
        # production entry point via data_layer re-export
        public_view = data_layer.soft_desirability(candidate, thresholds, target_ids=("KEAP1",))
        self.assertIsNone(public_view["metrics"]["L7_scrmsd"]["desirability"])

    def test_non_finite_guard_returns_none(self):
        from soft_desirability import _desirability
        self.assertIsNone(_desirability(float("nan"), 5.0, "maximize"))
        self.assertIsNone(_desirability(float("inf"), 2.0, "minimize"))
        self.assertIsNone(_desirability(float("-inf"), 1.0, "maximize"))
        self.assertTrue(math.isfinite(_desirability(1.0, 2.0, "minimize")))

    def test_target_scoped_metrics_without_target_ids_are_unavailable(self):
        thresholds = {}
        view = soft_desirability(_candidate(), thresholds, target_ids=())
        self.assertIn("L2_ipsae", view["metrics"])
        self.assertIn("L5_hotspot_coverage", view["metrics"])
        self.assertIn("L6_pose_rmsd", view["metrics"])
        self.assertFalse(view["metrics"]["L2_ipsae"]["hard_eligible"])
        self.assertIsNone(view["metrics"]["L2_ipsae"]["desirability"])
        self.assertEqual(view["metrics"]["L2_ipsae"]["reason"], "missing_target_ids")
        self.assertIn("L2_ipsae", view["soft_only_metrics"])

