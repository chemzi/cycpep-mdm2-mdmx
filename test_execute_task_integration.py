"""Task 10 integration test: execute_task full chain through Transaction Boundary (PR36 migration).

Drives the real ``execute_task()`` entry point with the Orchestrator surface
(claim/complete/fail) and packet validation mocked, but a REAL ExecutionWorker +
CommitManager + SQLiteStore + adapter. This proves the migration end-to-end:
execute_task -> TransactionContext -> ExecutionWorker.run -> CommitManager ->
Store.commit_transaction -> complete(transaction_managed=True).

Per the migration DoD: do NOT fake ExecutionWorker. The Orchestrator surface is
mocked only because constructing a full Planner plan + approval + dispatch packet
is out of scope for this regression; the transaction path itself is real.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contracts.candidate_update import CandidateUpdate
from contracts.transaction import TransactionContext
from execution.results import ExecutionActionResult
from execution.staging import StagedArtifact
from execution.worker import execute_task, ExecutionFailure
from storage import SQLiteStore


def _fake_dispatch_packet(task_id="T001", action="iterate_design"):
    return {
        "schema_version": 2,
        "run_id": "r1",
        "task_id": task_id,
        "task_attempt": 1,
        "claim_token": "0" * 32,
        "workflow_id": "w1",
        "plan_id": "plan1",
        "attempt_id": "T001-A01",
        "trace_context": {
            "project_id": "p1",
            "workflow_id": "w1",
            "run_id": "r1",
            "plan_id": "plan1",
            "task_id": task_id,
            "attempt_id": "T001-A01",
        },
        "task": {
            "task_id": task_id,
            "action": action,
            "phase": "design",
            "depends_on": [],
            "outputs": ["design_task_result.json"],
            "resource_request": {"class": "cpu", "proposal_count": 1, "candidate_limit": 10},
            "candidate_scope": {"candidate_ids": []},
            "parameters": {},
        },
    }


class ExecuteTaskIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cycpep-exec-integration-"))
        self.store = SQLiteStore(self.root / "project.db", project_id="p1")
        self.design_pdb = self.root / "C0001.pdb"
        self.design_pdb.write_text("FAKE PDB", encoding="utf-8")
        self.manifest = self.root / "C0001.json"
        self.manifest.write_text('{"candidate_id":"C0001"}', encoding="utf-8")
        self.task_dir = self.root / "exec" / "r1" / "T001" / "attempt_1"
        self.receipt_path = self.task_dir / "execution_receipt.json"
        self.failure_path = self.task_dir / "execution_failure.json"
        # common claimed payload returned by mock claim()
        self.claimed = {
            "claim_token": "0" * 32,
            "dispatch_packet_path": str(self.root / "dispatch.json"),
            "dispatch_packet_sha256": "abc",
            "run": {
                "run_id": "r1",
                "workflow_id": "w1",
                "plan": {"project_id": "p1", "plan_id": "plan1", "workflow_id": "w1"},
                "tasks": {"T001": {"attempts": 1, "status": "running"}},
                "resources": {},
            },
        }
        (self.root / "dispatch.json").write_text(
            json.dumps(_fake_dispatch_packet()), encoding="utf-8"
        )

    def _config(self):
        from execution.config import ExecutionConfig
        # Build a minimal ExecutionConfig with the fields execute_task touches.
        cfg = ExecutionConfig(
            repo_root=ROOT, execution_root=self.root / "exec",
            core_python=Path(sys.executable), design_python=Path(sys.executable),
            prediction_python=Path(sys.executable),
            prediction_artifacts_root=self.root / "pa",
            prediction_runs_root=self.root / "pr",
            colabdesign_dir=self.root / "cd", colabdesign_params=self.root / "cdp",
            cuda_data_dir=self.root / "cuda",
            boltz_executable=None, boltz_cache=None, boltz_checkpoint=None,
            prodigy_executable=None, pyrosetta_python=None, control_data_path=None,
        )
        return cfg

    def _patches(self, adapter_factory):
        """Return a stack of patches: Orchestrator surface + adapter selection."""
        return [
            patch("execution.worker.claim", return_value=self.claimed),
            patch("execution.worker.complete", return_value={
                "run": {"run_id": "r1", "status": "running",
                        "tasks": {"T001": {"outputs": [{"role": "design_result", "path": str(self.root / "dr.json"), "sha256": "x"}]}}}
            }),
            patch("execution.worker.fail", return_value={"run": {"status": "failed"}}),
            patch("execution.worker._read_packet", return_value=_fake_dispatch_packet()),
            patch("execution.worker.assert_action_executable", return_value={}),
            patch("execution.worker.handler_for", return_value=lambda ctx: None),
            patch("execution.worker.adapter_for", side_effect=adapter_factory),
            patch("execution.worker.get_storage_backend", return_value=self.store),
        ]

    def _test_adapter_success(self):
        """Real-shaped adapter: stages artifacts + returns candidate_updates."""
        cu = CandidateUpdate(
            candidate_id="C0001", sequence="GFEWCK", length=6, source_route="A",
            source_batch="b1", cyclization_type="head-to-tail_amide",
            cyclization_bonds=({"atom_1": "residue_6:C", "atom_2": "residue_1:N", "bond_type": "amide"},),
            design_pdb_path=str(self.design_pdb), design_pdb_hash="a" * 64,
            manifest_path=str(self.manifest), manifest_sha256="b" * 64,
            monomer_plddt=87.3, notes="{}",
        )

        def adapter(context, staging):
            staged = (
                staging.stage_artifact(str(self.design_pdb), artifact_id="C0001-design-pdb", artifact_type="design_pdb"),
                staging.stage_artifact(str(self.manifest), artifact_id="C0001-manifest", artifact_type="manifest"),
            )
            return ExecutionActionResult(
                candidate_updates=(cu,),
                artifacts=staged,
                outputs=(("design_result", self.root / "dr.json"),),
                processes=({"label": "design_job_01", "returncode": 0},),
            )
        return adapter

    def test_success_full_chain_commits_candidate_artifact_evidence(self):
        """Test 1: execute_task -> transaction -> commit -> complete.
        Verifies SQLite candidate + artifact + evidence + receipt transaction_id."""
        from contracts.candidate_update import CandidateUpdate
        adapter = self._test_adapter_success()
        with patch("execution.worker.ExecutionConfig.from_environment", return_value=self._config()):
            stacks = self._patches(lambda *a, **k: adapter)
            for p in stacks:
                p.start()
            try:
                receipt = execute_task(run_path=self.root / "run.json", task_id="T001", worker_id="w1")
            finally:
                for p in stacks:
                    p.stop()
        # receipt carries transaction_id
        self.assertTrue(receipt["transaction_id"])
        self.assertEqual(receipt["status"], "succeeded")
        # SQLite candidate committed
        self.assertIsNotNone(self.store.get("C0001"))
        # SQLite artifact registered (design_pdb + manifest)
        self.assertIsNotNone(self.store.get_artifact("C0001-design-pdb"))
        self.assertIsNotNone(self.store.get_artifact("C0001-manifest"))
        # evidence: execution_started + execution_completed
        events = self.store.trace_task("T001")
        types = [e["event_type"] for e in events]
        self.assertIn("execution_started", types)
        self.assertIn("execution_completed", types)

    def test_failure_rollback_leaves_no_candidate_and_records_failure(self):
        """Test 2: handler raises -> ExecutionFailure -> fail(); no candidate committed."""
        def adapter(context, staging):
            raise RuntimeError("boom")
        with patch("execution.worker.ExecutionConfig.from_environment", return_value=self._config()):
            stacks = self._patches(lambda *a, **k: adapter)
            for p in stacks:
                p.start()
            try:
                with self.assertRaises(Exception):
                    execute_task(run_path=self.root / "run.json", task_id="T001", worker_id="w1")
            finally:
                for p in stacks:
                    p.stop()
        # no candidate committed
        self.assertIsNone(self.store.get("C0001"))
        # failure evidence recorded
        events = self.store.trace_task("T001")
        self.assertTrue(any(e["event_type"] == "execution_failed" for e in events))
        # failure json written with transaction_id
        self.assertTrue(self.failure_path.is_file())
        failure = json.loads(self.failure_path.read_text(encoding="utf-8"))
        self.assertTrue(failure["transaction_id"])
        self.assertEqual(failure["status"], "failed")

    def test_no_duplicate_artifact_registration(self):
        """Test 3: one execution registers each artifact exactly once (not 2x)."""
        from contracts.candidate_update import CandidateUpdate
        adapter = self._test_adapter_success()
        with patch("execution.worker.ExecutionConfig.from_environment", return_value=self._config()):
            stacks = self._patches(lambda *a, **k: adapter)
            for p in stacks:
                p.start()
            try:
                execute_task(run_path=self.root / "run.json", task_id="T001", worker_id="w1")
            finally:
                for p in stacks:
                    p.stop()
        with self.store._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM artifacts WHERE artifact_id IN ('C0001-design-pdb','C0001-manifest')"
            ).fetchone()[0]
        self.assertEqual(count, 2)  # exactly 2, not 4

    def test_receipt_contains_transaction_id(self):
        """Test 4: receipt carries a non-empty transaction_id."""
        from contracts.candidate_update import CandidateUpdate
        adapter = self._test_adapter_success()
        with patch("execution.worker.ExecutionConfig.from_environment", return_value=self._config()):
            stacks = self._patches(lambda *a, **k: adapter)
            for p in stacks:
                p.start()
            try:
                receipt = execute_task(run_path=self.root / "run.json", task_id="T001", worker_id="w1")
            finally:
                for p in stacks:
                    p.stop()
        self.assertTrue(receipt["transaction_id"].startswith("tx-"))


if __name__ == "__main__":
    unittest.main()
