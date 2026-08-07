"""Seven-layer candidate evaluation battery.

Split from data_layer.py (PR8) so the core module stays under the
architecture-gate file-size limit. data_layer re-exports ``evaluate_battery``
and ``compute_pareto_front``; the remaining helpers are private to this module.
"""

from project_config import (
    global_value,
    required_target_ids,
    target_slug,
    target_value,
    threshold_for_target,
)

# Shared helpers come from data_layer_schema; data_layer re-exports this
# module from its tail, so battery must not import data_layer at module load
# (that would form an import-time cycle).
from data_layer_schema import alias_keys, to_float as _f


# ============================================================
# v5: 七层指标电池判定器（所有 Agent 共用）
# ============================================================
def _cmp(value: float, op: str, threshold: float) -> bool:
    """安全比较（value 对 op threshold）。值缺失返回 False。"""
    if value is None or value == "" or threshold is None:
        return False
    ops = {">": lambda a, b: a > b,
           "<": lambda a, b: a < b,
           ">=": lambda a, b: a >= b,
           "<=": lambda a, b: a <= b}
    return ops.get(op, lambda a, b: False)(float(value), float(threshold))


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def _threshold_has_value(entry: dict) -> bool:
    return bool(entry and entry.get("value") is not None)


def _threshold_is_justified(entry: dict) -> tuple[bool, str]:
    """Audit whether an entry may support final competition clearance.

    Provisional numbers remain usable for metric exploration, but they cannot
    turn ``competition_clearance`` true until evidence or calibration exists.
    """
    if not _threshold_has_value(entry):
        return False, "missing_value"
    source = str(entry.get("source") or "").strip()
    if not source:
        return False, "missing_source"
    calibration = str(entry.get("calibration_status") or "").casefold()
    if calibration in {"calibrated", "validated", "complete"}:
        return True, "calibrated"
    grade = str(entry.get("evidence_grade") or entry.get("grade") or "").casefold()
    if grade in {
        "paper_explicit", "method_explicit", "design_rule",
        "field_consensus", "positive_control", "empirical_null",
    }:
        return True, grade
    return False, grade or calibration or "ungraded"


def evaluate_battery(
    c: dict,
    thresholds: dict | None = None,
    required_targets: tuple[str, ...] | None = None,
) -> dict:
    """
    七层指标电池判定（v5 主判定）。

    导师要求（DeeCamp）：七层全清才算成功，缺一不可。每层阈值须有出处
    （来自 thresholds 由 Research Agent 文献检索+正对照标定填入）。

    参数:
      c: 候选 dict（含七层指标字段，旧名自动 alias）
      thresholds: 来自 state.json["thresholds"]
      required_targets: 本次判定必须覆盖的靶标。None 时从 project_config
                        读取；可传任意合法靶点 ID，不再限制 MDM2/MDMX。

    返回:
      {l1_pass..l7_pass: bool, all_layers_pass: bool,
       failed_layers: list[str],
       layer_values: dict,  # 每层主值 }
    """

    c, th, targets = _normalize_battery_context(c, thresholds, required_targets)

    l1 = _battery_l1(c, th)
    l2, l2_by_target = _battery_l2(c, th, targets)
    l3, l3_by_target, method_ok = _battery_l3(c, th, targets)
    l4, nc_pre, nc_post = _battery_l4(c, th)
    l5, l5_by_target = _battery_l5(c, th, targets)
    l6, l6_by_target = _battery_l6(c, th, targets)
    l7 = _battery_l7(c, th)

    layer_pass = {
        "l1_pass": bool(l1), "l2_pass": bool(l2),
        "l3_pass": bool(l3), "l4_pass": bool(l4),
        "l5_pass": bool(l5), "l6_pass": bool(l6),
        "l7_pass": bool(l7),
    }
    failed = [k for k, v in layer_pass.items() if not v]

    threshold_audit, all_thresholds_justified = _battery_threshold_audit(th, targets)
    missing_evidence = _battery_missing_evidence(c, targets)
    missing_thresholds = [
        name for name, item in threshold_audit.items() if item["reason"] == "missing_value"
    ]

    hard_failures = []
    if (
        nc_pre is not None
        and nc_post is not None
        and _threshold_has_value(th.get("L4_nc_term_dist") or {})
        and not l4
    ):
        hard_failures.append("ring_closure_geometry")

    return _build_battery_report(
        c=c,
        targets=targets,
        layer_pass=layer_pass,
        failed=failed,
        hard_failures=hard_failures,
        missing_evidence=missing_evidence,
        missing_thresholds=missing_thresholds,
        threshold_audit=threshold_audit,
        all_thresholds_justified=all_thresholds_justified,
        l2_by_target=l2_by_target,
        l3_by_target=l3_by_target,
        method_ok=method_ok,
        l5_by_target=l5_by_target,
        l6_by_target=l6_by_target,
        nc_pre=nc_pre,
        nc_post=nc_post,
    )


