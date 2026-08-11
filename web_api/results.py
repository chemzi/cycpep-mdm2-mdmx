"""Frontend V2 results digest: an honest, demo-ready results view.

Computed purely from the formal read model (candidates + battery evidence +
state thresholds). Never fabricates a score: NaN/Inf layer values are treated
as missing, and a candidate without a battery row is listed as pending rather
than passed. The ``data_basis`` field and the generated conclusion make the
synthetic-vs-real distinction explicit so the page can never be mistaken for
a final scientific result.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from exploration import exploration_shortlist, split_layer_key
from project_config import target_slug, threshold_for_target
from threshold_calibration import METRIC_SPECS
from threshold_contract import normalize_thresholds

CALIBRATED_STATUSES = frozenset({"calibrated", "validated", "complete"})
RESULTS_SCHEMA_VERSION = "frontend.results.v1"
DEFAULT_SHORTLIST_K = 5


def _finite(value: Any) -> float | None:
    """Parse one metric value; NaN/Inf and non-numeric input become None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _threshold_bucket(entry: Mapping[str, Any] | None) -> str:
    if not isinstance(entry, dict):
        return "unavailable"
    status = str(entry.get("calibration_status") or "unavailable")
    if status in CALIBRATED_STATUSES:
        return "calibrated"
    if status == "unavailable":
        return "unavailable"
    return "provisional"


