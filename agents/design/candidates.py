"""Shared candidate registration: refold -> ring closure -> manifest -> index.

Route A/B/C previously duplicated this tail block; extracting it keeps the
single-responsibility boundary (Engineering Standard 3) and prevents the
index/manifest/closure logic from drifting between routes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from data_layer import CandidateIndex, EvidenceLogger  # noqa: E402

from .manifests import _candidate_from_manifest, _write_manifest  # noqa: E402
from .runtime import _run_refold  # noqa: E402
from .validation import _infer_cyclization_type, _ring_closure_check  # noqa: E402


@dataclass
class CandidateRegistration:
    """Outcome of one candidate registration attempt."""

    candidate: Optional[dict]
    refold_pdb: str
    plddt: Optional[float]
    ring_closure: dict
    cyclization_type: str


def _register_refolded_candidate(
    *,
    candidate_id,
    sequence,
    config,
    batch_dir,
    route_name,
    batch_id,
    backbone_pdb,
    cyclization=None,
    ring_closure=None,
    notes=None,
    bb_alternatives=None,
) -> CandidateRegistration:
    """Refold, validate ring closure, write manifest, and register a candidate.

    Returns a :class:`CandidateRegistration` whose ``candidate`` is ``None``
    when the candidate must be skipped (failed refold, failed closure check,
    or manifest mismatch).  The caller decides how to report the skip.
    """
    refold_dir = os.path.join(batch_dir, "candidates", candidate_id)
    os.makedirs(refold_dir, exist_ok=True)
    refold_pdb = os.path.join(refold_dir, "refold.pdb")
    plddt = _run_refold(sequence, refold_pdb)
    cyclization_type = cyclization or _infer_cyclization_type(sequence)
    try:
        rc = (
            _ring_closure_check(refold_pdb, cyclization_type, sequence=sequence)
            if os.path.exists(refold_pdb)
            else {"pass": False, "reason": "refold_pdb_missing"}
        )
    except (ValueError, OSError) as exc:
        rc = {"pass": False, "reason": f"closure_check_error: {exc}"}

    if plddt is None or not rc.get("pass"):
        return CandidateRegistration(
            candidate=None, refold_pdb=refold_pdb, plddt=plddt,
            ring_closure=rc, cyclization_type=cyclization_type,
        )

    try:
        manifest = _write_manifest(
            candidate_id, sequence, route_name, batch_id, refold_pdb, config,
            backbone_pdb=backbone_pdb, cyclization=cyclization_type,
            ring_closure=rc, bb_alternatives=bb_alternatives,
        )
    except ValueError as exc:
        EvidenceLogger.error("design", "manifest_cyclization_mismatch",
            str(exc), recovery="skip mismatched candidate (P1-7)")
        return CandidateRegistration(
            candidate=None, refold_pdb=refold_pdb, plddt=plddt,
            ring_closure=rc, cyclization_type=cyclization_type,
        )

    candidate = _candidate_from_manifest(manifest, plddt, notes=notes or {})
    CandidateIndex.add(candidate)
    EvidenceLogger.log("design", "candidate_registered",
        {"candidate": candidate},
        targets=[config["target_id"]], phase="design")
    return CandidateRegistration(
        candidate=candidate, refold_pdb=refold_pdb, plddt=plddt,
        ring_closure=rc, cyclization_type=cyclization_type,
    )


def _collect_raw_sequences(backbone_entries):
    """Flatten per-backbone LigandMPNN outputs into (all_raw_seqs, bb_lookup).

    Used by the global cheap filter pass so early backbones cannot starve
    later ones (P2-3).
    """
    all_raw_seqs = []
    bb_lookup = {}  # seq.upper() -> [(bb_path, binder_chain), ...]
    for bb_path, binder_chain, seqs in backbone_entries:
        for s in seqs:
            key = s.upper() if isinstance(s, str) else ""
            if key:
                bb_lookup.setdefault(key, []).append((bb_path, binder_chain))
        all_raw_seqs.extend(seqs)
    return all_raw_seqs, bb_lookup