def _build_battery_report(
    c: dict,
    targets: tuple[str, ...],
    layer_pass: dict[str, bool],
    failed: list[str],
    hard_failures: list[str],
    missing_evidence: list[str],
    missing_thresholds: list[str],
    threshold_audit: dict,
    all_thresholds_justified: bool,
    l2_by_target: dict,
    l3_by_target: dict,
    method_ok: bool,
    l5_by_target: dict,
    l6_by_target: dict,
    nc_pre: float | None,
    nc_post: float | None,
) -> dict:
    """把七层判定结果组装为 v5 报告（纯组装，无副作用）。"""
    if hard_failures:
        triage_status = "invalid"
    elif missing_evidence or missing_thresholds:
        triage_status = "needs_more_evidence"
    elif not failed:
        triage_status = "shortlisted"
    else:
        triage_status = "needs_optimization"

    return {
        **layer_pass,
        "all_layers_pass": len(failed) == 0,
        "metric_clearance": len(failed) == 0,
        "competition_clearance": len(failed) == 0 and all_thresholds_justified,
        "all_thresholds_justified": all_thresholds_justified,
        "threshold_audit": threshold_audit,
        "triage_status": triage_status,
        "hard_failures": hard_failures,
        "missing_evidence": missing_evidence,
        "missing_thresholds": missing_thresholds,
        "failed_layers": failed,
        "required_targets": list(targets),
        "target_pass": {
            target: {
                "l2_pass": l2_by_target[target],
                "l3_pass": l3_by_target[target] and method_ok,
                "l5_pass": l5_by_target[target],
                "l6_pass": l6_by_target[target],
            }
            for target in targets
        },
        "layer_values": _build_layer_values(c, targets, nc_pre, nc_post),
    }


def _build_layer_values(
    c: dict,
    targets: tuple[str, ...],
    nc_pre: float | None,
    nc_post: float | None,
) -> dict:
    """组装七层主值（纯取值+格式化，无副作用）。"""
    return {
        "L1_plddt": _f(global_value(c, "plddt")),
        **{
            f"L2_ipsae_{target_slug(target)}": _f(target_value(c, target, "ipsae"))
            for target in targets
        },
        **{
            f"L3_dg_{target_slug(target)}": _f(target_value(c, target, "dg"))
            for target in targets
        },
        **{
            f"L3_sc_{target_slug(target)}": _f(target_value(c, target, "sc"))
            for target in targets
        },
        **{
            f"L3_dsasa_{target_slug(target)}": _f(target_value(c, target, "dsasa"))
            for target in targets
        },
        "L4_nc_distance_pre": nc_pre,
        "L4_nc_distance_post": nc_post,
        **{
            f"L5_hotspot_cov_{target_slug(target)}": _f(target_value(c, target, "hotspot_cov"))
            for target in targets
        },
        **{
            f"L6_pose_rmsd_{target_slug(target)}": _f(
                target_value(c, target, "pose_rmsd")
                if target_value(c, target, "pose_rmsd") not in (None, "")
                else c.get("pose_rmsd") if len(targets) == 1 else None
            )
            for target in targets
        },
        "L7_scrmsd": _f(global_value(c, "scrmsd")),
    }


def _normalize_battery_context(
    c: dict,
    thresholds: dict | None,
    required_targets: tuple[str, ...] | None,
) -> tuple[dict, dict, tuple[str, ...]]:
    c = alias_keys(dict(c))  # 旧名兜底
    th = thresholds or {}
    if required_targets is None:
        candidate_targets = c.get("required_targets")
        if candidate_targets:
            required_targets = tuple(candidate_targets)
        else:
            from data_layer import State  # lazy: avoid import-time cycle

            state = State.load()
            config = state.get("project_config") or State._project_config
            required_targets = required_target_ids(config)
    targets = tuple(str(target).strip() for target in required_targets if str(target).strip())
    if not targets:
        raise ValueError("required_targets must contain at least one target id")
    if len(set(target.casefold() for target in targets)) != len(targets):
        raise ValueError("required_targets contains duplicate target ids")
    return c, th, targets


