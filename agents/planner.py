"""
Planner Agent — 赵嘉策
职责：读 state.json → 判断当前阶段 → 产出任务列表 → 根据 Critic 反馈调整策略
入口：plan(state, tree=None, node=None) → list[Task]
      propose_children(parent, critic_report, exclude=()) → list[strategy]
      adjust(report, tree=None, parent=None) → dict
依赖：from data_layer import State, EvidenceLogger
"""
from __future__ import annotations

import copy
from typing import Optional

from agents.search_tree import strategy_branch_key
from data_layer import EvidenceLogger, State


def _has_research(state: dict) -> bool:
    """Research is present if pipeline meta or pocket / binder payloads exist."""
    meta = state.get("research_pipeline_meta") or {}
    if meta:
        return True
    if state.get("pocket_differences"):
        return True
    if state.get("known_dual_binders"):
        return True
    # Targets enriched by research often land under these keys
    if state.get("hotspot_analysis") or state.get("research_targets"):
        return True
    return False


_SCORE_KEYS = (
    "plddt", "ipsae_mdm2", "ipsae_mdmx", "l1_pass", "l2_pass",
    "all_layers_pass", "metric_clearance",
)


def _row_scored(row: dict) -> bool:
    """A candidate is 'scored' once any prediction-derived field is populated."""
    for key in _SCORE_KEYS:
        if row.get(key) not in (None, ""):
            return True
    return False


def _any_scored(candidates: list) -> bool:
    return any(_row_scored(row) for row in (candidates or []))


def _candidates_for_node(candidates: list, node_id: Optional[str]) -> list:
    if not node_id:
        return list(candidates or [])
    return [
        c for c in (candidates or [])
        if str(c.get("source_batch") or "").startswith(f"{node_id}/")
        or str(c.get("source_batch") or "") == node_id
    ]


def scope_candidates(node: Optional[dict], candidates: list) -> list:
    """
    The candidates a node is responsible for — the single source of truth for
    scoping, shared by the Planner and the orchestrator's Critic step.

    A non-root node sees ONLY its own tagged rows; an empty result is the signal
    to design, never a licence to borrow the parent's rows. The root may inherit
    an untagged legacy pool (e.g. a pre-existing CSV) so it is not ignored.
    """
    node_id = (node or {}).get("node_id")
    node_cands = _candidates_for_node(candidates, node_id)
    if node_cands:
        return node_cands
    is_root = not node or node.get("depth", 0) == 0
    if is_root:
        return list(candidates or [])
    return []


def plan(
    state: Optional[dict] = None,
    tree=None,
    node: Optional[dict] = None,
    candidates: Optional[list] = None,
) -> list[dict]:
    """
    Minimal phase router.

    Returns a list of task dicts:
      {agent, action, phase, reason, needs_gpu?, strategy?, node_id?}
    """
    state = state if state is not None else State.load()
    node = node or (tree.active_node() if tree is not None else None)
    strategy = (node or {}).get("strategy") or {
        "route_mix": copy.deepcopy(state.get("design_budget") or {}),
        "lengths": [10, 12, 14],
        "constraints": {},
    }
    node_id = (node or {}).get("node_id")
    round_num = (node or {}).get("round") or state.get("round") or 1

    if not _has_research(state):
        return [{
            "agent": "research",
            "action": "run",
            "phase": "research",
            "reason": "no research result in state",
            "needs_gpu": False,
            "node_id": node_id,
            "round": round_num,
        }]

    if candidates is None:
        try:
            from data_layer import CandidateIndex
            candidates = CandidateIndex.load()
        except Exception:
            candidates = []

    # Each search node judges only its own candidates (see scope_candidates).
    scope = scope_candidates(node, candidates)

    if len(scope) == 0:
        return [{
            "agent": "design",
            "action": "generate",
            "phase": "design",
            "reason": (
                "research present but no candidates for this node"
                if node_id else
                "research present but candidate pool is empty"
            ),
            "needs_gpu": True,
            "strategy": copy.deepcopy(strategy),
            "node_id": node_id,
            "round": round_num,
        }]

    # Route the candidates that still lack scores back to Prediction. Only when
    # every candidate in scope is scored do we hand off to the Critic. A partial
    # prediction must not jump to Critic, which would flag the unscored rows as a
    # hard failure and backtrack an otherwise viable branch.
    unscored = [c for c in scope if not _row_scored(c)]
    if unscored:
        return [{
            "agent": "prediction",
            "action": "score",
            "phase": "evaluate",
            "reason": f"{len(unscored)} of {len(scope)} candidate(s) still need scoring",
            "needs_gpu": True,
            "node_id": node_id,
            "round": round_num,
            "candidate_ids": [
                c.get("candidate_id") for c in unscored if c.get("candidate_id")
            ],
        }]

    return [{
        "agent": "critic",
        "action": "review",
        "phase": "critic",
        "reason": "candidates are scored; ready for critic review",
        "needs_gpu": False,
        "node_id": node_id,
        "round": round_num,
    }]


