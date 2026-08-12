"""Context-aware Design Agent facade (Engineering Standard P1-1)."""

from __future__ import annotations

from core.context import ProjectContext
from .config import DesignContext


class Design:
    """Design Agent with explicit project context injection.

    Use this entry point for new callers so a single process can serve
    multiple projects without import-time project globals::

        from agents.design import Design, DesignContext
        from core.context import ProjectContext

        design = Design(DesignContext(project_config=approved_config))
        # or the unified PR5 context:
        design = Design(ProjectContext.load(raw=approved_config))
        candidates = design.design_atsp_derived(design_config={"n": 10})

    The module-level route functions remain available as compatibility
    wrappers that use the default context derived from :mod:`config`.
    """

    def __init__(self, context=None):
        if isinstance(context, ProjectContext):
            context = DesignContext.from_project_context(context)
        self.context = context if context is not None else DesignContext.default()

    # ---- context access (public contract, Engineering Standard 4) ----
    @property
    def project_config(self):
        """Approved project configuration carried by this Design instance."""
        return self.context.project_config

    @property
    def output_dir(self):
        """Writable design root carried by this Design instance."""
        return self.context.output_dir

    def merge_config(self, target_spec=None, design_config=None):
        """Merge run controls with the approved target and coordinate artifact."""
        from .service import _merge_config
        return _merge_config(
            target_spec, design_config, project_config=self.context.project_config
        )

    def next_candidate_id(self):
        """Allocate the next C**** candidate ID (single-process thread-safe)."""
        from .service import _next_candidate_id
        return _next_candidate_id()

    def run_refold(self, sequence, output_pdb):
        """Fixed-sequence AfCycDesign refold; returns pLDDT or None on failure."""
        from .runtime import _run_refold
        return _run_refold(sequence, output_pdb)

    def ring_closure_check(self, pdb_path, cyclization_type, sequence=None):
        """Pre-relax geometric compatibility gate for the cyclization bond."""
        from .validation import _ring_closure_check
        return _ring_closure_check(pdb_path, cyclization_type, sequence=sequence)

    def write_manifest(self, cid, seq, route, batch_id, refold_pdb, config, *,
                       backbone_pdb=None, cyclization=None, ring_closure=None,
                       bb_alternatives=None, design_reference_role=None,
                       reference_metadata=None):
        """Write one versioned candidate manifest with audited closure geometry."""
        from .manifests import _write_manifest
        return _write_manifest(
            cid, seq, route, batch_id, refold_pdb, config,
            backbone_pdb=backbone_pdb, cyclization=cyclization,
            ring_closure=ring_closure, bb_alternatives=bb_alternatives,
            design_reference_role=design_reference_role,
            reference_metadata=reference_metadata,
        )

    def candidate_from_manifest(self, manifest, plddt, notes=None):
        """Convert a v5 manifest into the stable candidate handoff contract."""
        from .manifests import _candidate_from_manifest
        return _candidate_from_manifest(manifest, plddt, notes=notes)

    # ---- launcher-correlated initial boundary ----
    def materialize_initial_jobs(self):
        """Resolve the safe generic initial job set without scientific effects."""
        from .initial import materialize_initial_jobs
        return materialize_initial_jobs(self)

    def validate_initial_invocation(self, correlation, *, store=None):
        """Read the exact Design-owned receipts for recovery."""
        from .initial import validate_initial_invocation
        return validate_initial_invocation(correlation, store=store)

    def run_initial(self, correlation, *, store=None):
        """Run the initial generic Design route behind durable recovery receipts."""
        from .initial import run_initial
        return run_initial(self, correlation, store=store)

    # ---- routes ----
    def design_rfpeptides(self, target_spec=None, design_config=None):
        from .route_a import design_rfpeptides
        return design_rfpeptides(
            target_spec=target_spec, design_config=design_config,
            context=self.context,
        )

    def design_rfpeptides_initial(
        self, target_spec=None, design_config=None, *, candidate_updates=None
    ):
        """Run Route A with Launcher-owned scientific-tool failure semantics."""
        from .route_a import design_rfpeptides_initial
        return design_rfpeptides_initial(
            target_spec=target_spec,
            design_config=design_config,
            context=self.context,
            candidate_updates=candidate_updates,
        )

    def design_motif_guided(self, target_spec=None, design_config=None):
        from .route_b import design_motif_guided
        return design_motif_guided(
            target_spec=target_spec, design_config=design_config,
            context=self.context,
        )

    def design_atsp_derived(self, target_spec=None, design_config=None):
        from .route_c import design_atsp_derived
        return design_atsp_derived(
            target_spec=target_spec, design_config=design_config,
            context=self.context,
        )