def _battery_l1(c: dict, th: dict) -> bool:
    return (
        _cmp(
            _f(global_value(c, "plddt")),
            th.get("L1_plddt", {}).get("operator", ">"),
            th.get("L1_plddt", {}).get("value", 0.8),
        )
        if _threshold_has_value(th.get("L1_plddt") or {})
        else False
    )


def _battery_l2(c: dict, th: dict, targets: tuple[str, ...]) -> tuple[bool, dict]:
    t2_by_target = {
        target: threshold_for_target(th, "L2_ipsae", target) for target in targets
    }
    l2_by_target = {
        target: _cmp(
            _f(target_value(c, target, "ipsae")),
            t2_by_target[target].get("operator", ">"),
            t2_by_target[target].get("value"),
        )
        for target in targets
    }
    l2 = (
        all(_threshold_has_value(t2_by_target[target]) for target in targets)
        and all(l2_by_target.values())
    )
    return l2, l2_by_target


def _battery_l3(c: dict, th: dict, targets: tuple[str, ...]) -> tuple[bool, dict, bool]:
    l3_by_target = {}
    for target in targets:
        tdg = threshold_for_target(th, "L3_dg", target)
        tsc = threshold_for_target(th, "L3_sc", target)
        tdsasa = threshold_for_target(th, "L3_dsasa", target)
        l3_by_target[target] = all([
            _cmp(_f(target_value(c, target, "dg")), tdg.get("operator", "<"), tdg.get("value")),
            _cmp(_f(target_value(c, target, "sc")), tsc.get("operator", ">"), tsc.get("value")),
            _cmp(_f(target_value(c, target, "dsasa")), tdsasa.get("operator", ">"), tdsasa.get("value")),
        ])
    required_dg_method = th.get("L3_dg", {}).get("method")
    method_ok = (
        not required_dg_method
        or str(c.get("dg_method", "")).strip().casefold()
        == str(required_dg_method).strip().casefold()
    )
    l3 = (
        all(
            _threshold_has_value(threshold_for_target(th, key, target))
            for key in ("L3_dg", "L3_sc", "L3_dsasa")
            for target in targets
        )
        and method_ok
        and all(l3_by_target.values())
    )
    return l3, l3_by_target, method_ok


def _battery_l4(c: dict, th: dict) -> tuple[bool, object, object]:
    nc_pre = _f(global_value(c, "nc_distance_pre"))
    nc_post = _f(global_value(c, "nc_distance_post"))
    t4 = th.get("L4_nc_term_dist", {})
    l4 = (
        _threshold_has_value(t4)
        and _cmp(nc_pre, t4.get("operator", "<"), t4.get("value"))
        and _cmp(nc_post, t4.get("operator", "<"), t4.get("value"))
    )
    return l4, nc_pre, nc_post


def _battery_l5(c: dict, th: dict, targets: tuple[str, ...]) -> tuple[bool, dict]:
    l5_by_target = {}
    for target in targets:
        t5 = threshold_for_target(th, "L5_hotspot_coverage", target)
        specific_site = target_value(c, target, "site_consistency")
        if specific_site in (None, "") and len(targets) == 1:
            specific_site = c.get("site_consistency")
        l5_by_target[target] = (
            _cmp(
                _f(target_value(c, target, "hotspot_cov")),
                t5.get("operator", ">="),
                t5.get("value"),
            )
            and _truthy(specific_site)
        )
    l5 = (
        all(
            _threshold_has_value(threshold_for_target(th, "L5_hotspot_coverage", target))
            for target in targets
        )
        and all(l5_by_target.values())
    )
    return l5, l5_by_target


def _battery_l6(c: dict, th: dict, targets: tuple[str, ...]) -> tuple[bool, dict]:
    l6_by_target = {}
    for target in targets:
        t6 = threshold_for_target(th, "L6_pose_rmsd", target)
        min_seed_fraction = t6.get("min_seed_fraction", 0.67)
        pose = target_value(c, target, "pose_rmsd")
        seed = target_value(c, target, "seed_convergence")
        if len(targets) == 1:
            pose = pose if pose not in (None, "") else c.get("pose_rmsd")
            seed = seed if seed not in (None, "") else c.get("seed_convergence")
        l6_by_target[target] = (
            _cmp(_f(pose), t6.get("operator", "<"), t6.get("value"))
            and _f(seed) is not None
            and _f(seed) >= float(min_seed_fraction)
        )
    l6 = (
        all(
            _threshold_has_value(threshold_for_target(th, "L6_pose_rmsd", target))
            for target in targets
        )
        and all(l6_by_target.values())
    )
    return l6, l6_by_target


