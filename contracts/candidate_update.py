"""Design-to-Execution candidate staging contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


CANDIDATE_UPDATE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CandidateUpdate:
    candidate: Mapping[str, Any]

    def __post_init__(self) -> None:
        value = dict(self.candidate)
        if not value.get("candidate_id") or not value.get("sequence"):
            raise ValueError("candidate update requires candidate_id and sequence")
        object.__setattr__(self, "candidate", value)

    @property
    def candidate_id(self) -> str:
        return str(self.candidate["candidate_id"])

    @property
    def design_pdb_path(self) -> str:
        return str(self.candidate.get("design_pdb_path") or "")

    @property
    def manifest_path(self) -> str:
        return str(self.candidate.get("manifest_path") or "")

    def to_dict(self) -> dict[str, Any]:
        return dict(self.candidate)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CandidateUpdate":
        return cls(candidate=dict(value))


@dataclass(frozen=True)
class CandidateUpdateBatch:
    schema_version: int
    emitter: str
    job_id: str
    candidate_updates: tuple[CandidateUpdate, ...]

    def __post_init__(self) -> None:
        if self.schema_version != CANDIDATE_UPDATE_SCHEMA_VERSION:
            raise ValueError(f"unsupported candidate update schema: {self.schema_version}")
        if not self.emitter:
            raise ValueError("emitter is required")
        object.__setattr__(self, "candidate_updates", tuple(self.candidate_updates))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "emitter": self.emitter,
            "job_id": self.job_id,
            "candidate_updates": [item.to_dict() for item in self.candidate_updates],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CandidateUpdateBatch":
        return cls(
            schema_version=int(value.get("schema_version", CANDIDATE_UPDATE_SCHEMA_VERSION)),
            emitter=str(value.get("emitter") or "design"),
            job_id=str(value.get("job_id") or ""),
            candidate_updates=tuple(
                CandidateUpdate.from_dict(item)
                for item in (value.get("candidate_updates") or [])
            ),
        )
