import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import web_api.server as server


class WebApiTrustBoundaryTests(unittest.TestCase):
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

    def test_ssh_password_profile_does_not_require_key_alias(self):
        profile = server._validate_ssh_profile({
            "host": "example.ssh.damodel.com",
            "username": "root",
            "port": 40584,
            "password": "temporary-secret",
            "workspace_root": "/srv/cycpep",
        })
        self.assertIsNone(profile["key_path"])
        self.assertEqual(profile["password"], "temporary-secret")

    def test_workflow_start_requires_project_binding(self):
        with self.assertRaisesRegex(ValueError, "批准或切换"):
            server.ssh_start_workflow({
                "host": "example.ssh.damodel.com",
                "username": "root",
                "port": 40584,
                "password": "temporary-secret",
                "workspace_root": "/srv/cycpep",
            }, {})


if __name__ == "__main__":
    unittest.main()
