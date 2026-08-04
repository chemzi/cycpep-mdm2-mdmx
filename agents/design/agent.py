"""Context-aware Design Agent facade (Engineering Standard P1-1)."""

from __future__ import annotations

from .config import DesignContext


class Design:
    """Design Agent with explicit project context injection.

    Use this entry point for new callers so a single process can serve
    multiple projects without import-time project globals::

        from agents.design import Design, DesignContext

        design = Design(DesignContext(project_config=approved_config))
        candidates = design.design_atsp_derived(design_config={"n": 10})

    The module-level route functions remain available as compatibility
    wrappers that use the default context derived from :mod:`config`.
    """

    def __init__(self, context=None):
        self.context = context if context is not None else DesignContext.default()

    def design_rfpeptides(self, target_spec=None, design_config=None):
        from .route_a import design_rfpeptides
        return design_rfpeptides(
            target_spec=target_spec, design_config=design_config,
            context=self.context,
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
