"""ProjectContext — explicit per-project context (Engineering Standard §7 / Roadmap PR5).

Goal: remove import-time project globals (``ACTIVE_PROJECT_CONFIG = ...``) and
let Research / Design / Prediction / Critic / Planner / Execution receive the
current project through dependency injection, so several projects can coexist
safely in one process (MDM2, MDMX, KEAP1, BCL2, ...).
"""

from __future__ import annotations

import os

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from project_config import load_project_config, normalize_project_config, target_slug

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ProjectPaths:
    """Writable paths for one project.

    ``None`` fields mean "use the project-scoped default".  ``resolve()``
    materialises concrete paths without touching the environment, so callers
    stay side-effect free until they actually write.
    """

    data_dir: Path | None = None
    evidence_dir: Path | None = None
    output_dir: Path | None = None
    database_path: Path | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "data_dir", "evidence_dir", "output_dir", "database_path"
        ):
            value = getattr(self, field_name)
            if value is None:
                continue
            if isinstance(value, (str, os.PathLike)):
                object.__setattr__(self, field_name, Path(value))
            else:
                raise TypeError(f"{field_name} must be a str, Path, or None")

    def resolve(self, project_id: str, root: Path = ROOT) -> "ProjectPaths":
        """Return concrete paths, deriving project-scoped defaults when unset.

        The reference project keeps the legacy flat ``data/`` / ``evidence/``
        layout; every other project is isolated under ``data/projects/<slug>``
        and ``evidence/projects/<slug>`` (same rule as ``data_layer``).
        """
        slug = target_slug(project_id)
        is_reference = project_id == "mdm2_mdmx_reference"
        data_dir = (
            self.data_dir
            if self.data_dir is not None
            else (root / "data" if is_reference else root / "data" / "projects" / slug)
        )
        evidence_dir = (
            self.evidence_dir
            if self.evidence_dir is not None
            else (root / "evidence" if is_reference else root / "evidence" / "projects" / slug)
        )
        database_path = (
            self.database_path
            if self.database_path is not None
            else data_dir / "store.db"
        )
        return ProjectPaths(
            data_dir=data_dir,
            evidence_dir=evidence_dir,
            output_dir=self.output_dir,
            database_path=database_path,
        )


@dataclass(frozen=True)
class ProjectContext:
    """Immutable per-project context injected into agents.

    project_id : stable project identifier from the approved config.
    config     : normalized, approved project config.
    paths      : optional explicit paths; ``resolve_paths()`` fills defaults.
    runtime    : optional tool-path / environment snapshot (e.g. design tools).
    """

    project_id: str
    config: Mapping[str, Any]
    paths: ProjectPaths | None = None
    runtime: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.config, Mapping):
            raise TypeError("config must be a mapping")
        if not isinstance(self.project_id, str) or not self.project_id:
            raise ValueError("project_id must be a non-empty string")
        if self.paths is not None and not isinstance(self.paths, ProjectPaths):
            raise TypeError("paths must be a ProjectPaths or None")
        object.__setattr__(self, "config", dict(self.config))

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        paths: ProjectPaths | None = None,
        runtime: Mapping[str, Any] | None = None,
    ) -> "ProjectContext":
        """Build a context from a (possibly raw) project config."""
        normalized = normalize_project_config(dict(config))
        return cls(
            project_id=str(normalized["project_id"]),
            config=normalized,
            paths=paths,
            runtime=runtime,
        )

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        raw: dict | None = None,
        paths: ProjectPaths | None = None,
        runtime: Mapping[str, Any] | None = None,
    ) -> "ProjectContext":
        """Load and normalize a project config, then wrap it in a context."""
        return cls.from_config(
            load_project_config(path=path, raw=raw),
            paths=paths,
            runtime=runtime,
        )

    @classmethod
    def from_runtime_config(
        cls,
        config: Mapping[str, Any],
        *,
        environ: Mapping[str, str] | None = None,
        runtime: Mapping[str, Any] | None = None,
    ) -> "ProjectContext":
        """Freeze documented runtime path overrides into one context."""

        source = os.environ if environ is None else environ
        def runtime_path(name: str) -> str | None:
            value = source.get(name)
            return value if value else None

        paths = ProjectPaths(
            data_dir=runtime_path("CYCPEP_DATA_DIR"),
            evidence_dir=runtime_path("CYCPEP_EVIDENCE_DIR"),
            database_path=runtime_path("CYCPEP_DB_PATH"),
        )
        return cls.from_config(config, paths=paths, runtime=runtime)

    @classmethod
    def from_runtime(
        cls,
        path: str | Path | None = None,
        raw: dict | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        runtime: Mapping[str, Any] | None = None,
    ) -> "ProjectContext":
        """Load a project and resolve documented runtime paths exactly once."""

        return cls.from_runtime_config(
            load_project_config(path=path, raw=raw),
            environ=environ,
            runtime=runtime,
        )

    @classmethod
    def default(cls) -> "ProjectContext":
        """Context for the environment-selected project (legacy default)."""
        return cls.from_runtime()

    @property
    def targets(self) -> tuple[str, ...]:
        """Required target ids in the approved config."""
        return tuple(target["id"] for target in self.config.get("targets", []))

    def resolve_paths(self) -> ProjectPaths:
        """Concrete project-scoped paths (data / evidence / output)."""
        explicit = self.paths or ProjectPaths()
        return explicit.resolve(self.project_id)
