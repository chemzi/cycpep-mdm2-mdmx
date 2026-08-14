"""Read-only, project-scoped runtime readiness doctor."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

import agents.design.config as design_config
from core.context import ProjectContext
from core.integrity import file_sha256
from data_layer import validate_storage_backend
from execution.config import ExecutionConfig
from execution.prediction_runtime import validate_required_prediction_tool_paths
from prediction_pipeline.adapters import validate_prodigy_runtime
from prediction_pipeline.boltz_worker import validate_boltz_runtime
from prediction_pipeline.colabdesign_worker import validate_colabdesign_runtime
from prediction_pipeline.contracts import PredictionConfig
from prediction_pipeline.execution_identity import PRODIGY_VERSION
from prediction_pipeline.rosetta_worker import validate_pyrosetta_runtime
from project_config import required_target_ids
from structure_resolution import assert_target_structure_ready
from target_bootstrap import assert_project_approved

from .diagnostics import resolve_diagnostics_root


PROFILE = "fresh_full_launcher"
_STATUSES = frozenset({"pass", "fail", "warning", "skipped"})
_REQUIREMENTS = frozenset({"required", "conditional"})


@dataclass(frozen=True)
class DoctorCheck:
    id: str
    category: str
    requirement: str
    status: str
    observation: str
    remediation: str | None

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError(f"unsupported doctor status: {self.status}")
        if self.requirement not in _REQUIREMENTS:
            raise ValueError(f"unsupported requirement level: {self.requirement}")

    def to_dict(self) -> dict:
        result = {
            "id": self.id,
            "category": self.category,
            "requirement": self.requirement,
            "status": self.status,
            "observation": self.observation,
        }
        if self.remediation is not None:
            result["remediation"] = self.remediation
        return result


@dataclass(frozen=True)
class DoctorReport:
    profile: str
    project_path: str
    checks: tuple[DoctorCheck, ...]

    @property
    def ready(self) -> bool:
        return not any(
            check.requirement == "required" and check.status == "fail"
            for check in self.checks
        )

    @property
    def exit_code(self) -> int:
        return 0 if self.ready else 1

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "project_path": self.project_path,
            "ready": self.ready,
            "checks": [check.to_dict() for check in self.checks],
        }


class HostProbes(Protocol):
    """Bounded host metadata operations used by the doctor."""

    def getenv(self, name: str) -> str | None: ...
    def path_metadata(self, path: str | Path) -> Mapping[str, object]: ...
    def writable_parent(self, path: str | Path) -> Mapping[str, object]: ...
    def gpu_observation(self, timeout: int) -> Mapping[str, object]: ...


class DefaultHostProbes:
    def getenv(self, name: str) -> str | None:
        return os.environ.get(name)

    def path_metadata(self, path: str | Path) -> Mapping[str, object]:
        value = Path(path).expanduser().absolute()
        return {
            "path": str(value),
            "exists": value.exists(),
            "is_file": value.is_file(),
            "is_dir": value.is_dir(),
        }

    def writable_parent(self, path: str | Path) -> Mapping[str, object]:
        value = Path(path).expanduser().absolute()
        candidate = value if value.exists() and value.is_dir() else value.parent
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        return {"path": str(candidate), "writable": os.access(candidate, os.W_OK)}

    def gpu_observation(self, timeout: int) -> Mapping[str, object]:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError("nvidia-smi did not report a visible GPU")
        line = next((item.strip() for item in completed.stdout.splitlines() if item.strip()), "")
        fields = [item.strip() for item in line.split(",")]
        if len(fields) < 3:
            raise RuntimeError("nvidia-smi returned no visible GPU")
        return {"model": fields[0], "driver": fields[1], "memory_mb": fields[2]}


def _redact(value: object, probes: HostProbes) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")[:500]
    secret = probes.getenv("OPENAI_API_KEY")
    if secret:
        text = text.replace(secret, "[REDACTED]")
    return text


def _check(
    check_id: str,
    category: str,
    probes: HostProbes,
    operation,
    remediation: str,
) -> DoctorCheck:
    try:
        observation = operation()
        return DoctorCheck(
            check_id, category, "required", "pass", _redact(observation, probes), None
        )
    except Exception as error:
        # A doctor check is an isolation boundary: one failed host probe must not
        # suppress unrelated safe checks. The bounded, redacted fallback is
        # covered by focused failure-accumulation tests.
        return DoctorCheck(
            check_id,
            category,
            "required",
            "fail",
            _redact(error, probes),
            remediation,
        )


def _path_available(probes: HostProbes, path: str | Path, *, kind: str | None = None) -> str:
    metadata = probes.path_metadata(path)
    if not metadata.get("exists"):
        raise FileNotFoundError(f"path unavailable: {metadata.get('path', path)}")
    if kind == "file" and not metadata.get("is_file"):
        raise ValueError(f"required file is not a file: {metadata.get('path', path)}")
    if kind == "dir" and not metadata.get("is_dir"):
        raise ValueError(f"required directory is not a directory: {metadata.get('path', path)}")
    return f"availability verified: {metadata.get('path', path)}"


def _root_writable(probes: HostProbes, path: str | Path) -> str:
    target = probes.path_metadata(path)
    if target.get("exists") and not target.get("is_dir"):
        raise ValueError(f"configured root is not a directory: {target.get('path', path)}")
    metadata = probes.writable_parent(path)
    if not metadata.get("writable"):
        raise PermissionError(f"no writable existing parent: {metadata.get('path', path)}")
    return f"writable parent: {metadata.get('path', path)}; target: {path}"


def _coordinate_check(context: ProjectContext, target_id: str) -> str:
    target = assert_target_structure_ready(context.config, target_id)
    structure = target.get("structure") or {}
    coordinate_path = structure.get("coordinate_path")
    expected = str(structure.get("coordinate_sha256") or "").lower()
    if not coordinate_path or len(expected) != 64:
        raise ValueError(f"target {target_id} lacks an approved coordinate artifact and SHA-256")
    actual = file_sha256(coordinate_path)
    if actual != expected:
        raise ValueError(f"target {target_id} coordinate SHA-256 differs from approval")
    return f"approved coordinate SHA-256 verified: {Path(coordinate_path).absolute()}"


def _store_check(context: ProjectContext, probes: HostProbes) -> str:
    database = context.resolve_paths().database_path
    if database is None:
        raise ValueError("project runtime has no explicit Store target")
    metadata = probes.path_metadata(database)
    if metadata.get("exists"):
        validate_storage_backend(database, project_id=context.project_id)
        return f"existing Store passed read-only preflight: {database}"
    parent = probes.writable_parent(database)
    if not parent.get("writable"):
        raise PermissionError(f"Store target has no writable existing parent: {parent.get('path')}")
    return (
        "store_will_initialize_on_launch; "
        f"target: {database}; writable parent: {parent.get('path')}"
    )


def _project_checks(
    project_path: str | Path, host: HostProbes
) -> tuple[ProjectContext | None, list[DoctorCheck]]:
    checks: list[DoctorCheck] = []
    context: ProjectContext | None = None
    try:
        context = ProjectContext.from_runtime(path=project_path)
        assert_project_approved(context.config)
        checks.append(DoctorCheck(
            "project.approval", "project", "required", "pass",
            f"approved project: {context.project_id}", None,
        ))
    except Exception as error:
        checks.append(DoctorCheck(
            "project.approval", "project", "required", "fail",
            _redact(error, host), "Project owner: validate and approve the selected project",
        ))
    if context is not None:
        for target_id in required_target_ids(context.config):
            slug = "".join(character.lower() if character.isalnum() else "-" for character in target_id).strip("-")
            checks.append(_check(
                f"project.coordinates.{slug}", "project", host,
                lambda target_id=target_id: _coordinate_check(context, target_id),
                f"Project owner: materialize and re-approve coordinates for {target_id}",
            ))
        checks.append(_check(
            "store.formal", "storage", host, lambda: _store_check(context, host),
            "Data owner: configure a usable project Store or migrate the existing Store",
        ))
        resolved_paths = context.resolve_paths()
        for check_id, path in {
            "runtime.root.data": resolved_paths.data_dir,
            "runtime.root.evidence": resolved_paths.evidence_dir,
        }.items():
            checks.append(_check(
                check_id, "filesystem", host,
                lambda path=path: _root_writable(host, path),
                "Data owner: configure a root with a writable existing parent",
            ))
    else:
        checks.append(DoctorCheck(
            "store.formal", "storage", "required", "skipped",
            "project authority unavailable", "Project owner: fix project input first",
        ))
    return context, checks


def _independent_host_checks(host: HostProbes) -> list[DoctorCheck]:
    checks = [
        _check(
            "runtime.root.diagnostics", "filesystem", host,
            lambda: _root_writable(host, resolve_diagnostics_root()),
            "Workflow owner: configure a diagnostics root with a writable existing parent",
        ),
        _check(
            "runtime.gpu", "host", host,
            lambda: "visible GPU: " + json.dumps(host.gpu_observation(15), sort_keys=True),
            "Host owner: expose an NVIDIA GPU and working driver to this process",
        ),
    ]
    key_present = bool(host.getenv("OPENAI_API_KEY"))
    checks.append(DoctorCheck(
        "credential.openai_api_key", "research", "required",
        "pass" if key_present else "fail",
        "OPENAI_API_KEY is configured" if key_present else "OPENAI_API_KEY is missing",
        None if key_present else "Research owner: configure OPENAI_API_KEY for fresh_full_launcher",
    ))
    return checks


def _configured_path_checks(
    config: ExecutionConfig, host: HostProbes
) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    roots = {
        "runtime.root.execution": config.execution_root,
        "runtime.root.design": Path(design_config.DEFAULT_OUTPUT_DIR),
        "runtime.root.prediction_artifacts": config.prediction_artifacts_root,
        "runtime.root.prediction_runs": config.prediction_runs_root,
    }
    for check_id, path in roots.items():
        checks.append(_check(
            check_id, "filesystem", host, lambda path=path: _root_writable(host, path),
            "Runtime owner: configure a root with a writable existing parent",
        ))

    entrypoints = {
        "runtime.python.core": config.core_python,
        "runtime.python.design": config.design_python,
        "runtime.python.design_refold": Path(design_config.CYCPEP_PYTHON),
        "runtime.python.prediction": config.prediction_python,
        "runtime.python.rfdiffusion": Path(design_config.RFDIFF_PYTHON),
    }
    for check_id, path in entrypoints.items():
        checks.append(_check(
            check_id, "execution", host,
            lambda path=path: _path_available(host, path, kind="file"),
            "Runtime owner: configure the required Python entry point",
        ))
    return checks


def _scientific_path_checks(
    config: ExecutionConfig, host: HostProbes
) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    rfdiffusion_dir = Path(design_config.RFDIFF_DIR)
    rfdiffusion_environment = Path(design_config.RFDIFF_CONDA)
    rfdiffusion_python_version = host.getenv("RFDIFF_PYTHON_VERSION") or "3.10"
    ligandmpnn_dir = Path(design_config.LIGANDMPNN_DIR)
    paths = {
        "runtime.design.rfdiffusion_environment": (rfdiffusion_environment, "dir"),
        "runtime.design.rfdiffusion_site_packages": (
            rfdiffusion_environment
            / "lib"
            / f"python{rfdiffusion_python_version}"
            / "site-packages",
            "dir",
        ),
        "runtime.design.rfdiffusion_repository": (rfdiffusion_dir, "dir"),
        "runtime.design.rfdiffusion_entrypoint": (
            rfdiffusion_dir / "scripts" / "run_inference.py", "file"
        ),
        "runtime.design.ligandmpnn_repository": (ligandmpnn_dir, "dir"),
        "runtime.design.ligandmpnn_entrypoint": (ligandmpnn_dir / "run.py", "file"),
        "runtime.design.ligandmpnn_checkpoint": (Path(design_config.LIGANDMPNN_CHECKPOINT), "file"),
        "runtime.design.se3_root": (Path(design_config.SE3_ROOT), "dir"),
        "runtime.design.cuda": (Path(design_config.CUDA_DATA_DIR), "dir"),
        "runtime.colabdesign_params": (config.colabdesign_params, "dir"),
        "runtime.boltz_cache": (config.boltz_cache, "dir"),
        "runtime.cuda": (config.cuda_data_dir, "dir"),
    }
    for check_id, (path, kind) in paths.items():
        checks.append(_check(
            check_id, "scientific_runtime", host,
            lambda path=path, kind=kind: _path_available(host, path, kind=kind),
            "Runtime owner: provision the configured dependency path",
        ))
    checks.append(_check(
        "runtime.prediction.required_paths", "scientific_runtime", host,
        lambda: "required Prediction paths available: " + ", ".join(
            sorted(validate_required_prediction_tool_paths(config))
        ),
        "Prediction owner: configure all required full-Prediction tool paths",
    ))
    return checks


def _scientific_identity_checks(
    config: ExecutionConfig, host: HostProbes
) -> list[DoctorCheck]:
    prediction_config = PredictionConfig()
    return [
        _check(
            "runtime.colabdesign", "scientific_runtime", host,
            lambda: "identity verified; commit=" + validate_colabdesign_runtime(
                config.colabdesign_dir, expected_commit=prediction_config.colabdesign_commit
            ),
            "Prediction owner: install the production ColabDesign revision in a clean checkout",
        ),
        _check(
            "runtime.boltz", "scientific_runtime", host,
            lambda: "identity verified; " + json.dumps(validate_boltz_runtime(
                config.boltz_executable, config.boltz_checkpoint,
                timeout=min(config.prediction_timeout_seconds, 60),
            ), sort_keys=True),
            "Prediction owner: install the production Boltz distribution and checkpoint",
        ),
        _check(
            "runtime.pyrosetta", "scientific_runtime", host,
            lambda: "identity verified; version=" + validate_pyrosetta_runtime(config.pyrosetta_python),
            "Prediction owner: install authorized production PyRosetta",
        ),
        _check(
            "runtime.prodigy", "scientific_runtime", host,
            lambda: "identity verified; version=" + validate_prodigy_runtime(
                config.prodigy_executable, PRODIGY_VERSION
            ),
            "Prediction owner: install the production PRODIGY distribution",
        ),
    ]


def run_doctor(
    project_path: str | Path,
    *,
    probes: HostProbes | None = None,
) -> DoctorReport:
    """Evaluate launch readiness without creating or modifying formal state."""

    host = probes or DefaultHostProbes()
    _context, checks = _project_checks(project_path, host)
    checks.extend(_independent_host_checks(host))

    try:
        config = ExecutionConfig.from_environment()
    except Exception as error:
        checks.append(DoctorCheck(
            "runtime.execution_config", "execution", "required", "fail",
            _redact(error, host), "Execution owner: correct runtime environment selectors",
        ))
        return DoctorReport(PROFILE, str(project_path), tuple(checks))
    checks.extend(_configured_path_checks(config, host))
    checks.extend(_scientific_path_checks(config, host))
    checks.extend(_scientific_identity_checks(config, host))
    return DoctorReport(PROFILE, str(project_path), tuple(checks))


def invalid_doctor_report(project_path: str, observation: str = "invalid doctor input") -> DoctorReport:
    return DoctorReport(PROFILE, project_path, (
        DoctorCheck(
            "doctor.input", "input", "required", "fail", observation,
            "Operator: provide --project with a readable approved project JSON path",
        ),
    ))


def internal_doctor_report(project_path: str) -> DoctorReport:
    """Return a sanitized CLI-boundary report for an unexpected doctor failure."""

    return DoctorReport(PROFILE, project_path, (
        DoctorCheck(
            "doctor.runtime", "doctor", "required", "fail",
            "doctor execution failed before readiness could be determined",
            "Runtime owner: inspect the local doctor error log and repair the runtime",
        ),
    ))


def render_doctor_text(report: DoctorReport) -> str:
    lines = [f"Runtime readiness ({report.profile})", f"Project: {report.project_path}"]
    for check in report.checks:
        lines.append(
            f"[{check.status.upper()}] {check.id} "
            f"(category={check.category}, requirement={check.requirement}): "
            f"{check.observation}"
        )
        if check.remediation and check.status != "pass":
            lines.append(f"  Next: {check.remediation}")
    lines.append("READY" if report.ready else "NOT READY")
    return "\n".join(lines) + "\n"


def render_doctor_json(report: DoctorReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"


__all__ = [
    "DefaultHostProbes",
    "DoctorCheck",
    "DoctorReport",
    "HostProbes",
    "invalid_doctor_report",
    "internal_doctor_report",
    "render_doctor_json",
    "render_doctor_text",
    "run_doctor",
]
