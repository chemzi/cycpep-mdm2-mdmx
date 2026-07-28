"""Project/target configuration helpers for transferable binder-design runs.

The public workflow may still accept a minimal identifier.  Research resolves
that identifier into this normalized config before downstream agents run.
MDM2/MDMX is the bundled example, not a special case in the evaluator.
"""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_PROJECT_CONFIG = ROOT / "projects" / "mdm2_mdmx.json"


class ProjectConfigError(ValueError):
    """Raised when a project config cannot safely drive the pipeline."""


def target_slug(target_id: str) -> str:
    """Return the stable suffix used by legacy flat metric columns."""
    slug = re.sub(r"[^a-z0-9]+", "_", str(target_id).strip().lower()).strip("_")
    if not slug:
        raise ProjectConfigError("target id must contain at least one letter or digit")
    return slug


def normalize_targets(raw_targets: Any) -> list[dict]:
    """Accept the new target list or the legacy ``{id: info}`` mapping."""
    if isinstance(raw_targets, dict):
        targets = [dict(info or {}, id=target_id) for target_id, info in raw_targets.items()]
    elif isinstance(raw_targets, list):
        targets = [dict(item) for item in raw_targets]
    else:
        raise ProjectConfigError("targets must be a list or object mapping")

    if not targets:
        raise ProjectConfigError("at least one target is required")

    seen_ids: set[str] = set()
    seen_slugs: set[str] = set()
    for target in targets:
        target_id = str(target.get("id") or target.get("target_id") or "").strip()
        if not target_id:
            raise ProjectConfigError("every target requires id")
        if target_id in seen_ids:
            raise ProjectConfigError(f"duplicate target id: {target_id}")
        slug = target_slug(target_id)
        if slug in seen_slugs:
            raise ProjectConfigError(f"target ids collide after normalization: {target_id}")
        seen_ids.add(target_id)
        seen_slugs.add(slug)
        target["id"] = target_id
        target["metric_slug"] = slug
        target.setdefault("required", True)
    return targets


def normalize_project_config(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ProjectConfigError("project config must be an object")
    config = deepcopy(raw)
    config.setdefault("schema_version", 1)
    config.setdefault("project_id", "unnamed_cyclic_peptide_project")
    config.setdefault("modality", "cyclic_peptide")
    config.setdefault("objective", "binder")
    config["targets"] = normalize_targets(config.get("targets"))
    config.setdefault("selection", {})
    config["selection"].setdefault("experiment_budget", None)
    return config


def load_project_config(path: str | Path | None = None, raw: dict | None = None) -> dict:
    """Load and normalize a JSON config.

    ``CYCPEP_PROJECT_CONFIG`` overrides the bundled MDM2/MDMX example.  JSON
    is intentionally used here so no optional YAML dependency is required.
    """
    if raw is not None:
        return normalize_project_config(raw)
    selected = Path(path or os.environ.get("CYCPEP_PROJECT_CONFIG", DEFAULT_PROJECT_CONFIG))
    if not selected.exists():
        raise ProjectConfigError(f"project config not found: {selected}")
    return normalize_project_config(json.loads(selected.read_text(encoding="utf-8")))


def required_target_ids(config: dict) -> tuple[str, ...]:
    return tuple(target["id"] for target in config["targets"] if target.get("required", True))


def candidate_metrics(candidate: dict) -> dict:
    metrics = candidate.get("metrics")
    if isinstance(metrics, dict):
        return metrics
    encoded = candidate.get("metrics_json")
    if isinstance(encoded, str) and encoded.strip():
        try:
            decoded = json.loads(encoded)
            return decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def target_value(candidate: dict, target_id: str, metric: str):
    """Read a target metric from v6 nested data, with v5 flat fallback."""
    target_metrics = candidate_metrics(candidate).get("targets", {})
    if target_id in target_metrics and metric in target_metrics[target_id]:
        return target_metrics[target_id].get(metric)
    # Case-insensitive nested lookup helps configs created from gene symbols.
    for key, values in target_metrics.items():
        if str(key).casefold() == str(target_id).casefold() and metric in values:
            return values.get(metric)
    return candidate.get(f"{metric}_{target_slug(target_id)}")


def global_value(candidate: dict, metric: str):
    """Read a global metric from v6 nested data, with v5 flat fallback."""
    global_metrics = candidate_metrics(candidate).get("global", {})
    return global_metrics.get(metric, candidate.get(metric))


def threshold_for_target(thresholds: dict, key: str, target_id: str | None = None) -> dict:
    """Resolve a threshold's per-target override, preserving legacy entries."""
    base = dict(thresholds.get(key) or {})
    overrides = base.pop("targets", {})
    if target_id is not None and isinstance(overrides, dict):
        override = overrides.get(target_id)
        if override is None:
            override = next(
                (value for name, value in overrides.items()
                 if str(name).casefold() == str(target_id).casefold()),
                None,
            )
        if isinstance(override, dict):
            base.update(override)
    return base
