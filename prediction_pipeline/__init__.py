"""Production prediction pipeline for cyclic-peptide candidates.

The package deliberately separates immutable input validation, raw artifact
parsing, metric calculation, and orchestration.  Heavy predictors may run in a
different environment; their outputs cross the boundary through a versioned
``artifacts.json`` file and are validated again before scoring.
"""

from .contracts import (
    CandidateInput,
    ContractError,
    PredictionConfig,
    load_candidate_inputs,
)
from .pipeline import PredictionPipeline

__all__ = [
    "CandidateInput",
    "ContractError",
    "PredictionConfig",
    "PredictionPipeline",
    "load_candidate_inputs",
]
