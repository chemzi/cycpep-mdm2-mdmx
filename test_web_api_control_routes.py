"""HTTP route tests for browser project launch control."""

from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import Mock

import web_api.server as server
from workflow.errors import DiagnosticContractError


LAUNCHER_ID = "launcher_0123456789abcdef0123456789abcdef"
DIGEST = "a" * 64


def _control_view(status="awaiting_approval", *, failure=None):
    return {
        "launcher": {
            "schema_version": 1,
            "status": status,
            "launcher_run_id": LAUNCHER_ID,
            "project_id": "project-1",
            "approved_content_binding": DIGEST,
            "boundary": "approval",
            "prediction_invocation_id": None,
            "prediction_run_id": None,
            "formal_trace": {
                "project_id": None, "workflow_id": None,
                "plan_id": None, "run_id": None,
            },
            "evidence_ids": [],
            "artifact_ids": [],
            "required_task_ids": ["T001"],
            "task_status_counts": {},
            "last_known_formal_status": "awaiting_approval",
            "error": None,
        },
        "approval_control": None,
        "control_failure": failure,
    }


class WebApiControlRouteTests(unittest.TestCase):
    def _request(
        self, method, path, *, service=None, scoped_reader=None,
        body=None, raw_body=None, store=None,
    ):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        if service is not None:
            httpd.project_control_service = service
        if scoped_reader is not None:
            httpd.launcher_workbench_reader = scoped_reader
        if store is not None:
            httpd.workbench_store = store
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            connection = HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
            encoded = raw_body if raw_body is not None else (
                json.dumps(body).encode() if body is not None else None
            )
            headers = {
                "Content-Type": "application/json"
            } if encoded is not None else {}
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            return response.status, payload
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_all_control_routes_use_existing_success_envelope(self):
        service = Mock()
        draft = {"draft_id": "drf_demo", "project_id": "project-1"}
        service.create_draft.return_value = draft
        service.retrieve_draft.return_value = draft
        service.approve_project.return_value = {
            **draft, "review": {"status": "approved"}
        }
        service.launch_project.return_value = _control_view()
        service.status.return_value = _control_view()
        service.approve_and_continue.return_value = _control_view("running")
        service.continue_run.return_value = _control_view("awaiting_approval")
        approval_body = {
            "launcher_run_id": LAUNCHER_ID,
            "project_id": "project-1",
            "approved_content_binding": DIGEST,
            "plan_id": "planner_0123456789ab",
            "plan_sha256": DIGEST,
            "required_task_ids": ["T001"],
            "approver": "operator",
            "justification": "Reviewed exact plan.",
            "ceilings": {"max_gpu_job_slots": 1, "max_gpu_minutes": 20.0},
        }
        requests = (
            ("POST", "/api/v2/control/project-drafts", {"target_identifier": "MDM2"}, 201),
            ("GET", "/api/v2/control/project-drafts/drf_demo", None, 200),
            ("POST", "/api/v2/control/project-drafts/drf_demo/approve", {}, 200),
            (
                "POST", "/api/v2/control/project-drafts/drf_demo/launch",
                {"launcher_run_id": LAUNCHER_ID}, 200,
            ),
            ("GET", f"/api/v2/control/launcher-runs/{LAUNCHER_ID}", None, 200),
            (
                "POST", f"/api/v2/control/launcher-runs/{LAUNCHER_ID}/approval",
                approval_body, 200,
            ),
            (
                "POST", f"/api/v2/control/launcher-runs/{LAUNCHER_ID}/continue",
                {"launcher_run_id": LAUNCHER_ID}, 200,
            ),
        )
        for method, path, body, expected in requests:
            with self.subTest(path=path):
                status, payload = self._request(
                    method, path, service=service, body=body
                )
                self.assertEqual(status, expected)
                self.assertTrue(payload["request_id"].startswith("req_"))
                self.assertIn("data", payload)

    def test_invalid_json_ids_and_url_body_mismatch_are_400(self):
        service = Mock()
        cases = (
            ("POST", "/api/v2/control/project-drafts", None, b"{"),
            ("GET", "/api/v2/control/launcher-runs/launcher_short", None, None),
            (
                "POST", f"/api/v2/control/launcher-runs/{LAUNCHER_ID}/approval",
                {
                    "launcher_run_id": "launcher_ffffffffffffffffffffffffffffffff",
                    "project_id": "project-1",
                    "approved_content_binding": DIGEST,
                    "plan_id": "planner_0123456789ab",
                    "plan_sha256": DIGEST,
                    "required_task_ids": ["T001"],
                    "approver": "operator", "justification": "Reviewed.",
                    "ceilings": {},
                },
                None,
            ),
            (
                "POST", "/api/v2/control/project-drafts/drf_demo/launch",
                {"draft_id": "drf_other", "launcher_run_id": LAUNCHER_ID}, None,
            ),
        )
        for method, path, body, raw in cases:
            with self.subTest(path=path, body=body):
                status, payload = self._request(
                    method, path, service=service, body=body, raw_body=raw
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "validation_error")

    def test_control_failure_has_error_status_and_safe_control_view(self):
        service = Mock()
        failure = {
            "code": "approval_ceiling_exceeded",
            "category": "ceiling",
            "component": "planner",
            "message": "Ceiling exceeded.",
            "ceiling": "max_gpu_minutes",
        }
        service.launch_project.return_value = _control_view(failure=failure)
        status, payload = self._request(
            "POST", "/api/v2/control/project-drafts/drf_demo/launch",
            service=service, body={"launcher_run_id": LAUNCHER_ID},
        )

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "approval_ceiling_exceeded")
        self.assertEqual(
            payload["error"]["control"]["launcher"]["status"],
            "awaiting_approval",
        )

    def test_repeat_launch_delegates_same_identity_for_recovery(self):
        service = Mock()
        service.launch_project.return_value = _control_view("running")
        body = {"launcher_run_id": LAUNCHER_ID}
        first = self._request(
            "POST", "/api/v2/control/project-drafts/drf_demo/launch",
            service=service, body=body,
        )
        second = self._request(
            "POST", "/api/v2/control/project-drafts/drf_demo/launch",
            service=service, body=body,
        )

        self.assertEqual((first[0], second[0]), (200, 200))
        self.assertEqual(service.launch_project.call_count, 2)
        for call in service.launch_project.call_args_list:
            self.assertEqual(call.args[1].launcher_run_id, LAUNCHER_ID)

    def test_scoped_workbench_never_falls_back_and_sanitizes_failure(self):
        scoped = Mock(side_effect=DiagnosticContractError(
            "control_binding_conflict",
            "Traceback C:/secret/project.json token=abc",
        ))
        startup_store = Mock()
        status, payload = self._request(
            "GET", f"/api/v2/workbench?launcher_run_id={LAUNCHER_ID}",
            scoped_reader=scoped, store=startup_store,
        )

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "control_binding_conflict")
        self.assertNotIn("secret", payload["error"]["message"])
        self.assertNotIn("abc", payload["error"]["message"])
        startup_store.query.assert_not_called()

    def test_scoped_workbench_success_and_invalid_query(self):
        scoped = Mock(return_value={"schema_version": "frontend.workbench.v2"})
        success, payload = self._request(
            "GET", f"/api/v2/workbench?launcher_run_id={LAUNCHER_ID}",
            scoped_reader=scoped,
        )
        invalid, error = self._request(
            "GET", "/api/v2/workbench?launcher_run_id=launcher_short",
            scoped_reader=scoped,
        )

        self.assertEqual(success, 200)
        self.assertEqual(payload["data"]["schema_version"], "frontend.workbench.v2")
        self.assertEqual(invalid, 400)
        self.assertEqual(error["error"]["code"], "validation_error")
        scoped.assert_called_once_with(launcher_run_id=LAUNCHER_ID)


if __name__ == "__main__":
    unittest.main()
