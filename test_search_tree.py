"""
SearchTree unit tests — backtrack, beam prune, max_nodes, restore.
No GPU / bio tools required.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agents.search_tree import (
    SearchTree,
    strategy_branch_key,
    DEFAULT_STRATEGY,
)


class SearchTreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cycpep-search-tree-"))
        self.path = self.tmp / "search_tree.json"

    def _tree(self, beam_width=3, max_nodes=20):
        return SearchTree(path=self.path, beam_width=beam_width, max_nodes=max_nodes)

    def test_init_root(self):
        tree = self._tree()
        root = tree.init_root()
        self.assertEqual(root["node_id"], "N0001")
        self.assertEqual(root["parent_id"], None)
        self.assertEqual(root["depth"], 0)
        self.assertEqual(root["status"], "open")
        self.assertEqual(tree.active_id, "N0001")
        self.assertEqual(tree.frontier, ["N0001"])
        self.assertEqual(root["strategy"]["lengths"], DEFAULT_STRATEGY["lengths"])

    def test_backtrack_to_parent_then_new_sibling(self):
        tree = self._tree(beam_width=5, max_nodes=10)
        root = tree.init_root()
        child_a = tree.add_child(
            root["node_id"],
            {
                "route_mix": {"route_A_mdm2": 200, "route_B": 200, "route_C": 100},
                "lengths": [10, 12],
                "constraints": {"bias": "mdm2"},
            },
            trigger_event_id="evt_critic_1",
        )
        self.assertEqual(tree.active_id, child_a["node_id"])
        self.assertEqual(child_a["trigger_event_id"], "evt_critic_1")
        self.assertIn("route_mix", child_a["strategy_diff"])

        parent = tree.backtrack(verdict="backtrack")
        self.assertEqual(parent["node_id"], root["node_id"])
        self.assertEqual(tree.active_id, root["node_id"])
        self.assertEqual(child_a["status"], "dead_end")
        self.assertEqual(child_a["critic_verdict"], "backtrack")

        # Sibling with a different strategy must succeed
        sibling = tree.add_child(
            root["node_id"],
            {
                "route_mix": {"route_A_mdmx": 300, "route_B": 100, "route_C": 100},
                "lengths": [12, 14],
                "constraints": {"bias": "mdmx"},
            },
            trigger_event_id="evt_critic_2",
        )
        self.assertEqual(sibling["parent_id"], root["node_id"])
        self.assertEqual(len(root["children"]), 2)
        self.assertEqual(len(root["tried_branch_keys"]), 3)  # root key + 2 children
        self.assertNotEqual(sibling["branch_key"], child_a["branch_key"])

        # Re-trying the dead child's strategy must raise
        with self.assertRaises(ValueError):
            tree.add_child(root["node_id"], child_a["strategy"])

    def test_beam_width_prunes_extra_children(self):
        tree = self._tree(beam_width=2, max_nodes=20)
        root = tree.init_root()
        children = []
        for i in range(4):
            children.append(
                tree.add_child(
                    root["node_id"],
                    {
                        "route_mix": {"route_A_mdm2": 100 + i},
                        "lengths": [10 + i],
                        "constraints": {"i": i},
                    },
                    activate=False,
                )
            )
        live = [
            c for c in children
            if tree.get(c["node_id"])["status"] in ("open", "expanding", "evaluated")
        ]
        pruned = [
            c for c in children
            if tree.get(c["node_id"])["status"] == "pruned"
        ]
        self.assertEqual(len(live), 2)
        self.assertEqual(len(pruned), 2)
        # Most recent two kept
        self.assertEqual({c["node_id"] for c in live}, {children[-2]["node_id"], children[-1]["node_id"]})

    def test_max_nodes_stops_expansion(self):
        tree = self._tree(beam_width=10, max_nodes=3)
        root = tree.init_root()
        tree.add_child(root["node_id"], {
            "route_mix": {"route_A_mdm2": 1},
            "lengths": [10],
            "constraints": {},
        }, activate=False)
        tree.add_child(root["node_id"], {
            "route_mix": {"route_A_mdm2": 2},
            "lengths": [12],
            "constraints": {},
        }, activate=False)
        self.assertTrue(tree.budget_exhausted())
        with self.assertRaises(RuntimeError):
            tree.add_child(root["node_id"], {
                "route_mix": {"route_A_mdm2": 3},
                "lengths": [14],
                "constraints": {},
            })

    def test_restore_checkpoint(self):
        tree = self._tree()
        root = tree.init_root()
        child = tree.advance(
            {
                "route_mix": {"route_B": 200},
                "lengths": [12],
                "constraints": {},
            },
            trigger_event_id="evt_adv",
        )
        tree.update_checkpoint(
            child["node_id"],
            candidate_ids=["C0001", "C0002"],
            stats_snapshot={"total_candidates": 2, "l1_pass": 1},
            thresholds_ref="state.thresholds",
        )
        tree.mark_evaluated(child["node_id"], critic_verdict="advance")

        restored = tree.restore(root["node_id"])
        self.assertEqual(tree.active_id, root["node_id"])
        self.assertEqual(restored["node_id"], root["node_id"])
        # Child checkpoint untouched
        cp = tree.get(child["node_id"])["checkpoint"]
        self.assertEqual(cp["candidate_ids"], ["C0001", "C0002"])
        self.assertEqual(cp["stats_snapshot"]["total_candidates"], 2)

        # Restore pruned node reopens it
        pruned = tree.add_child(
            root["node_id"],
            {"route_mix": {"route_C": 50}, "lengths": [8], "constraints": {}},
            activate=False,
        )
        tree.mark_pruned(pruned["node_id"])
        reopened = tree.restore(pruned["node_id"])
        self.assertEqual(reopened["status"], "open")
        self.assertIn(pruned["node_id"], tree.frontier)

    def test_persist_and_load(self):
        tree = self._tree(beam_width=2, max_nodes=7)
        root = tree.init_root()
        tree.add_child(root["node_id"], {
            "route_mix": {"route_A_mdm2": 50},
            "lengths": [10],
            "constraints": {"note": "persist"},
        })
        tree.persist()
        self.assertTrue(self.path.exists())

        loaded = SearchTree.load(self.path)
        self.assertEqual(loaded.root_id, root["node_id"])
        self.assertEqual(loaded.node_count(), 2)
        self.assertEqual(loaded.config["beam_width"], 2)
        self.assertEqual(loaded.config["max_nodes"], 7)
        child = loaded.active_node()
        self.assertEqual(child["strategy"]["constraints"]["note"], "persist")

    def test_strategy_branch_key_stable(self):
        a = {"route_mix": {"b": 1, "a": 2}, "lengths": [10, 12], "constraints": {}}
        b = {"route_mix": {"a": 2, "b": 1}, "lengths": [10, 12], "constraints": {}}
        self.assertEqual(strategy_branch_key(a), strategy_branch_key(b))

    def test_mark_passed_and_best_leaf(self):
        tree = self._tree()
        root = tree.init_root()
        child = tree.advance({
            "route_mix": {"route_A_mdm2": 10},
            "lengths": [12],
            "constraints": {},
        })
        tree.mark_passed(child["node_id"])
        best = tree.best_leaf()
        self.assertEqual(best["node_id"], child["node_id"])
        self.assertEqual(best["status"], "passed")

    def test_pick_next_skips_dead(self):
        tree = self._tree(beam_width=5)
        root = tree.init_root()
        a = tree.add_child(root["node_id"], {
            "route_mix": {"route_A_mdm2": 1}, "lengths": [10], "constraints": {},
        }, activate=False)
        b = tree.add_child(root["node_id"], {
            "route_mix": {"route_A_mdm2": 2}, "lengths": [12], "constraints": {},
        }, activate=False)
        tree.mark_dead_end(a["node_id"])
        nxt = tree.pick_next()
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt["node_id"], b["node_id"])

    def test_beam_prune_clears_active_id(self):
        """
        Regression (review P2-4): if the beam prunes the active child, active_id
        must be cleared so the orchestrator does not revive an out-of-beam node.
        """
        tree = self._tree(beam_width=1, max_nodes=10)
        root = tree.init_root()
        first = tree.add_child(root["node_id"], {
            "route_mix": {"route_A_mdm2": 1}, "lengths": [10], "constraints": {"i": 1},
        }, activate=True)
        self.assertEqual(tree.active_id, first["node_id"])
        # Adding a second child under beam_width=1 prunes the first (older) child.
        second = tree.add_child(root["node_id"], {
            "route_mix": {"route_A_mdm2": 2}, "lengths": [12], "constraints": {"i": 2},
        }, activate=False)
        self.assertEqual(tree.get(first["node_id"])["status"], "pruned")
        # The pruned node was active; active_id must not still point at it.
        self.assertNotEqual(tree.active_id, first["node_id"])
        # select_active must skip the pruned node entirely.
        self.assertNotEqual((tree.select_active() or {}).get("node_id"), first["node_id"])

    def test_mark_expanding_rejects_terminal_nodes(self):
        """Regression (review P2-4/6): terminal nodes must never be re-expanded."""
        tree = self._tree()
        root = tree.init_root()
        child = tree.advance({
            "route_mix": {"route_A_mdm2": 1}, "lengths": [10], "constraints": {},
        })
        for marker in (tree.mark_passed, tree.mark_dead_end, tree.mark_pruned):
            marker(child["node_id"])
            with self.assertRaises(ValueError):
                tree.mark_expanding(child["node_id"])

    def test_mark_passed_clears_active_for_resume(self):
        """
        Regression (review P2-6): a passed node must drop out of active_id so a
        --resume run recognises the finished state instead of re-running Critic.
        """
        tree = self._tree()
        root = tree.init_root()
        child = tree.advance({
            "route_mix": {"route_A_mdm2": 1}, "lengths": [10], "constraints": {},
        })
        tree.mark_passed(child["node_id"])
        self.assertIsNone(tree.active_id)
        # Nothing live remains, so select_active returns None (search finished).
        self.assertIsNone(tree.select_active())

    def test_mark_dead_end_preserve_verdict_keeps_real_critic_verdict(self):
        """
        Review P2-C: retiring a node for branch-space exhaustion (an orchestrator
        search decision) must NOT overwrite a real Critic verdict with a faked
        'dead_end'. preserve_verdict keeps the existing verdict and only records
        the search-level termination metadata.
        """
        tree = self._tree()
        root = tree.init_root()
        child = tree.advance({
            "route_mix": {"route_A_mdm2": 1}, "lengths": [10], "constraints": {},
        })
        # Simulate a node the Critic really judged as 'advance'.
        tree.mark_evaluated(child["node_id"], critic_verdict="advance")
        self.assertEqual(tree.get(child["node_id"])["critic_verdict"], "advance")

        tree.mark_dead_end(
            child["node_id"],
            preserve_verdict=True,
            termination_reason="child_strategy_space_exhausted",
            failure_source="orchestrator",
        )
        node = tree.get(child["node_id"])
        self.assertEqual(node["status"], "dead_end")
        # Real verdict preserved, not clobbered to a fake dead_end.
        self.assertEqual(node["critic_verdict"], "advance")
        self.assertEqual(node["termination_reason"], "child_strategy_space_exhausted")
        self.assertEqual(node["failure_source"], "orchestrator")

    def test_has_live_children(self):
        """Deep backtracking relies on has_live_children to decide whether an
        ancestor still has work under it (a live beam sibling) or is exhausted."""
        tree = self._tree(beam_width=5, max_nodes=10)
        root = tree.init_root()
        a = tree.add_child(root["node_id"], {
            "route_mix": {"route_A_mdm2": 1}, "lengths": [10], "constraints": {"i": 1},
        }, activate=False)
        b = tree.add_child(root["node_id"], {
            "route_mix": {"route_A_mdm2": 2}, "lengths": [12], "constraints": {"i": 2},
        }, activate=False)
        self.assertTrue(tree.has_live_children(root["node_id"]))
        tree.mark_dead_end(a["node_id"])
        self.assertTrue(tree.has_live_children(root["node_id"]))  # b still live
        tree.mark_dead_end(b["node_id"])
        self.assertFalse(tree.has_live_children(root["node_id"]))  # both dead

    def test_reopen_blocked_respects_retryable(self):
        """Review P2-D: retryable blocked nodes reopen (used on --resume);
        non-retryable ones stay blocked until an explicit retry."""
        tree = self._tree(beam_width=3, max_nodes=10)
        root = tree.init_root()
        retry = tree.add_child(root["node_id"], {
            "route_mix": {"route_B": 1}, "lengths": [10], "constraints": {"i": 1},
        }, activate=False)
        hard = tree.add_child(root["node_id"], {
            "route_mix": {"route_C": 1}, "lengths": [12], "constraints": {"i": 2},
        }, activate=False)
        tree.mark_blocked(retry["node_id"], blocked_by="prediction", retryable=True,
                          termination_reason="prediction_no_progress")
        tree.mark_blocked(hard["node_id"], blocked_by="research", retryable=False,
                          termination_reason="research_no_progress")

        reopened = tree.reopen_blocked(retryable_only=True)
        self.assertEqual(reopened, [retry["node_id"]])
        self.assertEqual(tree.get(retry["node_id"])["status"], "open")
        self.assertIn(retry["node_id"], tree.frontier)
        self.assertEqual(tree.get(hard["node_id"])["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
