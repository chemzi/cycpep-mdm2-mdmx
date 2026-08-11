"""Production prediction pipeline for cyclic-peptide candidates.

The package deliberately separates immutable input validation, raw artifact
parsing, metric calculation, and orchestration.  Heavy predictors may run in a
different environment; their outputs cross the boundary through a versioned
``artifacts.json`` file and are validated again before scoring.
"""

from .contracts import (
    CRITIC_READY_STATUSES,
    CandidateInput,
    ContractError,
    PREDICTION_RECORD_STATUSES,
    PredictionConfig,
    load_candidate_inputs,
    prediction_status_from_battery,
)
from .pipeline import PredictionPipeline

__all__ = [
    "CandidateInput",
    "CRITIC_READY_STATUSES",
    "ContractError",
    "PREDICTION_RECORD_STATUSES",
    "PredictionConfig",
    "PredictionPipeline",
    "load_candidate_inputs",
    "prediction_status_from_battery",
]
