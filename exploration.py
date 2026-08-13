"""Exploration shortlist (P0-E: Tournament / Pareto exploration shortlist).

在七层 hard clearance 之外提供连续 soft desirability 与多目标 Pareto
探索 shortlist：一批候选全灭时，仍能基于证据给出下一轮最值得探索的
候选及理由。科学语义红线：shortlist 永远不是 scientific pass；本模块
只读写 Evidence（append-only），不触碰 State / CandidateIndex / 事务边界。

输入语义（OpenSpec exploration-shortlist spec）：该 targets 下累计全部
``battery_evaluated`` 证据；shortlist 事件的轮次由 envelope ``round`` 承担。
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone

from contracts.event import EvidenceEvent
from data_layer import EvidenceLogger, State, compute_pareto_front
from contracts.trace import TraceContext
from project_config import target_slug, threshold_for_target
from threshold_calibration import METRIC_SPECS
from threshold_contract import normalize_thresholds

EVENT_BATTERY = "battery_evaluated"
EVENT_SHORTLIST = "exploration_shortlist"

# layer_values 键前缀 → METRIC_SPECS 规范指标键。前缀匹配以 "_" 为界，
# 最长前缀优先（如 L3_dsasa 先于 L3_dg 命中各自的键）。
_LAYER_KEY_TO_METRIC = {
    "L1_plddt": "L1_plddt",
    "L2_ipsae": "L2_ipsae",
    "L3_dg": "L3_dg",
    "L3_sc": "L3_sc",
    "L3_dsasa": "L3_dsasa",
    "L4_nc_distance": "L4_nc_term_dist",
    "L5_hotspot_cov": "L5_hotspot_coverage",
    "L6_pose_rmsd": "L6_pose_rmsd",
    "L7_scrmsd": "L7_scrmsd",
}

_CALIBRATED_STATUSES = {"calibrated", "validated", "complete"}


def _as_float(value):
    """Parse one metric value; NaN/Inf and non-numeric input become None."""
    try:
        if value is None or value == "":
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def _split_layer_key(layer_key):
    """Split ``layer_values`` key into (METRIC_SPECS metric key, slug suffix).

    靶标维度键（如 ``L2_ipsae_mdm2``）的 slug 后缀用于解析 per-target
    threshold override；全局键（如 ``L4_nc_distance_post``）的 suffix
    不是靶标 slug，不会命中任何 target，自然回落到 base 条目。
    """
    for prefix in sorted(_LAYER_KEY_TO_METRIC, key=len, reverse=True):
        if layer_key == prefix:
            return _LAYER_KEY_TO_METRIC[prefix], None
        if layer_key.startswith(f"{prefix}_"):
            return _LAYER_KEY_TO_METRIC[prefix], layer_key[len(prefix) + 1:]
    return None, None


def _margin(value: float, entry: dict | None):
    """Normalized distance-to-threshold margin in [-1, 1]; None when unusable.

    operator 决定方向（P0-C 接口契约：取值集合 ">"|">="|"<"|"<="）。
    threshold 为 0 时退化为符号判定：满足 operator（含严格性）记 0，否则 -1。
    """
    if not isinstance(entry, dict):
        return None
    threshold = _as_float(entry.get("value"))
    operator = entry.get("operator")
    if threshold is None or operator not in (">", ">=", "<", "<="):
        return None
    if threshold == 0:
        satisfied = {
            ">": value > 0, ">=": value >= 0,
            "<": value < 0, "<=": value <= 0,
        }[operator]
        return 0.0 if satisfied else -1.0
    if operator in (">", ">="):
        margin = (value - threshold) / abs(threshold)
    else:
        margin = (threshold - value) / abs(threshold)
    return max(-1.0, min(1.0, margin))


def _load_thresholds(thresholds=None) -> dict:
    """Normalize thresholds; None loads from State, unreadable backend → {}."""
    raw = thresholds
    if raw is None:
        try:
            raw = (State.load() or {}).get("thresholds")
        except Exception:
            # 阈值后端不可读时按"无阈值"处理，shortlist 退化为 Pareto/保守输出
            # （与 experience.py 同一文档化降级约定）
            raw = {}
    normalized, _audit = normalize_thresholds(raw)
    return normalized


def _resolve_entry(thresholds: dict, metric: str, slug, slug_to_target: dict):
    """Resolve the threshold entry, honoring per-target overrides.

    与 battery 侧同一解析器（project_config.threshold_for_target）：P0-C
    标定的主产物就是 target 级 override，base 条目可能是 provisional。
    """
    target = slug_to_target.get(slug) if slug else None
    if target is not None:
        return threshold_for_target(thresholds, metric, target), target
    return thresholds.get(metric), None


def desirability(layer_values: dict, thresholds: dict, target_ids=()):
    """Return (score, top_margin_metric, margins, consumed) for one candidate.

    score 为各可计算指标 margin 的均值；无可计算指标时 score 为 None
    （不伪造分数）。consumed 为 (metric, target, calibration_status) 列表，
    供批次级 calibration 汇总。
    """
    slug_to_target = {target_slug(t): t for t in target_ids or ()}
    margins = {}
    consumed = []
    for layer_key, raw in (layer_values or {}).items():
        metric, slug = _split_layer_key(layer_key)
        if metric is None:
            continue
        value = _as_float(raw)
        if value is None:
            continue
        entry, target = _resolve_entry(thresholds, metric, slug, slug_to_target)
        margin = _margin(value, entry)
        if margin is None:
            continue
        margins[layer_key] = margin
        consumed.append((
            metric, target,
            entry.get("calibration_status") if isinstance(entry, dict) else None,
        ))
    if not margins:
        return None, None, {}, []
    top_metric = max(margins, key=lambda key: margins[key])
    return sum(margins.values()) / len(margins), top_metric, margins, consumed


def _battery_rows(events=None, targets=None) -> list:
    """Return ``battery_evaluated`` rows for targets; never raises on backend.

    同一 candidate_id 跨轮重评估会产生多行：保留最新一行（日志序即时间序），
    避免 Pareto 支配比较在同 id 行间失效、shortlist 出现重复候选。
    """
    if events is not None:
        rows = [e for e in events if e.get("event_type") == EVENT_BATTERY]
    else:
        get_all = getattr(EvidenceLogger, "get_all", None)
        if get_all is None:
            return []
        try:
            rows = [e for e in get_all() if e.get("event_type") == EVENT_BATTERY]
        except Exception:
            # 证据后端不可读时按"无证据"处理（与 experience.py 同一降级约定）
            return []
    if targets:
        wanted = set(targets)
        rows = [row for row in rows if wanted & set(row.get("targets") or [])]
    by_id = {}
    anonymous = []
    for row in rows:
        candidate_id = row.get("candidate_id")
        if candidate_id:
            by_id[candidate_id] = row
        else:
            anonymous.append(row)
    return [*by_id.values(), *anonymous]


def _calibration_summary(consumed) -> dict:
    """Bucket the consumed (metric, target) threshold entries by status."""
    summary = {"calibrated": 0, "provisional": 0, "unavailable": 0}
    for _metric, _target, status in sorted(set(consumed)):
        if status in _CALIBRATED_STATUSES:
            summary["calibrated"] += 1
        elif status == "unavailable":
            summary["unavailable"] += 1
        else:
            summary["provisional"] += 1
    return summary


def exploration_shortlist(events=None, targets=None, k: int = 5, thresholds=None) -> dict:
    """Compute the top-k exploration shortlist from battery evidence.

    合成规则（design D4）：Pareto front 成员优先（内部按 desirability 降序），
    其余按 desirability 降序补足 k，desirability 为 None 的候选最后兜底。
    所有入选者的 passed 与其 battery 原始判定一致——shortlist 不是 pass。
    """
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        raise ValueError("k must be a positive integer")
    rows = _battery_rows(events, targets=targets)
    normalized = _load_thresholds(thresholds)

    candidates = []
    consumed = []
    unmapped = set()
    for row in rows:
        layer_values = row.get("layer_values") or {}
        unmapped.update(
            key for key in layer_values if _split_layer_key(key)[0] is None
        )
        score, top_metric, _margins, row_consumed = desirability(
            layer_values, normalized, target_ids=row.get("targets") or ()
        )
        consumed.extend(row_consumed)
        candidates.append({
            "candidate_id": row.get("candidate_id"),
            "passed": bool(row.get("passed")),
            "desirability": score,
            "top_margin_metric": top_metric,
            "pareto_values": {
                key: _as_float(layer_values.get(key))
                for key in layer_values
                if _split_layer_key(key)[0] is not None
            },
        })

    # Pareto front：方向来自 METRIC_SPECS（design D3），不需要阈值数值。
    objective_keys = sorted({
        key for candidate in candidates for key in candidate["pareto_values"]
    })
    objectives = [
        {"key": key,
         "direction": METRIC_SPECS[_split_layer_key(key)[0]]["direction"]}
        for key in objective_keys
    ]
    front_ids = set()
    if objectives:
        front_ids = set(compute_pareto_front([
            {"candidate_id": c["candidate_id"], **c["pareto_values"]}
            for c in candidates
        ], objectives))

    def _rank(entry):
        # desirability 降序；None 排最后
        score = entry["desirability"]
        return (score is not None, score if score is not None else 0.0)

    front = sorted(
        (c for c in candidates if c["candidate_id"] in front_ids),
        key=_rank, reverse=True,
    )
    rest = sorted(
        (c for c in candidates if c["candidate_id"] not in front_ids),
        key=_rank, reverse=True,
    )

    shortlist = []
    for entry in front[:k]:
        shortlist.append({
            "candidate_id": entry["candidate_id"],
            "passed": entry["passed"],
            "desirability": entry["desirability"],
            "pareto_front": True,
            "reason": "pareto_front",
            "top_margin_metric": entry["top_margin_metric"],
        })
    for entry in rest:
        if len(shortlist) >= k:
            break
        shortlist.append({
            "candidate_id": entry["candidate_id"],
            "passed": entry["passed"],
            "desirability": entry["desirability"],
            "pareto_front": False,
            "reason": (
                "desirability_rank"
                if entry["desirability"] is not None
                else "partial_evidence"
            ),
            "top_margin_metric": entry["top_margin_metric"],
        })

    return {
        "k": k,
        "n_evaluated": len(rows),
        "n_passed": sum(1 for row in rows if row.get("passed")),
        "shortlist": shortlist,
        "source_event_ids": [
            row["event_id"] for row in rows if row.get("event_id")
        ],
        "calibration": _calibration_summary(consumed),
        "unmapped_metrics": sorted(unmapped),
    }


def build_exploration_shortlist_event(
    result: dict,
    targets=None,
    round_num=None,
    trace_context=None,
) -> dict:
    """Materialize one validated shortlist event without publishing it."""
    if trace_context is not None and not isinstance(trace_context, TraceContext):
        trace_context = TraceContext.from_dict(trace_context)
    return EvidenceEvent(
        timestamp=datetime.now(timezone.utc).isoformat(),
        event_id=str(uuid.uuid4())[:12],
        agent="critic",
        event_type=EVENT_SHORTLIST,
        payload=dict(result or {}),
        trace_context=trace_context,
        phase="critic",
        round_num=round_num,
        targets=tuple(targets) if targets else None,
    ).to_dict()


def record_exploration_shortlist(result: dict, targets=None, round_num=None,
                                 trace_context=None, store=None):
    """Append one ``exploration_shortlist`` evidence event; return event_id.

    targets/round/trace 只走正式 envelope（design D1）：payload 不含 targets；
    属于正式 run 的生成方必须传 trace_context。
    """
    entry = build_exploration_shortlist_event(
        result,
        targets=targets,
        round_num=round_num,
        trace_context=trace_context,
    )
    EvidenceLogger._write(entry, store=store)
    return entry["event_id"]
