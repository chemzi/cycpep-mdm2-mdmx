"""Candidate manifest writer and stable dev handoff contract builder."""

from __future__ import annotations

import copy
import json
import os

from data_layer import EvidenceLogger, file_hash  # noqa: E402

from . import config  # noqa: E402
from .config import (
    DESIGN_PIPELINE_VERSION,
    DESIGN_PROTOCOL,
    DESIGN_PROTOCOL_IDENTITY_SHA256,
)  # noqa: E402
from .validation import (  # noqa: E402
    _canonical_cyclization_type,
    _infer_cyclization_type,
    _ring_closure_check,
)


def _resolve_manifest_ring_closure(cid, seq, refold_pdb,
                                          cyclization, ring_closure):
    """Infer/normalise cyclization and audit the closure geometry result."""
    if cyclization is None:
        cyclization = _infer_cyclization_type(seq)
    cyclization_description = str(cyclization)
    canonical_cyclization = _canonical_cyclization_type(
        cyclization_description, sequence=seq
    )
    rc = copy.deepcopy(ring_closure) if ring_closure is not None else None
    if rc is None:
        rc = (
            _ring_closure_check(
                refold_pdb, canonical_cyclization, sequence=seq
            )
            if os.path.exists(refold_pdb)
            else {"pass": False, "reason": "refold_pdb_missing"}
        )
    observed_type = rc.get("cyclization_type")
    if observed_type and observed_type != canonical_cyclization:
        raise ValueError(
            f"[{cid}] ring-closure result cyclization does not match manifest: "
            f"{observed_type!r} != {canonical_cyclization!r}"
        )
    return canonical_cyclization, cyclization_description, rc


def _design_reference_contract(cid, backbone_pdb, refold_pdb,
                               design_reference_role):
    """Validate the L7 design reference and derive its contract fields."""
    if not backbone_pdb:
        return "", "", ""
    reference_path = os.path.realpath(str(backbone_pdb))
    refold_path = os.path.realpath(str(refold_pdb))
    if not os.path.isfile(reference_path):
        raise ValueError(f"[{cid}] Design reference does not exist: {reference_path}")
    if reference_path == refold_path:
        raise ValueError(
            f"[{cid}] fixed-sequence refold cannot be its own L7 Design reference"
        )
    design_reference_hash = file_hash(reference_path)
    refold_hash = file_hash(refold_path) if os.path.exists(refold_path) else ""
    if refold_hash and design_reference_hash == refold_hash:
        raise ValueError(
            f"[{cid}] L7 Design reference is byte-identical to fixed-sequence refold"
        )
    design_reference_role = (
        design_reference_role or "rfdiffusion_target_bound_backbone"
    )
    if design_reference_role not in {
        "rfdiffusion_target_bound_backbone",
        "experimental_cyclic_peptide_structure",
    }:
        raise ValueError(
            f"[{cid}] unsupported Design reference role: "
            f"{design_reference_role!r}"
        )
    return reference_path, design_reference_hash, design_reference_role


def _write_manifest(
        cid, seq, route, batch_id, refold_pdb, config, backbone_pdb=None,
        cyclization=None, ring_closure=None, bb_alternatives=None,
        design_reference_role=None, reference_metadata=None):
    """Write one versioned candidate manifest with audited closure geometry."""
    refold_dir = os.path.dirname(refold_pdb)
    manifest_path = os.path.join(refold_dir, "manifest.json")
    canonical_cyclization, cyclization_description, rc = (
        _resolve_manifest_ring_closure(
            cid, seq, refold_pdb, cyclization, ring_closure
        )
    )
    design_reference, design_reference_hash, design_reference_role = (
        _design_reference_contract(
            cid, backbone_pdb, refold_pdb, design_reference_role
        )
    )

    manifest = {
        "design_pipeline_version": DESIGN_PIPELINE_VERSION,
        "protocol_version": DESIGN_PROTOCOL["version"],
        "protocol_sha256": DESIGN_PROTOCOL_IDENTITY_SHA256,
        "candidate_id": cid, "sequence": seq, "length": len(seq),
        "source_route": route, "source_batch": batch_id,
        "cyclization_type": canonical_cyclization,
        "cyclization_description": cyclization_description,
        "refold_pdb": refold_pdb,
        "refold_pdb_hash": file_hash(refold_pdb) if os.path.exists(refold_pdb) else "",
        # Explicit v5.2 contract.  backbone_* remains a compatibility alias for
        # older Prediction readers and historical manifests.
        "design_reference_pdb": design_reference,
        "design_reference_pdb_hash": design_reference_hash,
        "design_reference_role": design_reference_role,
        "backbone_pdb": design_reference,
        "backbone_pdb_hash": design_reference_hash,
        "backbone_alternatives": bb_alternatives or [],
        "ring_closure": rc,
        "design_config_summary": {
            "project_id": config.get("project_id"),
            "target": config.get("target_id"),
            "target_pdb": config.get("target_pdb"),
            "target_pdb_sha256": config.get("target_pdb_sha256"),
            "seed": config.get("seed"),
        },
        "reference_metadata": copy.deepcopy(reference_metadata or {}),
    }
    manifest["manifest_path"] = manifest_path
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest

def _manifest_summary(manifest):
    return {
        key: manifest[key]
        for key in [
            "design_pipeline_version", "candidate_id", "sequence",
            "refold_pdb_hash", "manifest_path",
        ]
        if key in manifest
    }

def _candidate_from_manifest(manifest, plddt, notes=None):
    """Convert a v5 manifest into the stable dev candidate handoff contract."""
    length = manifest["length"]
    cyclization = manifest["cyclization_type"]
    if "head-to-tail_amide" in cyclization:
        bonds = [{
            "atom_1": f"residue_{length}:C",
            "atom_2": "residue_1:N",
            "bond_type": "amide",
        }]
    elif "Cys-Cys_disulfide" in cyclization:
        bonds = [{
            "atom_1": "residue_1:SG",
            "atom_2": f"residue_{length}:SG",
            "bond_type": "disulfide",
        }]
    else:
        EvidenceLogger.error("design", "unknown_cyclization_bonds", {
            "cyclization_type": cyclization,
            "candidate_id": manifest["candidate_id"],
            "remediation": "add bond geometry to _candidate_from_manifest",
        })
        raise ValueError(
            f"unsupported cyclization type {cyclization!r} — cannot determine "
            f"cyclization bonds for candidate {manifest['candidate_id']}"
        )
    note_payload = {**_manifest_summary(manifest), **(notes or {})}
    return {
        "candidate_id": manifest["candidate_id"],
        "sequence": manifest["sequence"],
        "length": length,
        "source_route": manifest["source_route"],
        "source_batch": manifest["source_batch"],
        "cyclization_type": cyclization,
        "cyclization_bonds": bonds,
        "design_pdb_path": manifest["refold_pdb"],
        "design_pdb_hash": manifest["refold_pdb_hash"],
        "manifest_path": manifest["manifest_path"],
        "monomer_plddt": round(float(plddt), 3),
        "notes": json.dumps(note_payload, ensure_ascii=False),
    }