def _issue_codes(report: Optional[dict]) -> set[str]:
    if not report:
        return set()
    return {str(i.get("code") or "") for i in (report.get("issues") or []) if isinstance(i, dict)}


def propose_children(
    parent: dict,
    critic_report: Optional[dict] = None,
    exclude: Optional[set] = None,
    max_proposals: int = 3,
) -> list[dict]:
    """
    Propose sibling strategies under parent that are not in exclude /
    parent['tried_branch_keys'].

    Strategies are derived from parent.strategy with Critic-driven tweaks.
    Does not mutate state or write evidence — call adjust() for that.
    """
    exclude = set(exclude or ())
    exclude |= set(parent.get("tried_branch_keys") or [])
    base = copy.deepcopy(parent.get("strategy") or {})
    route_mix = copy.deepcopy(base.get("route_mix") or State.load().get("design_budget") or {})
    lengths = list(base.get("lengths") or [10, 12, 14])
    constraints = copy.deepcopy(base.get("constraints") or {})
    codes = _issue_codes(critic_report)

    proposals: list[dict] = []

    def _try(route_mix_, lengths_, constraints_, label: str):
        strat = {
            "route_mix": copy.deepcopy(route_mix_),
            "lengths": list(lengths_),
            "constraints": {**copy.deepcopy(constraints_), "branch_label": label},
        }
        key = strategy_branch_key(strat)
        if key in exclude:
            return
        exclude.add(key)
        proposals.append(strat)

    # Default branches: shift budget toward B / C / MDMX, and length scan variants
    mix_b = copy.deepcopy(route_mix)
    mix_b["route_B"] = int(mix_b.get("route_B") or 0) + 100
    mix_b["route_A_mdm2"] = max(0, int(mix_b.get("route_A_mdm2") or 0) - 50)
    mix_b["route_A_mdmx"] = max(0, int(mix_b.get("route_A_mdmx") or 0) - 50)
    _try(mix_b, lengths, constraints, "boost_route_B")

    mix_c = copy.deepcopy(route_mix)
    mix_c["route_C"] = int(mix_c.get("route_C") or 0) + 100
    mix_c["route_A_mdm2"] = max(0, int(mix_c.get("route_A_mdm2") or 0) - 50)
    _try(mix_c, lengths, {**constraints, "prefer_known_binder_cyclize": True}, "boost_route_C")

    mix_mx = copy.deepcopy(route_mix)
    mix_mx["route_A_mdmx"] = int(mix_mx.get("route_A_mdmx") or 0) + 150
    mix_mx["route_A_mdm2"] = max(0, int(mix_mx.get("route_A_mdm2") or 0) - 100)
    _try(mix_mx, lengths, {**constraints, "bias": "mdmx"}, "bias_mdmx")

    if "low_diversity" in codes or "empty_candidate_pool" in codes:
        longer = sorted(set(lengths + [14, 16]))
        _try(route_mix, longer, {**constraints, "diversity": "high"}, "longer_diverse")

    if "threshold_needs_review" in codes or "threshold_uncalibrated" in codes:
        _try(
            route_mix,
            lengths,
            {**constraints, "require_positive_control_first": True},
            "positive_control_first",
        )

    if "duplicate_sequences" in codes:
        hotter = copy.deepcopy(route_mix)
        hotter["route_A_mdm2"] = int(hotter.get("route_A_mdm2") or 0) + 50
        _try(hotter, lengths, {**constraints, "sampling_temp": 0.2}, "raise_sampling_temp")

    # Always offer a length-shifted variant as last resort
    shifted = [max(8, (lengths[0] if lengths else 10) - 2)] + [
        min(20, L + 2) for L in (lengths or [12])
    ]
    _try(route_mix, sorted(set(shifted)), {**constraints, "length_shift": True}, "length_shift")

    return proposals[:max_proposals]


