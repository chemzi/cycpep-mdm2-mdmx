"""Deprecated Design API kept for callers that predate the v5 route split."""

from __future__ import annotations

import warnings

from .route_a import design_rfpeptides  # noqa: E402
from .route_b import design_motif_guided  # noqa: E402
from .route_c import design_atsp_derived  # noqa: E402


def design_afcyc(target=None, n=10, lengths=None, hotspots=None, chain=None, seed=None):
    import warnings
    warnings.warn("deprecated, use design_rfpeptides", DeprecationWarning)
    target_spec = {}
    if target is not None:
        target_spec["target_name"] = target
    if chain is not None:
        target_spec["chain"] = chain
    if hotspots is not None:
        target_spec["hotspots"] = hotspots
    return design_rfpeptides(
        target_spec=target_spec,
        design_config={"n": n, "lengths": lengths or [10], "seed": seed})


def design_motif_graft(n=400, seed=None):
    import warnings
    warnings.warn("deprecated, use design_motif_guided", DeprecationWarning)
    return design_motif_guided(design_config={"n": n, "seed": seed})


def design_atsp_cyclize(n=200, seed=None):
    import warnings
    warnings.warn("deprecated, use design_atsp_derived", DeprecationWarning)
    return design_atsp_derived(design_config={"n": n, "seed": seed})


# ============================================================
# 兼容旧 dual_target_score（保留但不推荐）
# ============================================================

def dual_target_score(iptm_mdm2, iptm_mdmx):
    """旧版加权组合打分（被 Pareto 前沿替代，保留兼容）"""
    import warnings
    warnings.warn("dual_target_score deprecated, use threshold_filter+pareto_front",
                  DeprecationWarning)
    combined = (iptm_mdm2 + iptm_mdmx) / 2
    asymmetry = abs(iptm_mdm2 - iptm_mdmx)
    return {
        "dual_score": round(combined - 0.5 * asymmetry, 4),
        "combined": round(combined, 4),
        "asymmetry": round(asymmetry, 4),
        "passed": iptm_mdm2 > 0.7 and iptm_mdmx > 0.55 and asymmetry < 0.25,
    }
