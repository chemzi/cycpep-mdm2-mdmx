"""Versioned scientific protocols shared by Planner and Execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class PredictionProtocol:
    version: str
    colabdesign_seeds: tuple[int, ...]
    colabdesign_model_numbers: tuple[int, ...]
    colabdesign_num_recycles: int
    boltz_seed_base: int
    post_relax_seed_base: int
    post_relax_repeats: int

    def payload(self) -> dict:
        value = asdict(self)
        value["colabdesign_seeds"] = list(self.colabdesign_seeds)
        value["colabdesign_model_numbers"] = list(self.colabdesign_model_numbers)
        return value

    @property
    def sha256(self) -> str:
        encoded = json.dumps(
            self.payload(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


PREDICTION_PROTOCOL = PredictionProtocol(
    version="af2_boltz2_prodigy_rosetta_postrelax_v1",
    colabdesign_seeds=(0, 1, 2),
    colabdesign_model_numbers=(0, 1, 2),
    colabdesign_num_recycles=3,
    boltz_seed_base=101,
    post_relax_seed_base=20260802,
    post_relax_repeats=3,
)
