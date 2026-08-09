import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import web_api.server as server
from storage import SQLiteStore
from test_workbench import FakeStore


class WebApiTrustBoundaryTests(unittest.TestCase):
    @staticmethod
    def _database_dump(path):
        connection = sqlite3.connect(path)
        try:
            return "\n".join(connection.iterdump())
        finally:
            connection.close()

    def _request(self, method, path, *, store=None, body=None):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        if store is not None:
            httpd.workbench_store = store
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            connection = HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
            encoded = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"} if encoded is not None else {}
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            payload = (
                json.loads(raw)
                if response.getheader("Content-Type", "").startswith("application/json")
                else raw
            )
            connection.close()
            return response.status, payload
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_v2_workbench_uses_existing_success_envelope(self):
        status, payload = self._request(
            "GET",
            "/api/v2/workbench",
            store=FakeStore(state={"project_id": "project-1", "project": "Demo"}),
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["request_id"].startswith("req_"))
        self.assertEqual(payload["data"]["schema_version"], "frontend.workbench.v2")
        self.assertEqual(payload["data"]["project"]["project_id"], "project-1")

    def test_v2_exposes_only_the_exploration_shortlist_payload_contract(self):
        shortlist_payload = {
            "k": 2,
            "n_evaluated": 12,
            "n_passed": 4,
            "shortlist": [{"candidate_id": "C1"}, {"candidate_id": "C2"}],
            "calibration": {"status": "calibrated"},
            "source_event_ids": ["evt-source-1"],
            "unmapped_metrics": ["novel_metric"],
        }
        evidence = [
            {
                "event_id": "evt-shortlist",
                "event_type": "exploration_shortlist",
                "project_id": "project-1",
                **shortlist_payload,
            },
            {
                "event_id": "evt-other",
                "event_type": "other_event",
                "project_id": "project-1",
                **shortlist_payload,
            },
        ]

        status, payload = self._request(
            "GET",
            "/api/v2/workbench",
            store=FakeStore(state={"project_id": "project-1"}, evidence=evidence),
        )

        self.assertEqual(status, 200)
        items = {item["event_id"]: item for item in payload["data"]["evidence"]["items"]}
        for key, value in shortlist_payload.items():
            self.assertEqual(items["evt-shortlist"][key], value)
        self.assertNotIn("shortlist", items["evt-other"])
        self.assertNotIn("unmapped_metrics", items["evt-other"])

    def test_v2_invalid_binding_has_one_partial_response_contract(self):
        with tempfile.TemporaryDirectory() as root_dir:
            store = SQLiteStore(Path(root_dir) / "store.db", project_id="project-1")
            store.update_state("project-1", {
                "project_id": "project-1",
                "orchestrator": {"run_path": "internal/secret.json"},
            })
            store.upsert({"candidate_id": "C1", "sequence": "AAAA"})
            status, payload = self._request("GET", "/api/v2/workbench", store=store)

        self.assertEqual(status, 200)
        data = payload["data"]
        self.assertIsNone(data["workflow"])
        self.assertIsNone(data["run"])
        self.assertEqual(data["tasks"]["items"], [])
        self.assertEqual(data["candidates"]["items"][0]["candidate_id"], "C1")
        self.assertEqual(data["blockers"]["items"][0]["code"], "workflow_binding_invalid")
        self.assertNotIn("secret", str(data))

    def test_v2_get_does_not_mutate_formal_or_projection_state(self):
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            store = SQLiteStore(root / "store.db", project_id="project-1")
            store.update_state("project-1", {"project_id": "project-1", "project": "Demo"})
            projection = root / "state.json"
            projection.write_text('{"compatibility":"unchanged"}', encoding="utf-8")
            projection_before = (projection.read_bytes(), projection.stat().st_mtime_ns)
            database_before = self._database_dump(store.path)
            artifacts_before = dict(server.ARTIFACTS)

            status, payload = self._request("GET", "/api/v2/workbench", store=store)

            database_after = self._database_dump(store.path)
            self.assertEqual(status, 200)
            self.assertEqual(payload["data"]["project"]["project_id"], "project-1")
            self.assertEqual(database_after, database_before)
            self.assertEqual((projection.read_bytes(), projection.stat().st_mtime_ns), projection_before)
            self.assertEqual(server.ARTIFACTS, artifacts_before)

    def test_v2_has_no_mutation_routes(self):
        status, payload = self._request(
            "POST",
            "/api/v2/workbench/retry",
            store=FakeStore(state={"project_id": "project-1"}),
        )

        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_v1_health_envelope_remains_compatible(self):
        status, payload = self._request("GET", "/api/v1/health")

        self.assertEqual(status, 200)
        self.assertEqual(payload["data"], {"status": "ok", "adapter": "local"})
        self.assertTrue(payload["request_id"].startswith("req_"))

    def test_v1_read_routes_remain_compatible(self):
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            drafts = root / "drafts"
            drafts.mkdir()
            (drafts / "drf_demo.json").write_text(
                json.dumps({"draft_id": "drf_demo", "project_id": "draft-project", "name": "Draft"}),
                encoding="utf-8",
            )
            coordinate = root / "candidate.pdb"
            coordinate.write_bytes(b"ATOM\n")
            artifact_id = "art_" + "a" * 24
            artifact = {"path": coordinate, "sha256": "existing", "format": "pdb", "candidate_id": "C1"}
            with patch.object(server, "DRAFTS", drafts), patch.object(
                server.State, "load", return_value={"project_id": "project-1", "project": "Demo"}
            ), patch.object(server, "local_snapshot", return_value={"source": {"mode": "local"}}), patch.dict(
                server.ARTIFACTS, {artifact_id: artifact}, clear=True
            ):
                projects_status, projects = self._request("GET", "/api/v1/projects")
                snapshot_status, snapshot = self._request("GET", "/api/v1/snapshot")
                draft_status, draft = self._request("GET", "/api/v1/project-drafts/drf_demo")
                artifact_status, artifact_payload = self._request(
                    "GET", f"/api/v1/artifacts/{artifact_id}/coordinates"
                )

        self.assertEqual((projects_status, snapshot_status, draft_status, artifact_status), (200, 200, 200, 200))
        self.assertEqual(projects["data"][0]["project_id"], "project-1")
        self.assertEqual(snapshot["data"], {"source": {"mode": "local"}})
        self.assertEqual(draft["data"]["draft_id"], "drf_demo")
        self.assertEqual(artifact_payload, b"ATOM\n")

    def test_v1_write_adapter_routes_remain_compatible(self):
        with tempfile.TemporaryDirectory() as root_dir:
            drafts = Path(root_dir) / "drafts"
            bootstrapper = Mock()
            bootstrapper.create_draft.return_value = {"project_id": "project-1", "name": "Demo"}
            with patch.object(server, "DRAFTS", drafts), patch.object(
                server, "TargetBootstrapper", return_value=bootstrapper
            ), patch.object(server, "ssh_snapshot", return_value={"source": {"mode": "ssh"}}), patch.object(
                server, "approve_draft", return_value={"review": {"status": "approved"}}
            ), patch.object(server, "edit_target_draft", return_value={"targets": [{"id": "MDM2"}]}), patch.dict(
                server.CONNECTIONS, {}, clear=True
            ):
                create_status, created = self._request(
                    "POST", "/api/v1/project-drafts", body={"identifier": "Q00987"}
                )
                ssh_status, ssh = self._request(
                    "POST", "/api/v1/connections/ssh", body={
                        "host": "example.org", "username": "user", "port": 22,
                        "key_alias": "gpu1", "workspace_root": "/work",
                    }
                )
                connection_id = ssh["data"]["connection_id"]
                refresh_status, refreshed = self._request(
                    "POST", "/api/v1/connections/ssh/snapshot", body={"connection_id": connection_id}
                )
                draft_id = created["data"]["draft_id"]
                approve_status, approved = self._request(
                    "POST", f"/api/v1/project-drafts/{draft_id}/approve", body={"force": False}
                )
                patch_status, patched = self._request(
                    "PATCH", f"/api/v1/project-drafts/{draft_id}/targets/MDM2", body={"binding_site": {}}
                )

        self.assertEqual((create_status, ssh_status, refresh_status, approve_status, patch_status), (201, 200, 200, 200, 200))
        self.assertTrue(created["data"]["draft_id"].startswith("drf_"))
        self.assertEqual(refreshed["data"]["source"]["mode"], "ssh")
        self.assertEqual(approved["data"]["review"]["status"], "approved")
        self.assertEqual(patched["data"]["targets"][0]["id"], "MDM2")

    def test_ssh_candidate_payload_never_registers_local_artifact(self):
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            coordinate = root / "candidate.pdb"
            manifest = root / "manifest.json"
            coordinate.write_text("ATOM\n", encoding="utf-8")
            digest = hashlib.sha256(coordinate.read_bytes()).hexdigest()
            row = {
                "candidate_id": "cand-1",
                "sequence": "AAAA",
                "all_layers_pass": True,
                "design_pdb_path": str(coordinate),
                "design_pdb_hash": digest,
                "manifest_path": str(manifest),
            }
            manifest.write_text(json.dumps({
                "candidate_id": "cand-1",
                "sequence": "AAAA",
                "length": 4,
                "refold_pdb": str(coordinate),
                "refold_pdb_hash": digest,
                "manifest_path": str(manifest),
            }), encoding="utf-8")
            with patch.object(server, "ROOT", root), patch.dict(
                os.environ, {"CYCPEP_ARTIFACT_ROOTS": str(root)}, clear=False
            ):
                self.assertIsNotNone(server._candidate_payload(row)["artifact_id"])
                self.assertIsNone(server._candidate_payload(row, allow_artifacts=False)["artifact_id"])

    def test_manifest_identity_and_hash_are_required(self):
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            coordinate = root / "candidate.pdb"
            manifest = root / "manifest.json"
            coordinate.write_text("ATOM\n", encoding="utf-8")
            digest = hashlib.sha256(coordinate.read_bytes()).hexdigest()
            row = {
                "candidate_id": "cand-1",
                "sequence": "AAAA",
                "all_layers_pass": True,
                "design_pdb_path": str(coordinate),
                "design_pdb_hash": digest,
                "manifest_path": str(manifest),
            }
            manifest.write_text(json.dumps({
                "candidate_id": "cand-other",
                "sequence": "AAAA",
                "refold_pdb": str(coordinate),
                "refold_pdb_hash": digest,
                "manifest_path": str(manifest),
            }), encoding="utf-8")
            with patch.object(server, "ROOT", root), patch.dict(
                os.environ, {"CYCPEP_ARTIFACT_ROOTS": str(root)}, clear=False
            ):
                self.assertIsNone(server._candidate_payload(row)["artifact_id"])

    def test_remote_bind_requires_explicit_opt_in(self):
        self.assertTrue(server._bind_host_is_loopback("127.0.0.1"))
        self.assertTrue(server._bind_host_is_loopback("::1"))
        self.assertFalse(server._bind_host_is_loopback("0.0.0.0"))
        self.assertFalse(server._bind_host_is_loopback("192.0.2.10"))


if __name__ == "__main__":
    unittest.main()
