"""Failure experience store (B 组: 失败经验库闭环).

聚合 evidence_log 中的 ``battery_evaluated`` 淘汰原因，输出可执行的生成偏好。
设计上保持保守：证据不足时不输出任何建议；Design 每轮最多应用一条偏好，
且只在调用方未显式指定对应参数时生效。
"""
from __future__ import annotations

import math

from data_layer import EvidenceLogger
from peptide_contract import MAX_CYCLIC_PEPTIDE_LENGTH, MIN_CYCLIC_PEPTIDE_LENGTH

EVENT_BATTERY = "battery_evaluated"
EVENT_EXPERIENCE = "experience_applied"

_LAYER_PREFIX = {
    "L1": "l1_pass",
    "L2": "l2_pass",
    "L3": "l3_pass",
    "L4": "l4_pass",
    "L5": "l5_pass",
    "L6": "l6_pass",
    "L7": "l7_pass",
}


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


def _length_key(value):
    """Normalize a length value to an int; None when not an integer."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return None
    if n % 2 == 1:
        return ordered[n // 2]
    return (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0


def _layer_of_metric(metric_key):
    for prefix, layer in _LAYER_PREFIX.items():
        if metric_key.startswith(f"{prefix}_"):
            return layer
    return None


def _row_payload(row):
    raw = row.get("payload")
    return raw if isinstance(raw, dict) else row


def _row_targets(row, payload):
    targets = row.get("targets")
    if not targets:
        targets = payload.get("targets") if isinstance(payload, dict) else None
    if not targets:
        return ()
    if isinstance(targets, str):
        return (targets,)
    return tuple(str(item) for item in targets)


def _target_match(row, targets):
    """True when a row is evidence for any requested target.

    Rows without target context are never claimed for a specific target;
    ``targets=None`` (no filter) keeps every row.
    """
    if not targets:
        return True
    payload = _row_payload(row)
    row_targets = _row_targets(row, payload)
    if not row_targets:
        return False
    return bool(set(row_targets) & set(targets))


def _battery_events(events=None, targets=None):
    """Return ``battery_evaluated`` rows; never raises on a missing backend."""
    if events is not None:
        rows = [e for e in events if e.get("event_type") == EVENT_BATTERY]
    else:
        get_all = getattr(EvidenceLogger, "get_all", None)
        if get_all is None:
            return []
        try:
            rows = [e for e in get_all() if e.get("event_type") == EVENT_BATTERY]
        except Exception:
            # 证据后端不可读时按"无证据"处理，不阻断设计（有意降级）
            return []
    return [row for row in rows if _target_match(row, targets)]


def summarize_failures(events=None, targets=None) -> dict:
    """Aggregate battery verdicts into per-layer / per-length / per-metric stats.

    ``targets`` restricts aggregation to events carrying at least one of the
    given target IDs so one target's failures cannot change another target's
    preference (P2-4).

    返回结构:
      n_evaluated / n_passed / n_failed
      failed_layers: {layer_key: count}
      triage_status: {status: count}
      lengths: {str(length): {"n": int, "failed": int}}
      metrics: {metric_key: {"layer": str, "n_failed": int, "n_passed": int,
                             "median_failed": float|None, "median_passed": float|None}}
    """
    rows = _battery_events(events, targets=targets)
    n_passed = 0
    n_failed = 0
    layer_counts = {}
    triage_counts = {}
    length_stats = {}
    metric_failed = {}
    metric_passed = {}
    for row in rows:
        payload = _row_payload(row)
        passed = bool(payload.get("passed"))
        if passed:
            n_passed += 1
        else:
            n_failed += 1
        status = payload.get("triage_status")
        triage_counts[status] = triage_counts.get(status, 0) + 1

        length = _length_key(payload.get("length"))
        if length is not None:
            key = str(length)
            stat = length_stats.setdefault(key, {"n": 0, "failed": 0})
            stat["n"] += 1
            if not passed:
                stat["failed"] += 1

        failed_layers = payload.get("failed_layers") or []
        for layer in failed_layers:
            layer_counts[layer] = layer_counts.get(layer, 0) + 1

        layer_values = payload.get("layer_values") or {}
        for metric_key, raw in layer_values.items():
            value = _as_float(raw)
            if value is None:
                continue
            layer = _layer_of_metric(metric_key)
            if layer is None:
                continue
            if layer in failed_layers:
                metric_failed.setdefault(metric_key, []).append(value)
            else:
                metric_passed.setdefault(metric_key, []).append(value)

    metrics = {}
    for key in sorted(set(metric_failed) | set(metric_passed)):
        metrics[key] = {
            "layer": _layer_of_metric(key),
            "n_failed": len(metric_failed.get(key, [])),
            "n_passed": len(metric_passed.get(key, [])),
            "median_failed": _median(metric_failed.get(key, [])),
            "median_passed": _median(metric_passed.get(key, [])),
        }

    return {
        "n_evaluated": len(rows),
        "n_passed": n_passed,
        "n_failed": n_failed,
        "failed_layers": layer_counts,
        "triage_status": triage_counts,
        "lengths": length_stats,
        "metrics": metrics,
    }


def suggest_length_preference(summary: dict, min_failures: int = 5) -> dict | None:
    """Emit a conservative length preference from failure statistics.

    规则：仅考虑评估数 >= min_failures 的长度；若最差长度失败率 >= 70% 且
    存在失败率 <= 30% 的更好长度，则建议迁移到更好长度；否则返回 None。
    """
    stats = summary.get("lengths") or {}
    rated = []
    for key, stat in stats.items():
        n = _as_float(stat.get("n"))
        failed = _as_float(stat.get("failed"))
        if n is None or failed is None or n < min_failures or n <= 0:
            continue
        rated.append((key, failed / n))
    if len(rated) < 2:
        return None
    best_key, best_rate = min(rated, key=lambda item: item[1])
    worst_key, worst_rate = max(rated, key=lambda item: item[1])
    if best_key == worst_key or best_rate == worst_rate:
        return None
    if worst_rate >= 0.7 and best_rate <= 0.3:
        best_length = _length_key(best_key)
        if (
            best_length is None
            or best_length < MIN_CYCLIC_PEPTIDE_LENGTH
            or best_length > MAX_CYCLIC_PEPTIDE_LENGTH
        ):
            return None
        reason = (
            f"length {worst_key} failed {worst_rate:.0%} of evaluated candidates "
            f"while {best_key} failed only {best_rate:.0%}; shift generation to "
            f"{best_length}"
        )
        return {"lengths": [best_length], "reason": reason}
    return None


def _validated_lengths(lengths):
    """Keep only lengths inside the supported cyclic-peptide range."""
    result = []
    for value in lengths:
        length = _length_key(value)
        if (
            length is not None
            and MIN_CYCLIC_PEPTIDE_LENGTH <= length <= MAX_CYCLIC_PEPTIDE_LENGTH
        ):
            result.append(length)
    return sorted(set(result))


def _record_applied(old_lengths, hint, summary, targets=None):
    logger = getattr(EvidenceLogger, "log", None)
    if logger is None:
        return
    try:
        logger(
            "design", EVENT_EXPERIENCE, {
                "preference": "lengths",
                "old_lengths": old_lengths,
                "new_lengths": hint["lengths"],
                "reason": hint["reason"],
                "evidence": {
                    "n_evaluated": summary["n_evaluated"],
                    "n_failed": summary["n_failed"],
                },
            },
            targets=targets,
            phase="design",
        )
    except Exception:
        # 证据不可写时降级为不记录，不阻断生成（有意降级）
        pass


def record_applied_preference(old_lengths, hint, summary=None, targets=None):
    """Record ``experience_applied`` after the config was merged successfully.

    Keeping the audit event on the success path guarantees the ledger only
    claims preferences that actually took effect (P2-5).
    """
    if summary is None:
        summary = summarize_failures(targets=targets)
    _record_applied(old_lengths, hint, summary, targets=targets)


def consume_experience_preference(targets=None, min_failures: int = 5):
    """Return (lengths, hint) for task construction; (None, None) without an
    evidence-backed preference.  Used by the Planner when the approved target
    config does not pin explicit lengths."""
    summary = summarize_failures(targets=targets)
    hint = suggest_length_preference(summary, min_failures=min_failures)
    if hint is None:
        return None, None
    validated = _validated_lengths(hint["lengths"])
    if not validated:
        return None, None
    return validated, hint


def apply_experience_preference(design_config=None, target_spec=None,
                                targets=None, min_failures: int = 5):
    """Return (design_config, hint) for the Design entry point.

    只在调用方未显式指定 lengths（design_config 或 target_spec 均检查）且
    证据充分时调整；显式参数永远优先。调用方在 merge 成功后通过
    ``record_applied_preference`` 记账，保证闭环可审计。证据不足或环境
    缺失时原样返回，不改变行为。
    """
    dc = dict(design_config or {})
    ts = target_spec or {}
    if dc.get("lengths") or ts.get("lengths"):
        return dc, None
    if targets is None and ts.get("target_name"):
        targets = [ts["target_name"]]
    summary = summarize_failures(targets=targets)
    hint = suggest_length_preference(summary, min_failures=min_failures)
    if hint is None:
        return dc, None
    validated = _validated_lengths(hint["lengths"])
    if not validated:
        return dc, None
    dc["lengths"] = validated
    return dc, hint