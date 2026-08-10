"""Direct-addressed persistence for the non-authoritative diagnostic journal."""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Mapping

from execution.supervisor import durable_atomic_json

from .errors import DiagnosticContractError
from .locking import exclusive_file_lock
from .models import DiagnosticReport


_LAUNCHER_RUN_ID_RE = re.compile(r"^launcher_[0-9a-f]{32}$")
DurableWriter = Callable[[Path, dict], None]


def resolve_diagnostics_root(
    *, env: Mapping[str, str] | None = None, repository_root: str | Path | None = None
) -> Path:
    environment = os.environ if env is None else env
    configured = environment.get("CYCPEP_LAUNCHER_DIAGNOSTICS")
    if configured:
        return Path(configured).expanduser()
    np_data = environment.get("NP_DATA")
    if np_data:
        return Path(np_data).expanduser() / "launcher_diagnostics"
    base = Path(repository_root) if repository_root is not None else Path(__file__).resolve().parents[1]
    return base / "data" / "launcher_diagnostics"


def validate_launcher_run_id(launcher_run_id: str) -> str:
    if not isinstance(launcher_run_id, str) or not _LAUNCHER_RUN_ID_RE.fullmatch(launcher_run_id):
        raise DiagnosticContractError("launcher_run_id_invalid", "Invalid launcher run identifier.")
    return launcher_run_id


class DiagnosticStore:
    """Atomic journal storage addressed by one exact validated run identifier."""

    def __init__(self, root: str | Path, *, durable_writer: DurableWriter = durable_atomic_json):
        self.root = Path(root)
        self._durable_writer = durable_writer

    def _path(self, launcher_run_id: str) -> Path:
        return self.root / f"{validate_launcher_run_id(launcher_run_id)}.json"

    def _lock_path(self, launcher_run_id: str) -> Path:
        return self.root / f".{validate_launcher_run_id(launcher_run_id)}.lock"

    @contextmanager
    def exclusive(self, launcher_run_id: str) -> Iterator[None]:
        try:
            with exclusive_file_lock(self._lock_path(launcher_run_id)):
                yield
        except TimeoutError as error:
            raise DiagnosticContractError(
                "launcher_run_locked", "Launcher run is already being coordinated."
            ) from error

    @contextmanager
    def locked(self, launcher_run_id: str) -> Iterator["LockedDiagnosticSession"]:
        """Hold one run lock across formal inspection and one continuation.

        The yielded session is bound to the validated run identifier and does
        not expose the underlying diagnostic path.
        """

        with self.exclusive(launcher_run_id):
            yield LockedDiagnosticSession(self, launcher_run_id)

    def create(self, report: DiagnosticReport) -> DiagnosticReport:
        path = self._path(report.launcher_run_id)
        with self.exclusive(report.launcher_run_id):
            if path.exists():
                raise DiagnosticContractError(
                    "launcher_diagnostic_already_exists", "Launcher diagnostic already exists."
                )
            self._persist(path, report)
        return report

    def write(self, report: DiagnosticReport) -> DiagnosticReport:
        path = self._path(report.launcher_run_id)
        with self.exclusive(report.launcher_run_id):
            if not path.is_file():
                raise DiagnosticContractError(
                    "launcher_diagnostic_not_found", "Launcher diagnostic was not found."
                )
            self._persist(path, report)
        return report

    def read(self, launcher_run_id: str) -> DiagnosticReport:
        return self._read_unlocked(launcher_run_id)

    def _read_unlocked(self, launcher_run_id: str) -> DiagnosticReport:
        path = self._path(launcher_run_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            report = DiagnosticReport.from_dict(value)
        except FileNotFoundError as error:
            raise DiagnosticContractError(
                "launcher_diagnostic_not_found", "Launcher diagnostic was not found."
            ) from error
        except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
            raise DiagnosticContractError(
                "launcher_diagnostic_invalid", "Launcher diagnostic cannot be validated."
            ) from error
        if report.launcher_run_id != launcher_run_id:
            raise DiagnosticContractError(
                "launcher_diagnostic_binding_mismatch", "Launcher diagnostic binding is invalid."
            )
        return report

    def _write_unlocked(self, report: DiagnosticReport) -> DiagnosticReport:
        path = self._path(report.launcher_run_id)
        if not path.is_file():
            raise DiagnosticContractError(
                "launcher_diagnostic_not_found", "Launcher diagnostic was not found."
            )
        self._persist(path, report)
        return report

    def _persist(self, path: Path, report: DiagnosticReport) -> None:
        try:
            self._durable_writer(path, report.to_dict())
        except OSError as error:
            raise DiagnosticContractError(
                "launcher_diagnostic_persistence_failed", "Launcher diagnostic could not be persisted."
            ) from error


class LockedDiagnosticSession:
    """Run-bound read/write view available only while its store lock is held."""

    def __init__(self, store: DiagnosticStore, launcher_run_id: str):
        self._store = store
        self.launcher_run_id = validate_launcher_run_id(launcher_run_id)

    def read(self) -> DiagnosticReport:
        return self._store._read_unlocked(self.launcher_run_id)

    def write(self, report: DiagnosticReport) -> DiagnosticReport:
        if report.launcher_run_id != self.launcher_run_id:
            raise DiagnosticContractError(
                "launcher_diagnostic_binding_mismatch", "Launcher diagnostic binding is invalid."
            )
        return self._store._write_unlocked(report)


__all__ = [
    "DiagnosticStore", "DurableWriter", "LockedDiagnosticSession", "resolve_diagnostics_root",
    "validate_launcher_run_id",
]