def _battery_l7(c: dict, th: dict) -> bool:
    t7 = th.get("L7_scrmsd", {})
    return (
        _cmp(_f(global_value(c, "scrmsd")), t7.get("operator", "<"), t7.get("value"))
        if _threshold_has_value(t7)
        else False
    )


def _battery_threshold_audit(th: dict, targets: tuple[str, ...]) -> tuple[dict, bool]:
    threshold_audit = {}
    for key in ("L1_plddt", "L4_nc_term_dist", "L7_scrmsd"):
        ok, reason = _threshold_is_justified(th.get(key) or {})
        threshold_audit[key] = {"justified": ok, "reason": reason}
    for key in ("L2_ipsae", "L3_dg", "L3_sc", "L3_dsasa", "L5_hotspot_coverage", "L6_pose_rmsd"):
        for target in targets:
            ok, reason = _threshold_is_justified(threshold_for_target(th, key, target))
            threshold_audit[f"{key}:{target}"] = {"justified": ok, "reason": reason}
    return threshold_audit, all(item["justified"] for item in threshold_audit.values())


def _battery_missing_evidence(c: dict, targets: tuple[str, ...]) -> list[str]:
    missing_evidence = []
    global_required = {
        "plddt": global_value(c, "plddt"),
        "nc_distance_pre": global_value(c, "nc_distance_pre"),
        "nc_distance_post": global_value(c, "nc_distance_post"),
        "scrmsd": global_value(c, "scrmsd"),
    }
    missing_evidence.extend(name for name, value in global_required.items() if value in (None, ""))
    for target in targets:
        for metric in (
            "ipsae", "dg", "sc", "dsasa", "hotspot_cov",
            "site_consistency", "pose_rmsd", "seed_convergence",
        ):
            value = target_value(c, target, metric)
            if value in (None, "") and not (
                len(targets) == 1
                and metric in {"site_consistency", "pose_rmsd", "seed_convergence"}
                and c.get(metric) not in (None, "")
            ):
                missing_evidence.append(f"{target}:{metric}")
    return missing_evidence

def compute_pareto_front(
    candidates: list[dict],
    objectives: tuple = ("ipsae_mdm2", "ipsae_mdmx"),
) -> list[str]:
    """Return non-dominated candidates for mixed-direction objectives.

    Backward-compatible strings mean ``maximize``. A generic objective may be
    ``{"target": "NEW1", "metric": "dg", "direction": "minimize"}`` or
    ``{"key": "scrmsd", "direction": "minimize"}``.
    """
    specs = []
    for objective in objectives:
        if isinstance(objective, str):
            specs.append({"key": objective, "direction": "maximize"})
        elif isinstance(objective, dict):
            direction = objective.get("direction", "maximize")
            if direction not in {"maximize", "minimize"}:
                raise ValueError(f"invalid Pareto direction: {direction}")
            if not objective.get("key") and not (objective.get("target") and objective.get("metric")):
                raise ValueError("Pareto objective requires key or target+metric")
            specs.append(dict(objective, direction=direction))
        else:
            raise TypeError("Pareto objectives must be strings or dictionaries")

    def objective_value(candidate: dict, spec: dict):
        if spec.get("target"):
            value = target_value(candidate, spec["target"], spec["metric"])
        else:
            value = candidate.get(spec["key"])
        number = _f(value)
        if number is None:
            return None
        return -number if spec["direction"] == "minimize" else number

    valid = []
    for candidate in candidates:
        values = tuple(objective_value(candidate, spec) for spec in specs)
        if candidate.get("candidate_id") and all(value is not None for value in values):
            valid.append((candidate["candidate_id"], values))

    front = []
    for candidate_id, values in valid:
        dominated = any(
            all(other >= current for other, current in zip(other_values, values))
            and any(other > current for other, current in zip(other_values, values))
            for other_id, other_values in valid
            if other_id != candidate_id
        )
        if not dominated:
            front.append(candidate_id)
    return front
