"""
Orchestrator integration tests (dry-run, no GPU / bio tools).

These lock in the control-flow fixes from the PR review:
  P1-1  a fresh child branch never reviews its parent's candidates
  P2-3  a max_nodes=1 run does not inflate state.round or re-review the root
  P2-5  legacy untagged root candidates get scored (no Prediction loop)
  P2-6  --resume after a pass does not re-run the Critic
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
