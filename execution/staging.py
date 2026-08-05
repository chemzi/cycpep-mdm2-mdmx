"""Filesystem staging primitives for one execution transaction."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StagedArtifact:
    artifact_id: str
    artifact_type: str
    staged_path: str
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
        destination = self.artifacts_path / source_path.name
        if source_path.resolve() != destination.resolve():
            destination.write_bytes(source_path.read_bytes())
        digest = sha256(destination.read_bytes()).hexdigest()
        return StagedArtifact(artifact_id, artifact_type, str(destination), digest)

    def write_manifest(self, name: str, payload: dict[str, Any]) -> Path:
        destination = self.metadata_path / name
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
            newline="\n",
        )
        return destination

    def discard(self) -> None:
        if not self.path.exists():
            return
        for child in sorted(self.path.rglob("*"), reverse=True):
            if child.is_file() or child.is_symlink():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        self.path.rmdir()
