"""
Orchestrator integration tests (dry-run, no GPU / bio tools).

These lock in the control-flow fixes from the PR review:
  P1-1  a fresh child branch never reviews its parent's candidates
  P2-3  a max_nodes=1 run does not inflate state.round or re-review the root
  P2-5  legacy untagged root candidates get scored (no Prediction loop)
  P2-6  --resume after a pass does not re-run the Critic

Second review round (agent-classified recovery + honest statuses):
  only zero-output Design backtracks and spawns a sibling
  zero-scoring Prediction / stalled Research block instead of mutating Design
  exhausted / no-untried adjust() returns an honest search-level status
  synthetic upstream failures record termination_reason, not a faked verdict
  State.round is projected onto the spawned child's tree round
  a persisted terminal dead-end is not re-executed on resume
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
TEST_ROOT = Path(tempfile.mkdtemp(prefix="cycpep-orchestrator-"))
DATA_DIR = TEST_ROOT / "data"
EVIDENCE_DIR = TEST_ROOT / "evidence"
DATA_DIR.mkdir(parents=True)
EVIDENCE_DIR.mkdir(parents=True)
os.environ["CYCPEP_DATA_DIR"] = str(DATA_DIR)
os.environ["CYCPEP_EVIDENCE_DIR"] = str(EVIDENCE_DIR)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from data_layer import CandidateIndex, State  # noqa: E402
from agents.search_tree import SearchTree  # noqa: E402
from agents import planner as planner_agent  # noqa: E402
import run_pipeline  # noqa: E402


def _reset():
    for path in (DATA_DIR / "state.json", DATA_DIR / "candidate_index.csv",
                 DATA_DIR / "search_tree.json"):
        if path.exists():
            path.unlink()
    log = EVIDENCE_DIR / "evidence_log.jsonl"
    if log.exists():
        log.unlink()
    State.save(copy.deepcopy(State._default))


def _evidence_entries():
    log = EVIDENCE_DIR / "evidence_log.jsonl"
    if not log.exists():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _critic_reviews():
    return [e for e in _evidence_entries() if e.get("event_type") == "critic_review"]


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_dry_run_converges_and_each_node_reviews_only_own_candidates(self):
        result = run_pipeline.run_pipeline(
            dry_run=True, max_nodes=6, beam_width=2, max_steps=12,
        )
        best = result["best_node"]
        self.assertIsNotNone(best)
        self.assertEqual(best["status"], "passed")

        reviews = _critic_reviews()
        self.assertTrue(reviews, "expected at least one critic_review")
        # P1-1: mock_design registers exactly 4 candidates per node, so every
        # per-node review must see exactly 4. A larger count would mean the node
        # reviewed the global pool (its parent's rows leaking in).
        for r in reviews:
            self.assertEqual(
                r["metrics_snapshot"]["total_candidates"], 4,
                f"node review saw {r['metrics_snapshot']['total_candidates']} "
                "candidates; expected only its own 4",
            )

    def test_max_nodes_one_does_not_inflate_round_or_repeat(self):
        run_pipeline.run_pipeline(
            dry_run=True, max_nodes=1, beam_width=2, max_steps=3,
        )
        # P2-3: budget is spent after the root, so the single backtrack cannot
        # spawn a child. Round must stay 1 and no planner_adjust may be logged.
        self.assertEqual(int(State.load().get("round") or 1), 1)
        self.assertEqual(len(_critic_reviews()), 1)
        entries = _evidence_entries()
        self.assertEqual(
            [e for e in entries if e.get("event_type") == "planner_adjust"], [],
        )

    def test_resume_after_pass_does_not_rerun_critic(self):
        run_pipeline.run_pipeline(
            dry_run=True, max_nodes=6, beam_width=2, max_steps=12,
        )
        before = len(_critic_reviews())
        self.assertGreater(before, 0)

        result = run_pipeline.run_pipeline(
            dry_run=True, max_nodes=6, beam_width=2, max_steps=12, resume=True,
        )
        # P2-6: the resumed tree is already finished; no extra step, no new review.
        self.assertEqual(result["steps"], 0)
        self.assertEqual(len(_critic_reviews()), before)
        self.assertEqual(result["best_node"]["status"], "passed")

    def test_legacy_untagged_root_candidate_gets_scored(self):
        """P2-5: mock_predict scores exactly the ids the Planner scoped in,
        including a legacy root candidate that carries no source_batch tag."""
        State.update({
            "research_pipeline_meta": {"run_status": "complete"},
            "candidate_count": 1,
            "thresholds": {},
        })
        CandidateIndex.add({
            "candidate_id": "C9001",
            "sequence": "GFEWALAAKCFG",
            "source_batch": "",  # legacy: no node tag
            "manifest_path": "m.json",
        })
        tree = SearchTree(path=DATA_DIR / "search_tree.json", beam_width=2, max_nodes=6)
        root = tree.init_root()

        tasks = planner_agent.plan(
            state=State.load(), tree=tree, node=root, candidates=CandidateIndex.load()
        )
        self.assertEqual(tasks[0]["agent"], "prediction")
        self.assertIn("C9001", tasks[0]["candidate_ids"])

        run_pipeline.mock_predict(State.load(), root, tasks[0])
        row = next(c for c in CandidateIndex.load() if c["candidate_id"] == "C9001")
        self.assertNotIn(row.get("plddt"), (None, ""))

        # And the Planner now moves this node on to the Critic instead of looping.
        follow = planner_agent.plan(
            state=State.load(), tree=tree, node=root, candidates=CandidateIndex.load()
        )
        self.assertEqual(follow[0]["agent"], "critic")

    def test_zero_output_child_backtracks_to_sibling(self):
        """
        Regression: Design can return success while generating zero candidates.
        The failed child must backtrack and create an untried sibling, rather
        than clearing active/frontier and terminating the entire search.
        """
        State.update({
            "research_pipeline_meta": {"run_status": "complete"},
            "candidate_count": 0,
        })
        tree = SearchTree(
            path=DATA_DIR / "search_tree.json", beam_width=2, max_nodes=6
        )
        root = tree.init_root()
        first_strategy = planner_agent.propose_children(
            root, max_proposals=1
        )[0]
        child = tree.add_child(root["node_id"], first_strategy)

        with patch.object(
            run_pipeline,
            "mock_design",
            return_value={"status": "ok", "candidate_ids": []},
        ):
            result = run_pipeline.run_once_node(tree, dry_run=True)

        self.assertEqual(result["status"], "backtracked_and_rebranched")
        dead = tree.get(child["node_id"])
        self.assertEqual(dead["status"], "dead_end")
        # Synthetic upstream failure must NOT fake a Critic verdict; it records a
        # termination reason and the agent that stalled instead.
        self.assertIsNone(dead.get("critic_verdict"))
        self.assertEqual(dead.get("termination_reason"), "design_no_output")
        self.assertEqual(dead.get("failure_source"), "design")

        sibling_id = result["adjust"]["child_node_id"]
        self.assertIsNotNone(sibling_id)
        self.assertNotEqual(sibling_id, child["node_id"])
        self.assertEqual(tree.get(sibling_id)["parent_id"], root["node_id"])
        self.assertEqual(tree.active_id, sibling_id)
        self.assertEqual(tree.select_active()["node_id"], sibling_id)

        entries = _evidence_entries()
        stalled = [
            e for e in entries
            if e.get("event_type") == "error"
            and e.get("error_type") == "upstream_no_progress"
        ]
        self.assertEqual(len(stalled), 1)
        self.assertEqual(stalled[0]["phase"], "design")
        self.assertEqual(stalled[0]["failure_scope"], "branch")
        adjustments = [
            e for e in entries if e.get("event_type") == "planner_adjust"
        ]
        self.assertEqual(len(adjustments), 1)
        self.assertEqual(
            adjustments[0]["trigger_event_id"], stalled[0]["event_id"]
        )

    def test_root_zero_output_design_spawns_child(self):
        """Root Design zero-output with budget left → rebranch a root child,
        keep the root as an evaluated branching point (no faked Critic verdict)."""
        State.update({
            "research_pipeline_meta": {"run_status": "complete"},
            "candidate_count": 0,
        })
        tree = SearchTree(path=DATA_DIR / "search_tree.json", beam_width=2, max_nodes=6)
        root = tree.init_root()

        with patch.object(
            run_pipeline, "mock_design",
            return_value={"status": "ok", "candidate_ids": []},
        ):
            result = run_pipeline.run_once_node(tree, dry_run=True)

        self.assertEqual(result["status"], "rebranched_root")
        child_id = result["adjust"]["child_node_id"]
        self.assertIsNotNone(child_id)
        self.assertEqual(tree.get(child_id)["parent_id"], root["node_id"])
        self.assertEqual(tree.get(root["node_id"])["status"], "evaluated")
        self.assertIsNone(tree.get(root["node_id"]).get("critic_verdict"))
        # State.round tracks the newly created node, not a blind +1.
        self.assertEqual(int(State.load()["round"]), int(tree.get(child_id)["round"]))

    def test_root_zero_output_design_budget_one_is_exhausted(self):
        """Root Design zero-output but max_nodes=1 → honest search_budget_exhausted,
        no sibling, round untouched, no planner_adjust."""
        State.update({
            "research_pipeline_meta": {"run_status": "complete"},
            "candidate_count": 0,
        })
        tree = SearchTree(path=DATA_DIR / "search_tree.json", beam_width=2, max_nodes=1)
        root = tree.init_root()

        with patch.object(
            run_pipeline, "mock_design",
            return_value={"status": "ok", "candidate_ids": []},
        ):
            result = run_pipeline.run_once_node(tree, dry_run=True)

        self.assertEqual(result["status"], "search_budget_exhausted")
        self.assertEqual(tree.get(root["node_id"])["status"], "dead_end")
        self.assertIsNone(tree.get(root["node_id"]).get("critic_verdict"))
        self.assertEqual(tree.node_count(), 1)
        self.assertEqual(int(State.load().get("round") or 1), 1)
        adjustments = [
            e for e in _evidence_entries() if e.get("event_type") == "planner_adjust"
        ]
        self.assertEqual(adjustments, [])

    def test_non_root_zero_output_no_untried_branch_is_search_exhausted(self):
        """Non-root Design zero-output where the parent has no untried strategy
        → search_exhausted; do not claim a rebranch."""
        State.update({
            "research_pipeline_meta": {"run_status": "complete"},
            "candidate_count": 0,
        })
        tree = SearchTree(path=DATA_DIR / "search_tree.json", beam_width=2, max_nodes=6)
        root = tree.init_root()
        strat = planner_agent.propose_children(root, max_proposals=1)[0]
        child = tree.add_child(root["node_id"], strat)

        with patch.object(
            run_pipeline, "mock_design",
            return_value={"status": "ok", "candidate_ids": []},
        ), patch.object(planner_agent, "propose_children", return_value=[]):
            result = run_pipeline.run_once_node(tree, dry_run=True)

        self.assertEqual(result["status"], "search_exhausted")
        self.assertEqual(tree.get(child["node_id"])["status"], "dead_end")
        self.assertEqual(tree.get(root["node_id"])["status"], "dead_end")
        self.assertIsNone(tree.select_active())

    def test_prediction_no_progress_blocks_without_changing_design(self):
        """Zero-scoring Prediction is an infrastructure failure: block (retryable)
        and never mutate the Design strategy or spawn a sibling."""
        root_id = "N0001"
        State.update({
            "research_pipeline_meta": {"run_status": "complete"},
            "candidate_count": 1,
            "thresholds": {},
        })
        CandidateIndex.add({
            "candidate_id": "C1001",
            "sequence": "GFEWALAAKCFG",
            "source_batch": f"{root_id}/route_A_mdm2/L12",
            "manifest_path": "m.json",
        })
        tree = SearchTree(path=DATA_DIR / "search_tree.json", beam_width=2, max_nodes=6)
        root = tree.init_root()
        self.assertEqual(root["node_id"], root_id)

        with patch.object(
            run_pipeline, "mock_predict", return_value={"status": "ok", "scored": []},
        ):
            result = run_pipeline.run_once_node(tree, dry_run=True)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failed_agent"], "prediction")
        self.assertTrue(result["retryable"])
        # No Design re-branch: node count unchanged, no planner_adjust.
        self.assertEqual(tree.node_count(), 1)
        self.assertNotEqual(tree.get(root_id)["status"], "dead_end")
        entries = _evidence_entries()
        self.assertEqual(
            [e for e in entries if e.get("event_type") == "planner_adjust"], []
        )
        err = [
            e for e in entries
            if e.get("event_type") == "error"
            and e.get("error_type") == "upstream_no_progress"
        ]
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0]["phase"], "evaluate")
        self.assertEqual(err[0]["failure_scope"], "search")

    def test_research_no_progress_blocks_and_logs_research_phase(self):
        """Research no-progress must be logged under phase=research and block the
        search, not enter Design sibling expansion."""
        tree = SearchTree(path=DATA_DIR / "search_tree.json", beam_width=2, max_nodes=6)
        root = tree.init_root()

        with patch.object(run_pipeline, "mock_research", return_value={"status": "ok"}):
            result = run_pipeline.run_once_node(tree, dry_run=True)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failed_agent"], "research")
        self.assertFalse(result["retryable"])
        self.assertEqual(tree.node_count(), 1)
        entries = _evidence_entries()
        self.assertEqual(
            [e for e in entries if e.get("event_type") == "planner_adjust"], []
        )
        err = [
            e for e in entries
            if e.get("event_type") == "error"
            and e.get("error_type") == "upstream_no_progress"
        ]
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0]["phase"], "research")

    def test_adjust_projects_state_round_onto_child_round(self):
        """P2 round drift: after adjust spawns a child, State.round equals the
        child's tree round rather than a blind increment of a drifted counter."""
        State.update({
            "research_pipeline_meta": {"run_status": "complete"},
            "candidate_count": 0,
            "round": 5,  # simulate a drifted global counter
        })
        tree = SearchTree(path=DATA_DIR / "search_tree.json", beam_width=2, max_nodes=6)
        root = tree.init_root(round_num=1)
        report = {
            "event_id": "evt-round",
            "verdict": "backtrack",
            "issues": [{"code": "low_diversity"}],
            "recommendation": "try sibling",
        }
        adj = planner_agent.adjust(report=report, tree=tree, parent=root, max_proposals=1)
        self.assertEqual(adj["status"], "adjusted")
        child = tree.get(adj["child_node_id"])
        self.assertEqual(child["round"], 2)
        self.assertEqual(int(State.load()["round"]), child["round"])

    def test_resume_terminal_dead_end_not_reexecuted(self):
        """After a synthetic dead-end child is persisted, a reloaded tree must not
        re-execute that terminal node."""
        State.update({
            "research_pipeline_meta": {"run_status": "complete"},
            "candidate_count": 0,
        })
        tree = SearchTree(path=DATA_DIR / "search_tree.json", beam_width=2, max_nodes=6)
        root = tree.init_root()
        strat = planner_agent.propose_children(root, max_proposals=1)[0]
        child = tree.add_child(root["node_id"], strat)
        with patch.object(
            run_pipeline, "mock_design",
            return_value={"status": "ok", "candidate_ids": []},
        ):
            run_pipeline.run_once_node(tree, dry_run=True)
        tree.persist()

        reloaded = SearchTree.load(DATA_DIR / "search_tree.json")
        self.assertEqual(reloaded.get(child["node_id"])["status"], "dead_end")
        active = reloaded.select_active()
        self.assertIsNotNone(active)
        self.assertNotEqual(active["node_id"], child["node_id"])


if __name__ == "__main__":
    unittest.main()
