"""CLI shell tests for ``python -m workflow``."""

from __future__ import annotations

import io
import json
import unittest

from workflow.cli import CommandHandlers, main
from workflow.models import BrowserResult, LauncherCommandResult


def _result(status: str = "awaiting_approval", exit_code: int = 0) -> LauncherCommandResult:
    return LauncherCommandResult(
        payload=BrowserResult(status=status, launcher_run_id="launcher_0123456789abcdef0123456789abcdef"),
        exit_code=exit_code,
    )


class WorkflowCLITests(unittest.TestCase):
    def test_launch_emits_exactly_one_json_document(self):
        calls = []
        handlers = CommandHandlers(
            launch_project=lambda **kwargs: calls.append(("launch", kwargs)) or _result(),
            status_launcher_run=lambda **kwargs: _result(),
            resume_launcher_run=lambda **kwargs: _result(),
        )
        stdout = io.StringIO()

        code = main(
            ["launch", "--project", "approved.json"],
            handlers=handlers,
            stdout=stdout,
        )

        self.assertEqual(code, 0)
        self.assertEqual(calls, [("launch", {"project_path": "approved.json"})])
        self.assertEqual(json.loads(stdout.getvalue())["status"], "awaiting_approval")
        self.assertEqual(stdout.getvalue().count("\n"), 1)

    def test_status_and_resume_dispatch_without_approval_bypass(self):
        calls = []
        handlers = CommandHandlers(
            launch_project=lambda **kwargs: _result(),
            status_launcher_run=lambda **kwargs: calls.append(("status", kwargs)) or _result("completed"),
            resume_launcher_run=lambda **kwargs: calls.append(("resume", kwargs)) or _result("blocked", 3),
        )
        launcher_id = "launcher_0123456789abcdef0123456789abcdef"

        self.assertEqual(main(["status", "--launcher-run", launcher_id], handlers=handlers, stdout=io.StringIO()), 0)
        self.assertEqual(
            main(
                ["resume", "--launcher-run", launcher_id, "--approval", "a.json", "--approval", "b.json"],
                handlers=handlers,
                stdout=io.StringIO(),
            ),
            3,
        )
        self.assertEqual(calls[0], ("status", {"launcher_run_id": launcher_id}))
        self.assertEqual(
            calls[1],
            ("resume", {
                "launcher_run_id": launcher_id,
                "approval_paths": ("a.json", "b.json"),
                "retry_bootstrap_prediction": False,
            }),
        )

    def test_invalid_cli_input_is_one_browser_safe_json_error(self):
        stdout = io.StringIO()

        code = main(["resume"], handlers=None, stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["component"], "launcher")
        self.assertNotIn("usage:", stdout.getvalue())

    def test_unexpected_service_failure_is_sanitized_and_does_not_escape(self):
        def fail(**_):
            raise RuntimeError("token=secret-value at C:/internal/launcher/service.py")

        handlers = CommandHandlers(
            launch_project=fail,
            status_launcher_run=fail,
            resume_launcher_run=fail,
        )
        stdout = io.StringIO()

        with self.assertLogs("workflow.cli", level="ERROR") as captured:
            code = main(
                ["launch", "--project", "approved.json"],
                handlers=handlers,
                stdout=stdout,
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["code"], "RuntimeError")
        self.assertNotIn("secret-value", stdout.getvalue())
        self.assertNotIn("C:/internal", stdout.getvalue())
        self.assertNotIn("secret-value", "\n".join(captured.output))
        self.assertNotIn("C:/internal", "\n".join(captured.output))
        self.assertEqual(stdout.getvalue().count("\n"), 1)


if __name__ == "__main__":
    unittest.main()
