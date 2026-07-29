"""
SearchTree — 策略轮次级有界回溯搜索树引擎。

每个节点 = 一轮 design → predict → critic 的策略配置。
失败时把当前节点标 dead_end，active 指回父节点，再展开未试过的兄弟分支。
全程只追加不删除；候选与证据靠 node_id / source_batch / trigger_event_id 溯源。

纯逻辑，不依赖其他 agents。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Optional

NODE_STATUSES = (
    "open",
    "expanding",
    "evaluated",
    "passed",
    "dead_end",
    "pruned",
)

CRITIC_VERDICTS = ("advance", "backtrack", "dead_end", "done", None)

DEFAULT_STRATEGY = {
    "route_mix": {
        "route_A_mdm2": 400,
        "route_A_mdmx": 400,
        "route_B": 400,
        "route_C": 200,
    },
    "lengths": [10, 12, 14],
    "constraints": {},
}


def default_tree_path() -> Path:
    """Resolve search_tree.json under CYCPEP_DATA_DIR or repo data/."""
    data_dir = os.environ.get("CYCPEP_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "search_tree.json"
    root = Path(__file__).resolve().parent.parent
    return root / "data" / "search_tree.json"


def strategy_branch_key(strategy: dict) -> str:
    """Stable key so siblings are unique under tried_branch_keys."""
    payload = {
        "route_mix": strategy.get("route_mix") or {},
        "lengths": list(strategy.get("lengths") or []),
        "constraints": strategy.get("constraints") or {},
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def strategy_diff(parent: Optional[dict], child: dict) -> dict:
    """Record what changed relative to parent (for audit / connection)."""
    if not parent:
        return {"full": copy.deepcopy(child)}
    diff: dict[str, Any] = {}
    for key in ("route_mix", "lengths", "constraints"):
        if parent.get(key) != child.get(key):
            diff[key] = {"from": copy.deepcopy(parent.get(key)), "to": copy.deepcopy(child.get(key))}
    return diff


class SearchTree:
    """In-memory search tree with JSON persistence and beam-bounded backtracking."""

    def __init__(
        self,
        path: Optional[Path] = None,
        beam_width: int = 3,
        max_nodes: int = 20,
    ):
        self.path = Path(path) if path else default_tree_path()
        self.nodes: dict[str, dict] = {}
        self.root_id: Optional[str] = None
        self.active_id: Optional[str] = None
        self.frontier: list[str] = []
        self.config = {"beam_width": int(beam_width), "max_nodes": int(max_nodes)}
        self.node_seq = 0

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def init_root(self, strategy: Optional[dict] = None, round_num: int = 1) -> dict:
        """Create root node and make it active. Clears any previous tree state."""
        self.nodes.clear()
        self.frontier.clear()
        self.node_seq = 0
        strategy = copy.deepcopy(strategy or DEFAULT_STRATEGY)
        node = self._make_node(
            parent_id=None,
            depth=0,
            round_num=round_num,
            strategy=strategy,
            trigger_event_id=None,
        )
        self.root_id = node["node_id"]
        self.active_id = node["node_id"]
        self.frontier = [node["node_id"]]
        return node

    def _make_node(
        self,
        parent_id: Optional[str],
        depth: int,
        round_num: int,
        strategy: dict,
        trigger_event_id: Optional[str],
        parent_strategy: Optional[dict] = None,
    ) -> dict:
        self.node_seq += 1
        node_id = f"N{self.node_seq:04d}"
        node = {
            "node_id": node_id,
            "parent_id": parent_id,
            "depth": depth,
            "round": round_num,
            "status": "open",
            "strategy": copy.deepcopy(strategy),
            "strategy_diff": strategy_diff(parent_strategy, strategy),
            "branch_key": strategy_branch_key(strategy),
            "checkpoint": {
                "candidate_ids": [],
                "stats_snapshot": {},
                "thresholds_ref": None,
            },
            "critic_verdict": None,
            "children": [],
            "tried_branch_keys": [strategy_branch_key(strategy)],
            "trigger_event_id": trigger_event_id,
        }
        self.nodes[node_id] = node
        return node

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------
    def active_node(self) -> Optional[dict]:
        if not self.active_id:
            return None
        return self.nodes.get(self.active_id)

    def get(self, node_id: str) -> Optional[dict]:
        return self.nodes.get(node_id)

    def node_count(self) -> int:
        return len(self.nodes)

    def budget_exhausted(self) -> bool:
        return self.node_count() >= self.config["max_nodes"]

    def set_config(self, beam_width: Optional[int] = None, max_nodes: Optional[int] = None):
        if beam_width is not None:
            self.config["beam_width"] = max(1, int(beam_width))
        if max_nodes is not None:
            self.config["max_nodes"] = max(1, int(max_nodes))

    # ------------------------------------------------------------------
    # Lifecycle mutations
    # ------------------------------------------------------------------
    def mark_expanding(self, node_id: Optional[str] = None) -> dict:
        node = self._require(node_id or self.active_id)
        node["status"] = "expanding"
        return node

    def mark_evaluated(
        self,
        node_id: Optional[str] = None,
        checkpoint: Optional[dict] = None,
        critic_verdict: Optional[str] = None,
    ) -> dict:
        node = self._require(node_id or self.active_id)
        node["status"] = "evaluated"
        if checkpoint:
            node["checkpoint"] = {
                "candidate_ids": list(checkpoint.get("candidate_ids") or []),
                "stats_snapshot": copy.deepcopy(checkpoint.get("stats_snapshot") or {}),
                "thresholds_ref": checkpoint.get("thresholds_ref"),
            }
        if critic_verdict is not None:
            if critic_verdict not in CRITIC_VERDICTS:
                raise ValueError(f"invalid critic_verdict: {critic_verdict}")
            node["critic_verdict"] = critic_verdict
        return node

    def mark_passed(self, node_id: Optional[str] = None) -> dict:
        node = self._require(node_id or self.active_id)
        node["status"] = "passed"
        node["critic_verdict"] = "done"
        if node["node_id"] in self.frontier:
            self.frontier = [n for n in self.frontier if n != node["node_id"]]
        return node

    def mark_dead_end(self, node_id: Optional[str] = None, verdict: str = "dead_end") -> dict:
        node = self._require(node_id or self.active_id)
        node["status"] = "dead_end"
        node["critic_verdict"] = verdict if verdict in ("backtrack", "dead_end") else "dead_end"
        if node["node_id"] in self.frontier:
            self.frontier = [n for n in self.frontier if n != node["node_id"]]
        return node

    def mark_pruned(self, node_id: str) -> dict:
        node = self._require(node_id)
        node["status"] = "pruned"
        if node_id in self.frontier:
            self.frontier = [n for n in self.frontier if n != node_id]
        return node

    def update_checkpoint(self, node_id: Optional[str] = None, **fields) -> dict:
        node = self._require(node_id or self.active_id)
        cp = node.setdefault("checkpoint", {
            "candidate_ids": [],
            "stats_snapshot": {},
            "thresholds_ref": None,
        })
        if "candidate_ids" in fields:
            cp["candidate_ids"] = list(fields["candidate_ids"] or [])
        if "stats_snapshot" in fields:
            cp["stats_snapshot"] = copy.deepcopy(fields["stats_snapshot"] or {})
        if "thresholds_ref" in fields:
            cp["thresholds_ref"] = fields["thresholds_ref"]
        return node

    # ------------------------------------------------------------------
    # Tree growth / backtrack
    # ------------------------------------------------------------------
    def add_child(
        self,
        parent_id: Optional[str],
        strategy: dict,
        trigger_event_id: Optional[str] = None,
        round_num: Optional[int] = None,
        activate: bool = True,
        enqueue: bool = True,
    ) -> dict:
        """Add a child under parent. Raises if budget exhausted or branch already tried."""
        if self.budget_exhausted():
            raise RuntimeError(
                f"max_nodes={self.config['max_nodes']} exhausted "
                f"(current={self.node_count()})"
            )
        parent = self._require(parent_id) if parent_id else None
        branch_key = strategy_branch_key(strategy)
        if parent and branch_key in parent.get("tried_branch_keys", []):
            raise ValueError(f"branch already tried under {parent_id}: {branch_key}")

        depth = (parent["depth"] + 1) if parent else 0
        if round_num is None:
            round_num = (parent["round"] + 1) if parent else 1
        child = self._make_node(
            parent_id=parent["node_id"] if parent else None,
            depth=depth,
            round_num=round_num,
            strategy=copy.deepcopy(strategy),
            trigger_event_id=trigger_event_id,
            parent_strategy=parent["strategy"] if parent else None,
        )
        if parent:
            parent.setdefault("children", []).append(child["node_id"])
            parent.setdefault("tried_branch_keys", []).append(branch_key)
            # Parent has been expanded into children; leave frontier to the kids.
            if parent["node_id"] in self.frontier:
                self.frontier = [n for n in self.frontier if n != parent["node_id"]]
        if enqueue:
            self.frontier.append(child["node_id"])
            self._apply_beam(parent["node_id"] if parent else None)
        if activate:
            self.active_id = child["node_id"]
        return child

    def advance(
        self,
        strategy: dict,
        trigger_event_id: Optional[str] = None,
        from_node_id: Optional[str] = None,
    ) -> dict:
        """Go deeper: add a more refined child under the current (or given) node."""
        parent_id = from_node_id or self.active_id
        return self.add_child(
            parent_id=parent_id,
            strategy=strategy,
            trigger_event_id=trigger_event_id,
            activate=True,
            enqueue=True,
        )

    def backtrack(
        self,
        node_id: Optional[str] = None,
        verdict: str = "backtrack",
    ) -> Optional[dict]:
        """
        Mark node dead_end and move active_id to its parent.
        Returns the parent node, or None if already at root.
        Does NOT invent a sibling — caller should call add_child / propose_children.
        """
        node = self.mark_dead_end(node_id or self.active_id, verdict=verdict)
        parent_id = node.get("parent_id")
        if not parent_id:
            self.active_id = None
            return None
        self.active_id = parent_id
        # Parent may still spawn untried siblings — keep it reachable via frontier.
        if parent_id not in self.frontier:
            parent = self.nodes[parent_id]
            if parent["status"] not in ("dead_end", "pruned", "passed"):
                self.frontier.insert(0, parent_id)
        return self.nodes[parent_id]

    def restore(self, node_id: str) -> dict:
        """
        Point active_id at an existing node so the orchestrator can resume from
        its checkpoint. Does not delete descendants or candidates.
        """
        node = self._require(node_id)
        self.active_id = node_id
        if node["status"] in ("dead_end", "pruned", "passed"):
            # Restore for inspection / re-branching: reopen if it was pruned only.
            if node["status"] == "pruned":
                node["status"] = "open"
                if node_id not in self.frontier:
                    self.frontier.append(node_id)
        return node

    def pick_next(self) -> Optional[dict]:
        """Pick next frontier node that is still open / expanding / evaluated."""
        live = {"open", "expanding", "evaluated"}
        while self.frontier:
            nid = self.frontier[0]
            node = self.nodes.get(nid)
            if node and node["status"] in live:
                self.active_id = nid
                return node
            self.frontier.pop(0)
        # Fall back to active if still live
        active = self.active_node()
        if active and active["status"] in live:
            return active
        return None

    def _apply_beam(self, parent_id: Optional[str]):
        """Keep at most beam_width live children of parent in the frontier."""
        if not parent_id:
            return
        parent = self.nodes.get(parent_id)
        if not parent:
            return
        k = self.config["beam_width"]
        live_children = [
            cid for cid in parent.get("children", [])
            if self.nodes.get(cid, {}).get("status") in ("open", "expanding", "evaluated")
        ]
        if len(live_children) <= k:
            return
        # Keep the most recent k; prune older extras
        keep = set(live_children[-k:])
        for cid in live_children:
            if cid not in keep:
                self.mark_pruned(cid)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "root_id": self.root_id,
            "active_id": self.active_id,
            "frontier": list(self.frontier),
            "config": copy.deepcopy(self.config),
            "node_seq": self.node_seq,
            "nodes": copy.deepcopy(self.nodes),
        }

    def persist(self, path: Optional[Path] = None) -> Path:
        out = Path(path) if path else self.path
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(out)
        self.path = out
        return out

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "SearchTree":
        path = Path(path) if path else default_tree_path()
        if not path.exists():
            raise FileNotFoundError(f"search tree not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        tree = cls(
            path=path,
            beam_width=data.get("config", {}).get("beam_width", 3),
            max_nodes=data.get("config", {}).get("max_nodes", 20),
        )
        tree.root_id = data.get("root_id")
        tree.active_id = data.get("active_id")
        tree.frontier = list(data.get("frontier") or [])
        tree.node_seq = int(data.get("node_seq") or 0)
        tree.nodes = data.get("nodes") or {}
        tree.config = {
            "beam_width": int(data.get("config", {}).get("beam_width", 3)),
            "max_nodes": int(data.get("config", {}).get("max_nodes", 20)),
        }
        return tree

    def best_leaf(self) -> Optional[dict]:
        """Prefer passed nodes, else deepest evaluated/open node."""
        if not self.nodes:
            return None
        passed = [n for n in self.nodes.values() if n["status"] == "passed"]
        if passed:
            return max(passed, key=lambda n: (n["depth"], n["round"]))
        candidates = [
            n for n in self.nodes.values()
            if n["status"] in ("evaluated", "open", "expanding")
        ]
        if not candidates:
            candidates = list(self.nodes.values())
        return max(candidates, key=lambda n: (n["depth"], n["round"], n["node_id"]))

    def _require(self, node_id: Optional[str]) -> dict:
        if not node_id or node_id not in self.nodes:
            raise KeyError(f"unknown node_id: {node_id}")
        return self.nodes[node_id]
