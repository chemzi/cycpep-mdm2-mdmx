"""Design Agent coordination: run-control merging, data access, and scoring."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

from data_layer import (  # noqa: E402
    CandidateIndex,
    EvidenceLogger,
    State,
)

from . import config  # noqa: E402
from .validation import _parse_hotspot_residues  # noqa: E402
from project_config import (  # noqa: E402
    required_target_ids,
    target_slug,
    target_value,
    threshold_for_target,
)
from structure_resolution import assert_target_structure_ready  # noqa: E402
from target_bootstrap import assert_project_approved  # noqa: E402
from peptide_contract import (  # noqa: E402
    MAX_CYCLIC_PEPTIDE_LENGTH,
    MIN_CYCLIC_PEPTIDE_LENGTH,
    supported_length_message,
)


def _require_mdm_reference_route(route_name, project_config=None):
    """Refuse MDM-specific motif routes outside the bundled MDM2/MDMX projects."""
    project = project_config if project_config is not None else config.ACTIVE_PROJECT_CONFIG
    target_ids = set(required_target_ids(project))
    if target_ids != {"MDM2", "MDMX"}:
        raise RuntimeError(
            f"{route_name} contains MDM-specific motif knowledge and is disabled for "
            f"project {project['project_id']}; provide project-specific motifs instead"
        )

def _load_existing_sequences():
    """Return sequences already registered in CandidateIndex for cross-batch dedup.

    By default, an unreadable index raises ``RuntimeError`` — RFdiffusion +
    LigandMPNN + AfCycDesign refold is too expensive to risk duplicates (P1).
    Set ``CYCPEP_ALLOW_WITHOUT_DEDUP=1`` to downgrade to a warning and
    proceed without dedup (testing / one-off exploration).
    """
    try:
        rows = CandidateIndex.load()
    except (OSError, UnicodeError, ValueError) as exc:
        if os.environ.get("CYCPEP_ALLOW_WITHOUT_DEDUP") == "1":
            EvidenceLogger.error("design", "candidate_index_unavailable",
                str(exc),
                recovery="cross-batch dedup disabled; candidates may duplicate")
            return None
        raise RuntimeError(
            "CandidateIndex is unavailable — cross-batch dedup cannot be "
            "guaranteed, which risks duplicating expensive RFdiffusion / "
            "LigandMPNN / AfCycDesign work.  Set CYCPEP_ALLOW_WITHOUT_DEDUP=1 "
            "to proceed without dedup, or fix the index and retry."
        ) from exc
    return {
        str(row.get("sequence") or "").upper()
        for row in rows
        if isinstance(row, dict) and row.get("sequence")
    }

def _safe_float(value):
    """Return float(value); return None for missing, empty, or non-finite values."""
    if value in (None, "") or isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None

def _resolve_threshold(*candidates):
    """Return the first candidate that passes _safe_float."""
    for value in candidates:
        resolved = _safe_float(value)
        if resolved is not None:
            return resolved
    return None

_MDM_PROJECT_IDS = frozenset({
    "mdm2_mdmx_reference", "design_v5_mdm2_mdmx", "design_v5_mdm2_mdmx_test",
})

def _resolve_target_thresholds(project, thresholds):
    """Pre-resolve per-target threshold gates once (P3-1).

    None of these depend on individual candidates, so hoisting them out of
    the inner loop avoids repeated dict lookups and legacy-fallback checks.
    Returns ``None`` when an uncalibrated MDM legacy threshold is rejected.
    """
    target_ids = required_target_ids(project)
    # MDM2/MDMX provisional legacy defaults — only for the bundled example projects.
    _pid = project.get("project_id", "").casefold()
    _is_mdm_project = _pid in _MDM_PROJECT_IDS
    if _is_mdm_project:
        EvidenceLogger.log("design", "mdm_legacy_defaults_active", {
            "project_id": _pid,
            "note": "uncalibrated provisional fallback thresholds (ipsae=0.6/0.5, "
                    "hotspot_cov=0.67) may be used when per-target configuration "
                    "is absent; calibrate against positive/negative controls",
        })
    _target_thresholds = []  # (target_id, slug, ipsae_threshold, hotspot_threshold)
    for target_id in target_ids:
        slug = target_slug(target_id)
        ipsae_rule = threshold_for_target(thresholds, "L2_ipsae", target_id)
        hotspot_rule = threshold_for_target(thresholds, "L5_hotspot_coverage", target_id)
        ipsae_threshold = _resolve_threshold(
            thresholds.get(f"ipsae_{slug}"),
            thresholds.get("ipsae"),
            ipsae_rule.get("value"),
        )
        ipsae_from_legacy = False
        if _is_mdm_project and ipsae_threshold is None:
            _LEGACY_MDM_THRESHOLDS = {"mdm2": 0.6, "mdmx": 0.5}
            ipsae_threshold = _LEGACY_MDM_THRESHOLDS.get(slug)
            if ipsae_threshold is not None:
                ipsae_from_legacy = True
        if ipsae_from_legacy:
            if os.environ.get("CYCPEP_ALLOW_UNVALIDATED_MDM_THRESHOLDS") != "1":
                EvidenceLogger.error("design", "mdm_threshold_rejected", {
                    "threshold": "ipsae", "value": ipsae_threshold, "target": slug,
                    "remediation": "calibrate per-target ipsae threshold in "
                                    "project_config, or set "
                                    "CYCPEP_ALLOW_UNVALIDATED_MDM_THRESHOLDS=1",
                })
                return None
            EvidenceLogger.error("design", "mdm_uncalibrated_threshold_used", {
                "threshold": "ipsae", "value": ipsae_threshold, "target": slug,
                "remediation": "calibrate per-target ipsae threshold against "
                                "positive/negative controls in project_config",
            })
        hotspot_threshold = _resolve_threshold(
            thresholds.get(f"hotspot_cov_{slug}"),
            thresholds.get("hotspot_cov"),
            hotspot_rule.get("value"),
        )
        hotspot_from_legacy = False
        if _is_mdm_project and hotspot_threshold is None:
            hotspot_threshold = 0.67
            hotspot_from_legacy = True
        if hotspot_from_legacy:
            if os.environ.get("CYCPEP_ALLOW_UNVALIDATED_MDM_THRESHOLDS") != "1":
                EvidenceLogger.error("design", "mdm_threshold_rejected", {
                    "threshold": "hotspot_cov", "value": 0.67, "target": slug,
                    "remediation": "calibrate per-target hotspot_cov threshold "
                                    "in project_config, or set "
                                    "CYCPEP_ALLOW_UNVALIDATED_MDM_THRESHOLDS=1",
                })
                return None
            EvidenceLogger.error("design", "mdm_uncalibrated_threshold_used", {
                "threshold": "hotspot_cov", "value": 0.67, "target": slug,
                "remediation": "calibrate per-target hotspot_cov threshold "
                                "against positive/negative controls",
            })
        _target_thresholds.append((target_id, slug, ipsae_threshold, hotspot_threshold))
    return _target_thresholds

def threshold_filter(candidates, thresholds, project_config=None):
    """Apply independent per-target ipSAE and hotspot-coverage gates."""
    project = project_config or config.ACTIVE_PROJECT_CONFIG
    resolved = _resolve_target_thresholds(project, thresholds)
    if resolved is None:
        return []

    passed = []
    for candidate in candidates:
        accepted = True
        for target_id, slug, ipsae_threshold, hotspot_threshold in resolved:
            ipsae_val = _safe_float(target_value(candidate, target_id, "ipsae"))
            hotspot_val = _safe_float(target_value(candidate, target_id, "hotspot_cov"))
            if ipsae_threshold is None:
                rejected_by = f"missing_ipsae_threshold_{slug}"
            elif hotspot_threshold is None:
                rejected_by = f"missing_hotspot_threshold_{slug}"
            elif ipsae_val is None:
                rejected_by = f"ipsae_nil_{slug}"
            elif hotspot_val is None:
                rejected_by = f"hotspot_nil_{slug}"
            elif ipsae_val < ipsae_threshold:
                rejected_by = f"ipsae_below_{slug}"
            elif hotspot_val < hotspot_threshold:
                rejected_by = f"hotspot_below_{slug}"
            else:
                rejected_by = None
            if rejected_by:
                accepted = False
                break
        if accepted:
            passed.append(candidate)
    return passed

def pareto_front(candidates, obj_x=None, obj_y=None, project_config=None):
    """Thin wrapper around data_layer.compute_pareto_front().

    The data-layer implementation handles missing objectives (exclude),
    mixed direction (maximize / minimize), per-target metrics, and
    candidate-ID validity — do NOT maintain a duplicate algorithm here.
    """
    project = project_config or config.ACTIVE_PROJECT_CONFIG
    if obj_x is None:
        target_ids = required_target_ids(project)
        # data_layer expects {"target": ..., "metric": ..., "direction": ...}
        objectives = tuple(
            {"target": tid, "metric": "ipsae", "direction": "maximize"}
            for tid in target_ids[:2]
        )
    else:
        objectives = (obj_x,) if obj_y is None else (obj_x, obj_y)

    from data_layer import compute_pareto_front
    front_ids = set(compute_pareto_front(candidates, objectives))
    return [c for c in candidates if c.get("candidate_id") in front_ids]

def _next_candidate_id():
    """Reserve the next C**** ID from the formal database sequence."""
    from data_layer import allocate_candidate_id

    return allocate_candidate_id()

def _load_target_spec():
    """
    从 State 读取 Research 产出的设计规则。
    若 Research 未运行则返回空结构（Route B/C 会报错退出）。
    """
    s = State.load()
    # 设计规则：Trp23 不变 / Phe19 ≤ Phe体积 / Leu26 换小脂肪族
    design_rules = s.get("design_rules", {}) or s.get("pocket_differences", {})
    return {
        "targets": s.get("targets", {}),
        "pocket_differences": s.get("pocket_differences", {}),
        "known_dual_binders": s.get("known_dual_binders", []),
        "design_rules": design_rules,
    }

def _resolve_target(project, target_spec, design_config):
    """Resolve the approved target selected by the run controls."""
    ts = target_spec or {}
    dc = design_config or {}
    default_target = project["targets"][0]["id"]
    target_ref = (
        dc.get("target_id") or ts.get("target_id") or ts.get("id")
        or dc.get("target_name") or ts.get("target_name")
        or default_target
    )
    return assert_target_structure_ready(project, target_ref)

def _resolve_coordinate_artifact(target, target_spec, design_config):
    """Resolve the approved coordinate artifact; returns (path, sha256, pdb_id, chain)."""
    ts = target_spec or {}
    dc = design_config or {}
    structure = target.get("structure") or {}
    coordinate_value = structure.get("coordinate_path")
    if not coordinate_value:
        raise RuntimeError(
            f"approved target {target['id']} has no structure.coordinate_path; "
            "materialize and approve the coordinate artifact before Design"
        )
    coordinate_path = Path(coordinate_value).expanduser().resolve()
    if not coordinate_path.is_file():
        raise FileNotFoundError(
            f"approved coordinate artifact does not exist: {coordinate_path}"
        )

    requested_path = dc.get("target_pdb") or ts.get("target_pdb")
    if requested_path and Path(requested_path).expanduser().resolve() != coordinate_path:
        raise ValueError("target_pdb cannot override the approved coordinate_path")

    chain = structure.get("chain")
    if not chain:
        raise RuntimeError(f"approved target {target['id']} has no structure.chain")
    requested_chain = dc.get("chain") or ts.get("chain")
    if requested_chain and requested_chain != chain:
        raise ValueError("chain cannot override the approved target chain")
    return (
        str(coordinate_path),
        structure.get("coordinate_sha256"),
        structure.get("pdb_id"),
        chain,
    )

def _resolve_binding_hotspots(target, target_spec, design_config):
    """Resolve the approved binding-site hotspots as a comma-joined string."""
    ts = target_spec or {}
    dc = design_config or {}
    binding_site = target.get("binding_site") or {}
    hotspots = ",".join(str(residue) for residue in binding_site.get("residues", []))
    if hotspots:
        try:
            _parse_hotspot_residues(hotspots)
        except ValueError as exc:
            raise ValueError(
                f"approved target {target['id']} has invalid hotspot residues: {exc}"
            ) from exc
    requested_hotspots = dc.get("hotspots") or ts.get("hotspots")
    if requested_hotspots and requested_hotspots != hotspots:
        raise ValueError("hotspots cannot override the approved binding site")
    return hotspots

def _resolve_design_lengths(target, target_spec, design_config):
    """Resolve and validate the requested peptide lengths."""
    ts = target_spec or {}
    dc = design_config or {}
    lengths = dc.get("lengths") or ts.get("lengths") or (
        target.get("design") or {}
    ).get("lengths", [10, 12, 14])
    lengths = [int(length) for length in lengths]
    if not lengths or any(
        length < MIN_CYCLIC_PEPTIDE_LENGTH
        or length > MAX_CYCLIC_PEPTIDE_LENGTH
        for length in lengths
    ):
        raise ValueError(supported_length_message())
    return lengths

def _resolve_proposal_count(target, target_spec, design_config):
    """Resolve and validate the number of proposals requested per run."""
    ts = target_spec or {}
    dc = design_config or {}
    approved_design = target.get("design") or {}
    n = next(
        (
            value
            for value in (dc.get("n"), ts.get("n"), approved_design.get("n"))
            if value is not None
        ),
        100,
    )
    if type(n) is not int or n < 1:
        raise ValueError("n must be a positive integer")
    return n

def _resolve_seed(target_spec, design_config):
    """Resolve an int32 non-negative seed; auto-generate when unspecified.

    Fractional floats are rejected because ``int()`` silently truncates them
    (P1-2), which nearly always indicates a caller bug.
    """
    ts = target_spec or {}
    dc = design_config or {}
    seed = dc.get("seed") if dc.get("seed") is not None else ts.get("seed")
    if seed is None:
        seed = int.from_bytes(os.urandom(4), "big") % (2**31)
        print(f"[Design] seed not specified \u2014 auto-generated seed={seed} "
              f"(controls LigandMPNN + Route C; RFdiffusion backbone "
              f"generation is GPU non-deterministic)")
    try:
        seed = int(seed)
    except (ValueError, TypeError) as exc:
        seed = int.from_bytes(os.urandom(4), "big") % (2**31)
        print(f"[Design] invalid seed value {exc}, falling back to "
              f"auto-generated seed={seed}")
    if isinstance(original := (dc.get("seed") if dc.get("seed") is not None
                                else ts.get("seed")), float):
        if original != int(original):
            EvidenceLogger.error("design", "fractional_seed_rejected",
                f"seed={original!r} has a fractional part; int() truncates to "
                f"{seed}. Pass an integer seed instead.",
                recovery="use an integer seed in [0, 2^31-1]")
            raise ValueError(
                f"seed must be an integer, got fractional float {original!r}"
            )
    if seed < 0 or seed > 2**31 - 1:
        raise ValueError(
            f"seed must be in [0, {2**31 - 1}] (int32 non-negative), got {seed}"
        )
    return seed

def _merge_config(target_spec, design_config, project_config=None):
    """Merge run controls with the approved target and coordinate artifact.

    Target identity, chain, hotspots, and coordinate path are security-sensitive
    project inputs.  They come from the approved project config; callers may
    select a configured target but may not replace those fields ad hoc.
    """
    project = project_config if project_config is not None else config.ACTIVE_PROJECT_CONFIG
    assert_project_approved(project)

    target = _resolve_target(project, target_spec, design_config)
    coordinate_path, coordinate_sha256, pdb_id, chain = _resolve_coordinate_artifact(
        target, target_spec, design_config
    )
    hotspots = _resolve_binding_hotspots(target, target_spec, design_config)
    lengths = _resolve_design_lengths(target, target_spec, design_config)
    n = _resolve_proposal_count(target, target_spec, design_config)
    seed = _resolve_seed(target_spec, design_config)

    return {
        "project_id": project["project_id"],
        "modality": project.get("modality", "cyclic_peptide"),
        "target_id": target["id"],
        "target_name": target["id"],
        "target_pdb": coordinate_path,
        "target_pdb_sha256": coordinate_sha256,
        "pdb_id": pdb_id,
        "chain": chain,
        "hotspots": hotspots,
        "lengths": lengths,
        "n": n,
        "seed": seed,
    }
