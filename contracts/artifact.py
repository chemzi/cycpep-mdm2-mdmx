"""Artifact provenance contract."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    artifact_type: str
    path: str
    sha256: str
    producer_task_id: str | None = None
    producer_attempt_id: str | None = None
    schema_version: str | int | None = None
    input_artifact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("artifact_id", "artifact_type", "path"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be non-empty")
        if not isinstance(self.sha256, str) or not SHA256_RE.fullmatch(self.sha256):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        for name in ("producer_task_id", "producer_attempt_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} must be a non-empty string or None")
        if self.schema_version is not None and not isinstance(
            self.schema_version, (str, int)
        ):
            raise ValueError("schema_version must be a string, integer or None")
        object.__setattr__(self, "input_artifact_ids", tuple(self.input_artifact_ids))

    def to_dict(self) -> dict[str, Any]:
        result = {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "path": self.path,
            "sha256": self.sha256,
            "producer_task_id": self.producer_task_id,
            "producer_attempt_id": self.producer_attempt_id,
            "schema_version": self.schema_version,
            "input_artifact_ids": list(self.input_artifact_ids),
        }
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactRef":
        if not isinstance(value, Mapping):
            raise ValueError("artifact ref must be an object")
        sha256 = value.get("sha256")
        artifact_id = value.get("artifact_id")
        # Compatibility adapter for the existing orchestrator inventory, which
        # predates ArtifactRef and only had role/path/sha256.
        if artifact_id is None and isinstance(sha256, str):
            artifact_id = f"artifact_{sha256[:12]}"
        return cls(
            artifact_id=artifact_id,
            artifact_type=value.get("artifact_type", value.get("role", "artifact")),
            path=value.get("path"),
            sha256=sha256,
            producer_task_id=value.get("producer_task_id"),
            producer_attempt_id=value.get("producer_attempt_id"),
            schema_version=value.get("schema_version"),
            input_artifact_ids=tuple(value.get("input_artifact_ids", ())),
        )