def _threshold_view(entry: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    value = _finite(entry.get("value"))
    if value is None or not entry.get("operator"):
        return {
            "calibration_status": _threshold_bucket(entry),
            "value": None,
            "operator": None,
        }
    return {
        "calibration_status": _threshold_bucket(entry),
        "value": value,
        "operator": str(entry.get("operator")),
    }


class ResultsReader:
    """Compute a results digest over the formal read model."""

    def __init__(
        self,
        store,
        *,
        thresholds: Mapping[str, Any] | None = None,
        shortlist_k: int = DEFAULT_SHORTLIST_K,
    ):
        self._store = store
        self._thresholds = thresholds
        self._shortlist_k = shortlist_k

    def _state(self) -> dict[str, Any]:
        return self._store.get_state(self._store.project_id)

    def _candidates(self) -> list[dict[str, Any]]:
        project_id = self._store.project_id
        return [
            value for value in self._store.list()
            if value.get("project_id") in (None, project_id)
        ]

    def _battery_rows(self) -> list[dict[str, Any]]:
        return [
            event for event in self._store.query(project_id=self._store.project_id)
            if event.get("event_type") == "battery_evaluated"
        ]

    def _normalized_thresholds(self) -> dict[str, dict]:
        if self._thresholds is not None:
            normalized, _audit = normalize_thresholds(self._thresholds)
            return normalized
        normalized, _audit = normalize_thresholds((self._state() or {}).get("thresholds"))
        return normalized

    @staticmethod
    def _target_ids(state: Mapping[str, Any]) -> list[str]:
        raw = state.get("targets") or {}
        if isinstance(raw, dict):
            return sorted(raw)
        if isinstance(raw, list):
            return sorted({str(item.get("id") or "") for item in raw if isinstance(item, dict) and item.get("id")})
        return []

    @staticmethod
    def _threshold_summary(normalized: Mapping[str, dict], calibration: Mapping[str, Any]) -> dict[str, Any]:
        """Headline threshold counts come from the exploration shortlist digest
        (the metrics actually consumed by evaluated candidates), so the results
        page and the workbench shortlist panel always agree."""
        keys = sorted(normalized)
        return {
            "counts": {
                "calibrated": int(calibration.get("calibrated") or 0),
                "provisional": int(calibration.get("provisional") or 0),
                "unavailable": int(calibration.get("unavailable") or 0),
            },
            "keys": keys,
            "metrics_covered": sorted(set(keys) & set(METRIC_SPECS)),
        }

    @staticmethod
    def _layer_flag(metric: str) -> str:
        """Battery pass-flag name for a metric key (L2_ipsae -> l2_pass)."""
        return str(metric).split("_", 1)[0].lower() + "_pass"

    def _layer_stats(
        self,
        normalized: Mapping[str, dict],
        latest: Mapping[str, dict],
        target_ids: list[str],
    ) -> list[dict[str, Any]]:
        slug_to_target = {target_slug(target): target for target in target_ids}
        stats: list[dict[str, Any]] = []
        for metric, spec in METRIC_SPECS.items():
            flag = self._layer_flag(metric)
            evaluated = 0
            passed = 0
            per_target: dict[str, dict[str, int]] = {}
            for row in latest.values():
                layer_values = row.get("layer_values") or {}
                failed = set(row.get("failed_layers") or [])
                target_pass = row.get("target_pass") if isinstance(row.get("target_pass"), Mapping) else {}
                keys = [
                    key for key, value in layer_values.items()
                    if split_layer_key(key)[0] == metric and _finite(value) is not None
                ]
                if not keys:
                    continue
                evaluated += 1
                if all(self._layer_key_passed(key, flag, failed, target_pass, slug_to_target, spec["scope"]) for key in keys):
                    passed += 1
                if spec["scope"] == "target":
                    for key in keys:
                        _base, slug = split_layer_key(key)
                        bucket = per_target.setdefault(slug or "?", {"evaluated": 0, "passed": 0})
                        bucket["evaluated"] += 1
                        if self._layer_key_passed(key, flag, failed, target_pass, slug_to_target, spec["scope"]):
                            bucket["passed"] += 1
            base = normalized.get(metric)
            per_target_thresholds: list[dict[str, Any]] = []
            if spec["scope"] == "target":
                for target in target_ids:
                    entry = threshold_for_target(normalized, metric, target)
                    if isinstance(entry, dict) and entry.get("calibration_status"):
                        per_target_thresholds.append({
                            "target": target,
                            **dict(_threshold_view(entry).items()),
                        })
            stats.append({
                "key": metric,
                "metric": spec["metric"],
                "direction": spec["direction"],
                "scope": spec["scope"],
                "evaluated": evaluated,
                "passed": passed,
                "pass_rate": (passed / evaluated) if evaluated else None,
                "threshold": _threshold_view(base),
                "per_target": [
                    {"target": target, "evaluated": bucket["evaluated"], "passed": bucket["passed"],
                     "pass_rate": (bucket["passed"] / bucket["evaluated"]) if bucket["evaluated"] else None}
                    for target, bucket in sorted(per_target.items())
                ],
                "per_target_thresholds": per_target_thresholds,
            })
        return stats

    @staticmethod
    def _layer_key_passed(
        key: str,
        flag: str,
        failed: set,
        target_pass: Mapping[str, Any],
        slug_to_target: Mapping[str, str],
        scope: str,
    ) -> bool:
        """Per-layer pass decision, honoring per-target battery results when present."""
        if scope == "target":
            _base, slug = split_layer_key(key)
            target = slug_to_target.get(slug) if slug else None
            if target and isinstance(target_pass, Mapping):
                entry = target_pass.get(target)
                if isinstance(entry, Mapping) and flag in entry:
                    return bool(entry[flag])
        return flag not in failed

    def read(self, *, limit: int = 50) -> dict[str, Any]:
        state = self._state()
        project_id = str(self._store.project_id)
        candidates = self._candidates()
        battery = self._battery_rows()
        normalized = self._normalized_thresholds()
        target_ids = self._target_ids(state)

        candidates_by_id = {str(candidate.get("candidate_id")): candidate for candidate in candidates}

        # Evidence order is time order; keep the newest battery row per candidate.
        latest: dict[str, dict] = {}
        for row in battery:
            candidate_id = row.get("candidate_id")
            if candidate_id:
                latest[str(candidate_id)] = row

        evaluated_ids = set(latest)
        # Hard clearance must be backed by layer evidence for every metric:
        # a battery row that claims passed while missing layer_values for any
        # METRIC_SPECS key (e.g. a hand-written demo row with no L3) is not
        # counted as cleared. Genuine evaluate_battery rows always carry all
        # nine layer keys, so this only rejects incomplete/fabricated rows.
        required_layer_keys = set(METRIC_SPECS)
        hard_cleared_ids = {
            candidate_id
            for candidate_id, row in latest.items()
            if row.get("passed")
            and required_layer_keys.issubset({
                split_layer_key(key)[0] for key in (row.get("layer_values") or {})
            })
        }
        pending_ids = set(candidates_by_id) - evaluated_ids

        shortlist = exploration_shortlist(
            events=list(battery), targets=target_ids or None, k=self._shortlist_k,
            thresholds=normalized,
        )
        pareto_ids = {
            str(item.get("candidate_id"))
            for item in shortlist.get("shortlist") or []
            if item.get("pareto_front")
        }
        desirability_by_id = {
            str(item.get("candidate_id")): item.get("desirability")
            for item in shortlist.get("shortlist") or []
        }

        finalists: list[dict[str, Any]] = []
        for candidate_id in sorted(evaluated_ids):
            candidate = candidates_by_id.get(candidate_id, {})
            row = latest[candidate_id]
            metrics = candidate.get("metrics") or candidate.get("metrics_json") or {}
            if not isinstance(metrics, Mapping):
                metrics = {}
            finalists.append({
                "candidate_id": candidate_id,
                "sequence": candidate.get("sequence"),
                "status": candidate.get("status") or candidate.get("final_status"),
                "source_route": candidate.get("source_route"),
                "hard_cleared": candidate_id in hard_cleared_ids,
                "failed_layers": list(row.get("failed_layers") or []),
                "desirability": desirability_by_id.get(candidate_id),
                "pareto_front": candidate_id in pareto_ids,
                "top_margin_metric": next(
                    (item.get("top_margin_metric") for item in shortlist.get("shortlist") or []
                     if str(item.get("candidate_id")) == candidate_id),
                    None,
                ),
                "targets": list(row.get("targets") or []),
                "metrics": {key: value for key, value in metrics.items() if isinstance(metrics, Mapping)},
                "battery_event_id": row.get("event_id"),
                "battery_timestamp": row.get("timestamp"),
            })
        def _desirability_key(item: dict) -> float:
            score = item.get("desirability")
            return score if score is not None else -math.inf
        finalists.sort(
            key=lambda item: (item["hard_cleared"], _desirability_key(item)),
            reverse=True,
        )
        for index, item in enumerate(finalists, start=1):
            item["rank"] = index

        orchestrator = state.get("orchestrator") or {}
        run = None
        if isinstance(orchestrator, dict) and orchestrator.get("run_id"):
            run = {
                "run_id": orchestrator.get("run_id"),
                "workflow_id": orchestrator.get("workflow_id"),
                "plan_id": orchestrator.get("plan_id"),
                "status": orchestrator.get("status"),
            }

        data_basis = "none"
        if battery or candidates:
            data_basis = "demo_fixture" if any(
                row.get("demo_fixture") for row in battery
            ) or any(candidate.get("demo_fixture") for candidate in candidates) else "real"

        threshold_summary = self._threshold_summary(normalized, shortlist.get("calibration") or {})
        layer_stats = self._layer_stats(normalized, latest, target_ids)
        summary = {
            "candidates_total": len(candidates),
            "candidates_evaluated": len(evaluated_ids),
            "candidates_pending_prediction": len(pending_ids),
            "hard_cleared": len(hard_cleared_ids),
            "hard_clearance_rate": (len(hard_cleared_ids) / len(evaluated_ids)) if evaluated_ids else None,
            "n_shortlisted": len(shortlist.get("shortlist") or []),
            "n_pareto_front": len(pareto_ids),
            "layers_total": len(METRIC_SPECS),
            "layers_evaluated": sum(1 for layer in layer_stats if layer["evaluated"]),
            "data_basis": data_basis,
        }
        summary.update(threshold_summary)

        conclusion = self._conclusion(summary, layer_stats, finalists, pending_ids)
        return {
            "schema_version": RESULTS_SCHEMA_VERSION,
            "project": {
                "project_id": project_id,
                "name": state.get("project") or project_id,
                "targets": target_ids,
            },
            "run": run,
            "summary": summary,
            "layers": layer_stats,
            "finalists": finalists,
            "pending_candidates": [candidates_by_id[cid] for cid in sorted(pending_ids)],
            "shortlist": shortlist,
            "thresholds": threshold_summary,
            "conclusion": conclusion,
            "trace": {
                "project_id": project_id,
                "workflow_id": (run or {}).get("workflow_id"),
                "run_id": (run or {}).get("run_id"),
            },
        }

    @staticmethod
    def _conclusion(summary: dict, layers: list[dict], finalists: list[dict], pending_ids: set) -> str:
        evaluated = summary["candidates_evaluated"]
        if not evaluated:
            return (
                "No candidate has a battery evaluation result yet. Run the prediction "
                "battery on generated candidates (P0-D) to produce the first hard-clearance outcome."
            )
        cleared = summary["hard_cleared"]
        rate = summary["hard_clearance_rate"]
        top = finalists[0] if finalists else None
        parts = [
            f"{cleared} of {evaluated} evaluated candidates passed the hard-clearance battery"
            f" ({rate * 100:.0f}%)."
        ]
        if top:
            top_score = "n/a" if top["desirability"] is None else f"{top['desirability']:.3f}"
            parts.append(
                f"Top-ranked candidate is {top['candidate_id']} "
                f"(hard clearance {'passed' if top['hard_cleared'] else 'failed'}, "
                f"exploration desirability {top_score})."
            )
        if pending_ids:
            parts.append(f"{len(pending_ids)} candidate(s) are designed but await prediction.")
        calibrated = summary["counts"]["calibrated"]
        provisional = summary["counts"]["provisional"]
        parts.append(
            f"Thresholds: {calibrated} effective entries calibrated, {provisional} provisional."
        )
        if summary["data_basis"] == "demo_fixture":
            parts.append(
                "Current rows are demo fixture data (synthetic); this is a pipeline "
                "demonstration, not a final scientific conclusion. Real samples require "
                "P0-D re-runs with calibrated thresholds."
            )
        elif summary["data_basis"] == "real":
            parts.append("Current rows are real run data.")
        return " ".join(parts)


__all__ = ["RESULTS_SCHEMA_VERSION", "ResultsReader"]
