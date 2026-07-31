#!/usr/bin/env python3
"""
Orchestrator entry — bounded backtracking over the strategy search tree.

Usage:
  python scripts/run_pipeline.py --dry-run
  python scripts/run_pipeline.py --dry-run --max-nodes 8 --beam-width 2
  python scripts/run_pipeline.py --dry-run --resume
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents import critic as critic_agent
from agents import planner as planner_agent
from agents.search_tree import SearchTree, DEFAULT_STRATEGY, default_tree_path
from data_layer import (
    CandidateIndex,
    EvidenceLogger,
    State,
)


# ---------------------------------------------------------------------------
# Mock adapters (deterministic; no GPU / LLM)
# ---------------------------------------------------------------------------
AA = "ACDEFGHIKLMNPQRSTVWY"


def _stable_seq(seed: str, length: int) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    chars = []
    for i in range(length):
        chars.append(AA[int(digest[(i * 2) % len(digest):(i * 2) % len(digest) + 2] or "0", 16) % len(AA)])
    # Guarantee F/W/L motif-ish anchors for dry-run readability
    if length >= 8:
        chars[1] = "F"
        chars[3] = "W"
        chars[6] = "L"
    return "".join(chars)


def mock_research(state: dict, node: dict) -> dict:
    """Write a minimal research payload into state."""
    provisional = {
        "L1_plddt": {
            "value": 0.8, "operator": ">", "unit": None,
            "evidence_grade": "team_provisional", "calibration_status": "pending",
            "source": "dry_run_mock",
        },
        "L2_ipsae": {
            "value": 0.55, "operator": ">", "unit": None,
            "evidence_grade": "team_provisional", "calibration_status": "pending",
            "source": "dry_run_mock",
        },
        "L3_dg": {
            "value": -10, "operator": "<", "unit": "kcal/mol",
            "evidence_grade": "team_provisional", "calibration_status": "pending",
            "source": "dry_run_mock",
        },
        "L3_sc": {
            "value": 0.6, "operator": ">", "unit": None,
            "evidence_grade": "team_provisional", "calibration_status": "pending",
            "source": "dry_run_mock",
        },
        "L3_dsasa": {
            "value": 400, "operator": ">", "unit": "A^2",
            "evidence_grade": "team_provisional", "calibration_status": "pending",
            "source": "dry_run_mock",
        },
        "L4_nc_term_dist": {
            "value": 2.0, "operator": "<", "unit": "A",
            "evidence_grade": "team_provisional", "calibration_status": "pending",
            "source": "dry_run_mock",
        },
        "L5_hotspot_coverage": {
            "value": 0.67, "operator": ">=", "unit": None,
            "evidence_grade": "team_provisional", "calibration_status": "pending",
            "source": "dry_run_mock",
        },
        "L6_pose_rmsd": {
            "value": 2.0, "operator": "<", "unit": "A",
            "evidence_grade": "team_provisional", "calibration_status": "pending",
            "source": "dry_run_mock", "min_seed_fraction": 0.67,
        },
        "L7_scrmsd": {
            "value": 2.0, "operator": "<", "unit": "A",
            "evidence_grade": "team_provisional", "calibration_status": "pending",
            "source": "dry_run_mock",
        },
    }
    result = {
        "phase": "design",
        "research_pipeline_meta": {
            "run_status": "dry_run_mock",
            "pocket_source": "mock",
            "stage_status": {"mock": "complete"},
        },
        "pocket_differences": {
            "MDM2": {"hotspots": ["Phe19", "Trp23", "Leu26"]},
            "MDMX": {"hotspots": ["Phe19", "Trp23", "Leu26"]},
        },
        "known_dual_binders": [
            {"name": "PMI", "sequence": "TSFAEYWNLLSP"},
        ],
        "thresholds": state.get("thresholds") or provisional,
    }
    if not result["thresholds"]:
        result["thresholds"] = provisional
    # Ensure dry-run always has a full battery so metric_clearance is testable
    for key, entry in provisional.items():
        result["thresholds"].setdefault(key, entry)
    State.update(result)
    EvidenceLogger.log(
        agent="research",
        event_type="research_targets",
        payload={"dry_run": True, "node_id": node.get("node_id")},
        phase="research",
        round_num=node.get("round"),
    )
    return {"status": "ok", "dry_run": True}


def mock_design(state: dict, node: dict, task: dict, n: int = 4) -> dict:
    """Register n deterministic candidates tagged with source_batch=node/route/Llen."""
    strategy = task.get("strategy") or node.get("strategy") or DEFAULT_STRATEGY
    route_mix = strategy.get("route_mix") or {}
    route = next(iter(route_mix), "route_A_mdm2") if route_mix else "route_A_mdm2"
    lengths = list(strategy.get("lengths") or [12])
    length = int(lengths[0])
    node_id = node.get("node_id") or "N0000"
    batch_tag = f"{node_id}/{route}/L{length}"
    registered = []
    existing = CandidateIndex.load()
    start = len(existing) + 1
    for i in range(n):
        cid = f"C{start + i:04d}"
        seq = _stable_seq(f"{batch_tag}:{i}", length)
        # Intentionally duplicate first sequence once when branch_label asks diversity stress
        if i == 1 and (strategy.get("constraints") or {}).get("branch_label") == "boost_route_B":
            seq = _stable_seq(f"{batch_tag}:0", length)
        row = {
            "candidate_id": cid,
            "sequence": seq,
            "length": length,
            "source_route": route,
            "source_batch": batch_tag,
            "cyclization_type": "head_to_tail_amide",
            "cyclization_bonds": json.dumps([
                {"atom_1": "N_term", "atom_2": "C_term", "bond_type": "amide"}
            ]),
            "manifest_path": f"data/mock_manifests/{cid}.json",
            "design_pdb_path": "",
            "design_pdb_hash": "",
        }
        CandidateIndex.add(row)
        EvidenceLogger.log(
            agent="design",
            event_type="candidate_registered",
            payload={"candidate": row, "dry_run": True, "source_batch": batch_tag},
            phase="design",
            round_num=node.get("round"),
        )
        registered.append(cid)
    EvidenceLogger.design_batch(
        route=route,
        n_generated=n,
        n_valid=n,
        tool_name="dry_run_mock_design",
        tool_version="0.1",
        duration_sec=0.0,
    )
    # Keep state counter in sync
    s = State.load()
    s["candidate_count"] = len(CandidateIndex.load())
    s["phase"] = "evaluate"
    State.save(s)
    return {"status": "ok", "candidate_ids": registered, "source_batch": batch_tag}


def mock_predict(state: dict, node: dict, task: dict) -> dict:
    """
    Assign deterministic pseudo-scores.
    First two candidates of each batch get weak scores (fail clearance);
    later ones get strong scores so critic can eventually advance/done
    after backtracking to a better branch.

    Scoring targets exactly the candidate_ids the Planner handed over. Falling
    back to a separate source_batch filter here (as an earlier version did)
    stranded legacy root candidates that had no batch tag: they were scoped in
    by the Planner but never scored, so --resume looped on Prediction forever.
    """
    candidates = CandidateIndex.load()
    node_id = node.get("node_id") or ""
    target_ids = set(task.get("candidate_ids") or [])
    scored = []
    for idx, row in enumerate(candidates):
        cid = row.get("candidate_id")
        if target_ids:
            if cid not in target_ids:
                continue
        else:
            # No explicit ids: fall back to this node's own batch.
            batch = str(row.get("source_batch") or "")
            if node_id and not (batch.startswith(f"{node_id}/") or batch == node_id):
                continue
        if row.get("plddt") not in (None, ""):
            continue
        # Branch quality: boost_route_C / length_shift / positive_control get good scores
        constraints = (node.get("strategy") or {}).get("constraints") or {}
        label = str(constraints.get("branch_label") or "")
        # First sibling (boost_route_B) stays weak to force a visible backtrack;
        # later labeled branches get strong scores so the search can converge.
        good_branch = label in {
            "boost_route_C", "length_shift", "positive_control_first",
            "longer_diverse", "bias_mdmx", "raise_sampling_temp",
        } or (int(node.get("depth") or 0) >= 2)
        # Index within this node's unscored set
        local_idx = len(scored)
        strong = good_branch and (local_idx % 3 != 0)
        scores = {
            "plddt": 0.91 if strong else 0.55,
            "ipsae_mdm2": 0.72 if strong else 0.30,
            "ipsae_mdmx": 0.68 if strong else 0.25,
            "dg_mdm2": -12.0 if strong else -4.0,
            "dg_mdmx": -11.0 if strong else -3.5,
            "dg_method": "dry_run_mock",
            "sc_mdm2": 0.75 if strong else 0.40,
            "sc_mdmx": 0.70 if strong else 0.35,
            "dsasa_mdm2": 520 if strong else 200,
            "dsasa_mdmx": 480 if strong else 180,
            "nc_distance_pre": 1.3,
            "nc_distance_post": 1.2,
            "ring_closure_pre": True,
            "ring_closure_post": True,
            "hotspot_cov_mdm2": 1.0 if strong else 0.33,
            "hotspot_cov_mdmx": 1.0 if strong else 0.33,
            "site_consistency_mdm2": True if strong else False,
            "site_consistency_mdmx": True if strong else False,
            "pose_rmsd_mdm2": 1.1 if strong else 3.5,
            "pose_rmsd_mdmx": 1.2 if strong else 3.8,
            "seed_convergence_mdm2": 0.9 if strong else 0.2,
            "seed_convergence_mdmx": 0.85 if strong else 0.2,
            "scrmsd": 1.0 if strong else 3.0,
        }
        CandidateIndex.update_score(row["candidate_id"], scores)
        EvidenceLogger.candidate_scored(
            candidate_id=row["candidate_id"],
            layer=0,
            scores=scores,
            tool_trace={
                "tool_name": "dry_run_mock_predict",
                "tool_version": "0.1",
                "exit_code": 0,
                "duration_sec": 0.0,
            },
            passed=strong,
        )
        scored.append(row["candidate_id"])
    s = State.load()
    s["phase"] = "critic"
    State.save(s)
    return {"status": "ok", "scored": scored}


# ---------------------------------------------------------------------------
# Orchestrator loop
# ---------------------------------------------------------------------------
def _print_tasks(tasks: list[dict]):
    for t in tasks:
        gpu = " [GPU]" if t.get("needs_gpu") else ""
        print(
            f"  → {t.get('agent')}.{t.get('action')} "
            f"phase={t.get('phase')}{gpu}"
        )
        print(f"    reason: {t.get('reason')}")


def _execute(task: dict, state: dict, node: dict, dry_run: bool) -> dict:
    agent = task["agent"]
    if not dry_run:
        raise RuntimeError(
            "real adapters not wired yet; re-run with --dry-run "
            "(Design/Prediction still TODO for full stack)"
        )
    if agent == "research":
        return mock_research(state, node)
    if agent == "design":
        return mock_design(state, node, task)
    if agent == "prediction":
        return mock_predict(state, node, task)
    if agent == "critic":
        # Same scoping rule as the Planner: a node reviews only its own rows.
        # No `or all_cands` fallback — an empty child branch must never be judged
        # on its parent's candidates (which could even mark it passed by mistake).
        scoped = planner_agent.scope_candidates(node, CandidateIndex.load())
        report = critic_agent.review(
            candidates=scoped,
            thresholds=state.get("thresholds") or State.load().get("thresholds"),
            state=State.load(),
            round_num=node.get("round"),
        )
        return report
    raise ValueError(f"unknown agent: {agent}")


# Bound the in-node plan→execute chain (research→design→predict→critic is 4).
INNER_STEP_CAP = 8


def _checkpoint_scope(tree: SearchTree, node: dict, thresholds_ref=None):
    scope = planner_agent.scope_candidates(node, CandidateIndex.load())
    tree.update_checkpoint(
        node["node_id"],
        candidate_ids=[c["candidate_id"] for c in scope if c.get("candidate_id")],
        stats_snapshot=CandidateIndex.stats() if CandidateIndex.load() else {},
        thresholds_ref=thresholds_ref,
    )


def _backtrack_failed_node(tree: SearchTree, node: dict, report: dict) -> dict:
    """Backtrack a failed node and immediately try an unvisited sibling."""
    verdict = report.get("verdict") or "backtrack"

    # Root has no parent, so keep it as the branching point and create a child.
    if not node.get("parent_id"):
        if node.get("status") == "expanding":
            tree.mark_evaluated(node["node_id"], critic_verdict=verdict)
        adj = planner_agent.adjust(
            report=report, tree=tree, parent=node, max_proposals=1
        )
        print(
            f"  root branch → adjust status={adj.get('status')} "
            f"child={adj.get('child_node_id')}"
        )
        if adj.get("status") != "adjusted":
            tree.mark_dead_end(node["node_id"], verdict="dead_end")
        tree.persist()
        return {
            "status": "rebranched_root",
            "node_id": node["node_id"],
            "report": report,
            "adjust": adj,
        }

    parent = tree.backtrack(node["node_id"], verdict=verdict)
    if parent is None:
        tree.persist()
        return {
            "status": "root_dead_end",
            "node_id": node["node_id"],
            "report": report,
        }

    adj = planner_agent.adjust(
        report=report, tree=tree, parent=parent, max_proposals=1
    )
    print(
        f"  backtrack → parent {parent['node_id']}; "
        f"adjust status={adj.get('status')} child={adj.get('child_node_id')}"
    )
    if adj.get("status") != "adjusted":
        # Parent has no untried branch left. Retire it and expose its parent so
        # a higher ancestor may still branch on the next orchestrator step.
        tree.mark_dead_end(parent["node_id"], verdict="dead_end")
        grandparent_id = parent.get("parent_id")
        if grandparent_id and tree.get(grandparent_id):
            grandparent = tree.get(grandparent_id)
            if grandparent["status"] not in ("dead_end", "pruned", "passed"):
                tree.active_id = grandparent_id
                if grandparent_id not in tree.frontier:
                    tree.frontier.insert(0, grandparent_id)
    tree.persist()
    return {
        "status": "backtracked",
        "node_id": node["node_id"],
        "report": report,
        "adjust": adj,
    }


def run_once_node(tree: SearchTree, dry_run: bool = True) -> dict:
    """Plan → execute → (if critic) react for the active node. Returns step summary."""
    node = tree.select_active()
    if node is None:
        return {"status": "stopped", "reason": "no live node"}

    tree.mark_expanding(node["node_id"])
    print(f"\n[node {node['node_id']}] depth={node['depth']} round={node['round']} status=expanding")
    print(f"  strategy constraints: {(node.get('strategy') or {}).get('constraints')}")

    # Chain upstream steps (research→design→prediction) until the Planner asks
    # for a Critic review or the node stops making progress. The Critic scope is
    # decided by the Planner's rule, never a global fallback.
    report = None
    stalled = False
    last_sig = None
    for _ in range(INNER_STEP_CAP):
        tasks = planner_agent.plan(
            state=State.load(), tree=tree, node=node, candidates=CandidateIndex.load()
        )
        if not tasks:
            break
        task = tasks[0]
        sig = (task.get("agent"), task.get("action"), tuple(task.get("candidate_ids") or ()))
        if sig == last_sig:
            # Same request twice → the upstream step produced no progress.
            stalled = True
            print("  upstream produced no progress; halting node")
            break
        last_sig = sig
        _print_tasks([task])
        if task["agent"] == "critic":
            report = _execute(task, State.load(), node, dry_run)
            break
        result = _execute(task, State.load(), node, dry_run)
        print(f"  executed {task['agent']}: {result.get('status')}")

    if report is None:
        # Never reached the Critic this tick.
        _checkpoint_scope(tree, node)
        if stalled:
            # Zero-output Design / Prediction is a branch failure, not a reason
            # to abandon the whole search. Record the failure, then use exactly
            # the same backtrack→adjust→sibling path as a Critic dead_end.
            failed_agent = last_sig[0] if last_sig else "planner"
            trigger_event_id = EvidenceLogger.log(
                agent=failed_agent,
                event_type="error",
                payload={
                    "error_type": "upstream_no_progress",
                    "error_message": (
                        f"{failed_agent} repeated without producing progress "
                        f"for node {node['node_id']}"
                    ),
                    "recovery_action": "backtrack and try an unvisited sibling",
                    "node_id": node["node_id"],
                },
                phase="design" if failed_agent == "design" else "evaluate",
                round_num=node.get("round"),
            )
            stalled_report = {
                "event_id": trigger_event_id,
                "verdict": "dead_end",
                "passed": False,
                "issues": [{
                    "code": "upstream_no_progress",
                    "message": "upstream step produced no candidates or scores",
                    "owner_hint": failed_agent,
                }],
                "recommendation": "backtrack and try an unvisited sibling strategy",
                "summary": (
                    f"verdict=dead_end; node={node['node_id']}; "
                    "reason=upstream_no_progress"
                ),
            }
            return _backtrack_failed_node(tree, node, stalled_report)
        tree.mark_evaluated(node["node_id"])
        tree.persist()
        return {"status": "continued", "node_id": node["node_id"]}

    verdict = report.get("verdict")
    print(f"  critic verdict={verdict} status={report.get('status')}")
    print(f"  summary: {report.get('summary')}")
    for issue in report.get("issues") or []:
        print(f"    - [{issue.get('code')}] {issue.get('message')}")

    _checkpoint_scope(tree, node, thresholds_ref="state.thresholds")
    tree.mark_evaluated(node["node_id"], critic_verdict=verdict)

    if verdict == "done":
        tree.mark_passed(node["node_id"])
        tree.persist()
        return {"status": "passed", "node_id": node["node_id"], "report": report}

    if verdict == "advance":
        proposals = planner_agent.propose_children(node, report, max_proposals=1)
        if proposals and not tree.budget_exhausted():
            child = tree.advance(proposals[0], trigger_event_id=report.get("event_id"))
            EvidenceLogger.planner_adjust(
                trigger_event_id=report.get("event_id") or "unknown",
                old_strategy=node.get("strategy") or {},
                new_strategy=proposals[0],
                reason=report.get("recommendation") or "advance",
                round_num=child.get("round"),
            )
            print(f"  advance → child {child['node_id']}")
            tree.persist()
            return {"status": "advanced", "node_id": node["node_id"], "report": report}
        # Cannot deepen (budget spent / nothing new): keep this node as a lead but
        # take it off the frontier so it is not re-reviewed on the next tick.
        if node["node_id"] in tree.frontier:
            tree.frontier = [n for n in tree.frontier if n != node["node_id"]]
        if tree.active_id == node["node_id"]:
            tree.active_id = None
        print("  advance requested but cannot expand (budget); node kept as lead")
        tree.persist()
        return {"status": "advance_blocked", "node_id": node["node_id"], "report": report}

    return _backtrack_failed_node(tree, node, report)


def run_pipeline(
    dry_run: bool = True,
    max_nodes: int = 12,
    beam_width: int = 3,
    resume: bool = False,
    max_steps: int = 30,
) -> dict:
    tree_path = default_tree_path()
    if resume and tree_path.exists():
        tree = SearchTree.load(tree_path)
        tree.set_config(beam_width=beam_width, max_nodes=max_nodes)
        print(f"[orchestrator] resumed tree from {tree_path} "
              f"(nodes={tree.node_count()} active={tree.active_id})")
    else:
        tree = SearchTree(path=tree_path, beam_width=beam_width, max_nodes=max_nodes)
        root_strategy = copy.deepcopy(DEFAULT_STRATEGY)
        state = State.load()
        if state.get("design_budget"):
            root_strategy["route_mix"] = copy.deepcopy(state["design_budget"])
        tree.init_root(strategy=root_strategy, round_num=int(state.get("round") or 1))
        tree.persist()
        print(f"[orchestrator] new tree at {tree_path} "
              f"beam_width={beam_width} max_nodes={max_nodes} dry_run={dry_run}")

    history = []
    for step in range(1, max_steps + 1):
        # A live node is one still open/expanding/evaluated. run_once_node retires
        # branches it cannot expand, so when nothing live remains the search is
        # genuinely finished — no separate budget/frontier special-casing needed.
        if tree.select_active() is None:
            print("\n[orchestrator] no live node left; stopping")
            break

        print(f"\n===== step {step} =====")
        summary = run_once_node(tree, dry_run=dry_run)
        history.append(summary)
        status = summary.get("status")
        if status == "passed":
            print("\n[orchestrator] PASSED — metric clearance path found")
            break
        if status == "root_dead_end":
            print("\n[orchestrator] root dead-end — no parent to backtrack to")
            break
        if status == "stopped":
            print("\n[orchestrator] nothing left to run; stopping")
            break

    best = tree.best_leaf()
    tree.persist()
    result = {
        "steps": len(history),
        "history": history,
        "best_node": best,
        "node_count": tree.node_count(),
        "tree_path": str(tree.path),
    }
    print("\n===== summary =====")
    print(f"steps={result['steps']} nodes={result['node_count']}")
    if best:
        print(f"best={best['node_id']} status={best['status']} depth={best['depth']}")
        print(f"  branch constraints: {(best.get('strategy') or {}).get('constraints')}")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="CycPep bounded-backtrack orchestrator")
    parser.add_argument("--dry-run", action="store_true", help="Use mock research/design/predict")
    parser.add_argument("--max-nodes", type=int, default=12, help="Hard node budget M")
    parser.add_argument("--beam-width", type=int, default=3, help="Live children per parent k")
    parser.add_argument("--max-steps", type=int, default=30, help="Orchestrator loop cap")
    parser.add_argument("--resume", action="store_true", help="Load existing search_tree.json")
    args = parser.parse_args(argv)

    if not args.dry_run:
        print("NOTE: real adapters are not fully wired; prefer --dry-run for Week-1 demos.",
              file=sys.stderr)

    try:
        run_pipeline(
            dry_run=bool(args.dry_run),
            max_nodes=args.max_nodes,
            beam_width=args.beam_width,
            resume=args.resume,
            max_steps=args.max_steps,
        )
    except Exception as exc:
        EvidenceLogger.error(
            agent="planner",
            error_type=type(exc).__name__,
            message=str(exc),
            recovery="fix and re-run with --dry-run / --resume",
        )
        print(f"[orchestrator] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
