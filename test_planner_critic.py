"""
Planner + Critic unit tests (no GPU / bio tools).
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEST_ROOT = Path(tempfile.mkdtemp(prefix="cycpep-planner-critic-"))
DATA_DIR = TEST_ROOT / "data"
EVIDENCE_DIR = TEST_ROOT / "evidence"
DATA_DIR.mkdir(parents=True)
EVIDENCE_DIR.mkdir(parents=True)
os.environ["CYCPEP_DATA_DIR"] = str(DATA_DIR)
os.environ["CYCPEP_EVIDENCE_DIR"] = str(EVIDENCE_DIR)

sys.path.insert(0, str(ROOT))

import data_layer  # noqa: E402
from data_layer import CandidateIndex, State  # noqa: E402
from agents import planner as planner_agent  # noqa: E402
from agents import critic as critic_agent  # noqa: E402
from agents.search_tree import SearchTree, strategy_branch_key  # noqa: E402


class PlannerCriticTests(unittest.TestCase):
    def setUp(self):
        # Reset shared files between tests
        for path in (DATA_DIR / "state.json", DATA_DIR / "candidate_index.csv",
                     DATA_DIR / "search_tree.json"):
            if path.exists():
                path.unlink()
        log = EVIDENCE_DIR / "evidence_log.jsonl"
        if log.exists():
            log.unlink()
        State.save(copy.deepcopy(State._default))

    def test_empty_state_plans_research(self):
        state = State.load()
        # Ensure no research markers
        state.pop("research_pipeline_meta", None)
        state["pocket_differences"] = {}
        state["known_dual_binders"] = []
        State.save(state)
        tasks = planner_agent.plan(state=State.load(), candidates=[])
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["agent"], "research")
        self.assertIn("no research", tasks[0]["reason"])

    def test_research_without_candidates_plans_design(self):
        State.update({
            "research_pipeline_meta": {"run_status": "complete"},
            "pocket_differences": {"MDM2": {}},
            "candidate_count": 0,
        })
        tasks = planner_agent.plan(state=State.load(), candidates=[])
        self.assertEqual(tasks[0]["agent"], "design")
        self.assertTrue(tasks[0].get("needs_gpu"))
        self.assertIn("candidate", tasks[0]["reason"])

    def test_scored_candidates_plan_critic(self):
        State.update({
            "research_pipeline_meta": {"run_status": "complete"},
            "candidate_count": 1,
        })
        candidates = [{
            "candidate_id": "C0001",
            "sequence": "GFEWALAAKCFG",
            "plddt": 0.9,
            "manifest_path": "x.json",
        }]
        tasks = planner_agent.plan(state=State.load(), candidates=candidates)
        self.assertEqual(tasks[0]["agent"], "critic")

    def test_fresh_branch_designs_instead_of_reusing_parent_candidates(self):
        """
        Regression: a non-root node whose scope is empty must ask for design.
        Falling back to the global pool here makes every new branch re-review its
        parent's rows and never generate candidates, which stalls the search.
        """
        State.update({
            "research_pipeline_meta": {"run_status": "complete"},
            "candidate_count": 4,
        })
        parent_pool = [
            {"candidate_id": f"C000{i}", "sequence": "GFEWALAAKCFG",
             "source_batch": "N0001/route_A_mdm2/L12",
             "manifest_path": "m.json", "plddt": 0.9}
            for i in range(1, 5)
        ]
        root = {"node_id": "N0001", "depth": 0, "round": 1, "strategy": {}}
        child = {"node_id": "N0002", "depth": 1, "round": 2, "strategy": {}}

        root_tasks = planner_agent.plan(
            state=State.load(), node=root, candidates=parent_pool)
        self.assertEqual(root_tasks[0]["agent"], "critic")

        child_tasks = planner_agent.plan(
            state=State.load(), node=child, candidates=parent_pool)
        self.assertEqual(child_tasks[0]["agent"], "design")
        self.assertIn("no candidates for this node", child_tasks[0]["reason"])

    def test_root_still_sees_untagged_legacy_pool(self):
        """Root keeps the global fallback so a pre-existing CSV is not ignored."""
        State.update({
            "research_pipeline_meta": {"run_status": "complete"},
            "candidate_count": 1,
        })
        legacy = [{
            "candidate_id": "C9001", "sequence": "AAAAAAAAAAAA",
            "source_batch": "", "manifest_path": "m.json", "plddt": 0.9,
        }]
        root = {"node_id": "N0001", "depth": 0, "round": 1, "strategy": {}}
        tasks = planner_agent.plan(state=State.load(), node=root, candidates=legacy)
        self.assertEqual(tasks[0]["agent"], "critic")

    def test_critic_empty_pool(self):
        report = critic_agent.review(candidates=[], thresholds={}, log_evidence=True)
        self.assertEqual(report["verdict"], "dead_end")
        codes = {i["code"] for i in report["issues"]}
        self.assertIn("empty_candidate_pool", codes)
        self.assertTrue(report["event_id"])

    def test_critic_duplicate_sequences(self):
        cands = [
            {"candidate_id": "C0001", "sequence": "AAAAAAAABBBB", "manifest_path": "a.json",
             "plddt": 0.9},
            {"candidate_id": "C0002", "sequence": "AAAAAAAABBBB", "manifest_path": "b.json",
             "plddt": 0.9},
            {"candidate_id": "C0003", "sequence": "CCCCCCCCDDDD", "manifest_path": "c.json",
             "plddt": 0.9},
        ]
        report = critic_agent.review(
            candidates=cands,
            thresholds={
                "L1_plddt": {
                    "value": 0.8, "operator": ">",
                    "calibration_status": "pending",
                    "evidence_grade": "team_provisional",
                    "source": "test",
                },
            },
            log_evidence=False,
        )
        codes = {i["code"] for i in report["issues"]}
        self.assertIn("duplicate_sequences", codes)

    def test_critic_missing_manifest_and_unscored(self):
        cands = [
            {"candidate_id": "C0001", "sequence": "GFEWALAAKCFG", "manifest_path": ""},
        ]
        report = critic_agent.review(candidates=cands, thresholds={}, log_evidence=False)
        codes = {i["code"] for i in report["issues"]}
        self.assertIn("missing_manifest", codes)
        self.assertIn("unscored_candidates", codes)
        self.assertEqual(report["verdict"], "backtrack")

    def test_critic_threshold_advisory(self):
        cands = [{
            "candidate_id": "C0001",
            "sequence": "GFEWALAAKCFG",
            "manifest_path": "m.json",
            "plddt": 0.9,
        }]
        report = critic_agent.review(
            candidates=cands,
            thresholds={
                "L1_plddt": {
                    "value": 0.8, "operator": ">",
                    "calibration_status": "pending",
                    "evidence_grade": "team_provisional",
                    "source": "test",
                },
            },
            log_evidence=False,
        )
        codes = {i["code"] for i in report["issues"]}
        self.assertIn("threshold_needs_review", codes)

    def test_propose_children_excludes_tried(self):
        parent = {
            "node_id": "N0001",
            "strategy": {
                "route_mix": {"route_A_mdm2": 400, "route_B": 400, "route_C": 200},
                "lengths": [10, 12],
                "constraints": {},
            },
            "tried_branch_keys": [],
        }
        props = planner_agent.propose_children(parent, critic_report={
            "issues": [{"code": "low_diversity"}],
        }, max_proposals=3)
        self.assertTrue(len(props) >= 1)
        keys = {strategy_branch_key(p) for p in props}
        # All unique
        self.assertEqual(len(keys), len(props))
        # Exclude works
        props2 = planner_agent.propose_children(parent, exclude=keys, max_proposals=5)
        for p in props2:
            self.assertNotIn(strategy_branch_key(p), keys)

    def test_adjust_writes_planner_adjust_with_trigger(self):
        tree = SearchTree(path=DATA_DIR / "search_tree.json", beam_width=3, max_nodes=10)
        root = tree.init_root()
        report = {
            "event_id": "evt_test_123",
            "issues": [{"code": "duplicate_sequences", "message": "dup"}],
            "recommendation": "raise diversity",
            "verdict": "backtrack",
        }
        # Backtrack path: mark a dead child then adjust from parent
        child = tree.add_child(root["node_id"], {
            "route_mix": {"route_A_mdm2": 1},
            "lengths": [10],
            "constraints": {"x": 1},
        })
        tree.backtrack(child["node_id"])
        result = planner_agent.adjust(report=report, tree=tree, parent=root)
        self.assertEqual(result["status"], "adjusted")
        self.assertEqual(result["trigger_event_id"], "evt_test_123")
        self.assertTrue(result["child_node_id"])
        log = (EVIDENCE_DIR / "evidence_log.jsonl").read_text(encoding="utf-8")
        self.assertIn("planner_adjust", log)
        self.assertIn("evt_test_123", log)


if __name__ == "__main__":
    unittest.main()
