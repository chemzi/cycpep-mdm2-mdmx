import unittest

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