def adjust(
    report: dict,
    tree=None,
    parent: Optional[dict] = None,
    state: Optional[dict] = None,
    max_proposals: int = 1,
) -> dict:
    """
    Apply Critic report: propose a new strategy, write planner_adjust evidence,
    optionally spawn a child on the search tree.

    Returns:
      {
        status, old_strategy, new_strategy, trigger_event_id,
        child_node_id?, reason, proposals
      }
    """
    state = state if state is not None else State.load()
    parent = parent or (tree.active_node() if tree is not None else None)
    if parent is None and tree is not None and tree.root_id:
        parent = tree.get(tree.root_id)
    if parent is None:
        # Synthetic parent from state budget
        parent = {
            "node_id": None,
            "strategy": {
                "route_mix": copy.deepcopy(state.get("design_budget") or {}),
                "lengths": [10, 12, 14],
                "constraints": {},
            },
            "tried_branch_keys": [],
        }

    old_strategy = copy.deepcopy(parent.get("strategy") or {})
    trigger_event_id = report.get("event_id") or report.get("trigger_event_id") or ""
    proposals = propose_children(parent, report, max_proposals=max(3, max_proposals))
    if not proposals:
        return {
            "status": "no_untried_branch",
            "old_strategy": old_strategy,
            "new_strategy": None,
            "trigger_event_id": trigger_event_id,
            "reason": "all candidate strategy branches already tried",
            "proposals": [],
        }

    new_strategy = proposals[0]
    reason_parts = [i.get("code") for i in (report.get("issues") or []) if i.get("code")]
    reason = report.get("recommendation") or (
        "adjust after critic: " + (", ".join(reason_parts) if reason_parts else "unspecified")
    )

    # Feasibility of spawning a child MUST be decided before mutating state or
    # writing evidence. Otherwise a budget-exhausted or already-tried branch still
    # bumps state.round, rewrites design_budget, and logs a planner_adjust that
    # never produced a node — the loop then keeps re-adjusting the same parent.
    can_spawn = tree is not None and bool(parent.get("node_id"))
    child = None
    if can_spawn:
        if tree.budget_exhausted():
            return {
                "status": "budget_exhausted",
                "old_strategy": old_strategy,
                "new_strategy": new_strategy,
                "trigger_event_id": trigger_event_id,
                "reason": "max_nodes reached; not spawning",
                "proposals": proposals,
                "child_node_id": None,
            }
        try:
            child = tree.add_child(
                parent_id=parent["node_id"],
                strategy=new_strategy,
                trigger_event_id=trigger_event_id or None,
                activate=True,
                enqueue=True,
            )
        except (ValueError, RuntimeError) as exc:
            return {
                "status": "spawn_failed",
                "old_strategy": old_strategy,
                "new_strategy": new_strategy,
                "trigger_event_id": trigger_event_id,
                "reason": str(exc),
                "proposals": proposals,
                "child_node_id": None,
            }

    # Commit state + evidence only now that we know the adjustment sticks.
    # The tree node round is the single source of truth: project State.round onto
    # the round of the node we just created, rather than a blind +1. A blind
    # increment drifts after a deep backtrack spawns a shallow sibling (e.g.
    # grandchild round 3 fails → new root sibling round 2, but a +1 would push
    # State.round to 4 while the active node is round 2).
    s = State.load()
    s["design_budget"] = copy.deepcopy(new_strategy.get("route_mix") or s.get("design_budget"))
    if child is not None:
        s["round"] = int(child["round"])
    else:
        s["round"] = int(s.get("round") or 1) + 1
    State.save(s)

    EvidenceLogger.planner_adjust(
        trigger_event_id=trigger_event_id or "unknown",
        old_strategy=old_strategy,
        new_strategy=new_strategy,
        reason=reason,
        round_num=int(child["round"]) if child else int(s.get("round") or 1),
    )

    return {
        "status": "adjusted",
        "old_strategy": old_strategy,
        "new_strategy": new_strategy,
        "trigger_event_id": trigger_event_id,
        "reason": reason,
        "proposals": proposals,
        "child_node_id": child["node_id"] if child else None,
    }
