"""Command-line adapter for the Workflow Launcher and readiness doctor."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from typing import Callable, Sequence, TextIO

from .errors import normalize_error
from .models import BrowserResult, LauncherCommandResult


Command = Callable[..., LauncherCommandResult]
DoctorCommand = Callable[..., object]
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandHandlers:
    launch_project: Command
    status_launcher_run: Command
    resume_launcher_run: Command


class _JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _JSONArgumentParser(prog="python -m workflow", add_help=True)
    commands = parser.add_subparsers(dest="command", required=True)
    launch = commands.add_parser("launch", add_help=True)
    launch.add_argument("--project", required=True)
    status = commands.add_parser("status", add_help=True)
    status.add_argument("--launcher-run", required=True)
    resume = commands.add_parser("resume", add_help=True)
    resume.add_argument("--launcher-run", required=True)
    resume.add_argument("--approval", action="append", default=[])
    resume.add_argument("--retry-bootstrap-prediction", action="store_true")
    doctor = commands.add_parser("doctor", add_help=True)
    doctor.add_argument("--project", required=True)
    doctor.add_argument("--json", action="store_true")
    return parser


def _default_handlers() -> CommandHandlers:
    def launch_project(**kwargs) -> LauncherCommandResult:
        from .service import launch_project as service_call

        return service_call(**kwargs)

    def status_launcher_run(**kwargs) -> LauncherCommandResult:
        from .service import status_launcher_run as service_call

        return service_call(**kwargs)

    def resume_launcher_run(**kwargs) -> LauncherCommandResult:
        from .service import resume_launcher_run as service_call

        return service_call(**kwargs)

    return CommandHandlers(launch_project, status_launcher_run, resume_launcher_run)


def _invalid_input(error: BaseException) -> LauncherCommandResult:
    return LauncherCommandResult(
        payload=BrowserResult(status="failed", error=normalize_error(error, component="launcher")),
        exit_code=2,
    )


def _dispatch(arguments: argparse.Namespace, handlers: CommandHandlers) -> LauncherCommandResult:
    if arguments.command == "launch":
        return handlers.launch_project(project_path=arguments.project)
    if arguments.command == "status":
        return handlers.status_launcher_run(launcher_run_id=arguments.launcher_run)
    return handlers.resume_launcher_run(
        launcher_run_id=arguments.launcher_run,
        approval_paths=tuple(arguments.approval),
        retry_bootstrap_prediction=arguments.retry_bootstrap_prediction,
    )


def _run_doctor_command(
    arguments: argparse.Namespace,
    doctor_handler: DoctorCommand | None,
    destination: TextIO,
) -> int:
    from .doctor import render_doctor_json, render_doctor_text, run_doctor

    runtime_failed = False
    try:
        report = (doctor_handler or run_doctor)(project_path=arguments.project)
    except Exception as error:
        from .doctor import internal_doctor_report

        runtime_failed = True
        report = internal_doctor_report(arguments.project)
        _LOGGER.error("doctor runtime failed: %s", type(error).__name__)
    destination.write(
        render_doctor_json(report) if arguments.json else render_doctor_text(report)
    )
    return 2 if runtime_failed else report.exit_code


def main(
    argv: Sequence[str] | None = None,
    *,
    handlers: CommandHandlers | None = None,
    doctor_handler: DoctorCommand | None = None,
    stdout: TextIO | None = None,
) -> int:
    destination = stdout or sys.stdout
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    doctor_requested = bool(raw_arguments and raw_arguments[0] == "doctor")
    try:
        arguments = _parser().parse_args(raw_arguments)
    except Exception as error:
        if doctor_requested:
            from .doctor import invalid_doctor_report, render_doctor_json, render_doctor_text

            report = invalid_doctor_report("", "invalid doctor input")
            wants_json = "--json" in raw_arguments
            destination.write(
                render_doctor_json(report) if wants_json else render_doctor_text(report)
            )
            _LOGGER.error("doctor input rejected: %s", type(error).__name__)
            return 2
        result = _invalid_input(error)
    else:
        if arguments.command == "doctor":
            return _run_doctor_command(arguments, doctor_handler, destination)
        try:
            result = _dispatch(arguments, handlers or _default_handlers())
            if not isinstance(result, LauncherCommandResult):
                raise TypeError("Launcher service must return LauncherCommandResult")
        except Exception as error:
            # The CLI is the final browser-facing boundary: unexpected service or
            # import failures are normalized once and never resumed later.
            result = _invalid_input(error)
    if result.payload.error is not None:
        normalized = result.payload.error
        _LOGGER.error(
            "launcher CLI failed: code=%s component=%s message=%s",
            normalized.code,
            normalized.component,
            normalized.message,
        )
    destination.write(json.dumps(result.payload.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")
    return result.exit_code


__all__ = ["CommandHandlers", "main"]
