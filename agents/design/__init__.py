"""Design Agent v5 — cyclic peptide design routes (于嘉乐).

Public entry points are re-exported here; internal helpers stay importable
from their owning submodule for the compatibility layer.
"""

from . import config  # noqa: F401
from .agent import Design  # noqa: F401
from .config import DESIGN_PIPELINE_VERSION, DesignContext  # noqa: F401
from .compat import (  # noqa: F401
    design_afcyc,
    design_atsp_cyclize,
    design_motif_graft,
    dual_target_score,
)
from .manifests import (  # noqa: F401
    _candidate_from_manifest,
    _manifest_summary,
    _write_manifest,
)
from .route_a import design_rfpeptides  # noqa: F401
from .route_b import design_motif_guided  # noqa: F401
from .route_c import (  # noqa: F401
    _route_c_base_combos,
    _route_c_cyclization_pairs,
    _route_c_design_references,
    design_atsp_derived,
)
from .runtime import (  # noqa: F401
    _build_refold_script,
    _cleanup_partial_rfdiff_output,
    _run_ligandmpnn,
    _run_refold,
    _run_rfdiff,
    _rfdiff_subprocess_env,
    _verify_colabdesign_runtime,
)
from .service import (  # noqa: F401
    _load_existing_sequences,
    _load_target_spec,
    _merge_config,
    _next_candidate_id,
    _require_mdm_reference_route,
    pareto_front,
    threshold_filter,
)
from .validation import (  # noqa: F401
    _binder_first_contig,
    _canonical_cyclization_type,
    _cheap_filter_sequences,
    _describe_cyclize,
    _extract_ligandmpnn_binder_sequence,
    _first_model_residues,
    _hotspot_fixed_residues,
    _hotspot_positions,
    _infer_binder_chain,
    _infer_cyclization_type,
    _parse_binder_residues,
    _parse_hotspot_residues,
    _pdb_chain_residue_layout,
    _pdb_chain_sequences,
    _pdb_residue_range,
    _ring_closure_check,
    _sequence_quality_score,
    _synthesizability_violations,
    _validate_sequence,
    _verify_fixed_sequence_pdb,
)


def __getattr__(name):  # noqa: N807
    """Backward-compatible live view over config module values (PEP 562)."""
    if hasattr(config, name):
        return getattr(config, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
