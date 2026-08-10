"""Single-document JSON command-line adapter for the Workflow Launcher."""

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
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    handlers: CommandHandlers | None = None,
    stdout: TextIO | None = None,
) -> int:
    destination = stdout or sys.stdout
    try:
        arguments = _parser().parse_args(argv)
        result = _dispatch(arguments, handlers or _default_handlers())
        if not isinstance(result, LauncherCommandResult):
            raise TypeError("Launcher service must return LauncherCommandResult")
    except Exception as error:
        # The CLI is the final browser-facing boundary: unexpected service or
        # import failures are normalized once, emitted once, and never resumed
        # into a later workflow boundary here.
        result = _invalid_input(error)
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
