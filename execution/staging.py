"""Isolated filesystem staging for one execution transaction."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from prediction_pipeline.contracts import file_sha256


@dataclass(frozen=True)
class StagedArtifact:
    artifact_id: str
    artifact_type: str
    staged_path: str
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StagingArea:
    def __init__(self, root: str | Path, transaction_id: str):
        self.path = Path(root) / transaction_id
        self.artifacts_path = self.path / "artifacts"
        self.metadata_path = self.path / "metadata"

    def create(self) -> "StagingArea":
        self.artifacts_path.mkdir(parents=True, exist_ok=True)
        self.metadata_path.mkdir(parents=True, exist_ok=True)
        return self

    def stage_artifact(
        self,
        source: str | Path,
        *,
        artifact_id: str,
        artifact_type: str,
    ) -> StagedArtifact:
        source_path = Path(source)
        if not source_path.is_file():
            raise ValueError(f"artifact does not exist: {source_path}")
        if not artifact_id or "/" in artifact_id or "\\" in artifact_id:
            raise ValueError("artifact_id must be one path-safe segment")
        destination = self.artifacts_path / artifact_id / source_path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ValueError(f"artifact already staged: {artifact_id}")
        shutil.copyfile(source_path, destination)
        return StagedArtifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            staged_path=str(destination),
            size_bytes=destination.stat().st_size,
            sha256=file_sha256(destination),
        )

    def write_manifest(self, name: str, payload: Mapping[str, Any]) -> Path:
        destination = self.metadata_path / name
        destination.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return destination

    def discard(self) -> None:
        if self.path.exists():
            shutil.rmtree(self.path)
