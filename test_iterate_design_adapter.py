"""Task 8 migration test: iterate_design handler adapter.

Verifies the PR36 migration adapter that wraps iterate_design(HandlerContext)
into the (TransactionContext, StagingArea) -> ExecutionActionResult signature
expected by ExecutionWorker.run.

The adapter must:
  1. stage design_pdb and manifest files via StagingArea (artifacts)
  2. double-fill ExecutionActionResult (candidate_updates + outputs + processes)
  3. NOT re-read candidate_updates.json or hand-assemble dicts; reuse the
     CandidateUpdate records produced by iterate_design (which read them via
     CandidateUpdateBatch.from_dict)

Full commit-before/after/failure acceptance (CandidateIndex unchanged before
commit, populated after, rolled back on failure) is covered by the integration
test in task 10 once execute_task is migrated.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import execution.handlers as handlers
from contracts.candidate_update import CandidateUpdate
from contracts.transaction import TransactionContext
from execution.handlers import HandlerContext, make_iterate_design_adapter
from execution.results import ExecutionActionResult
from execution.staging import StagingArea


def _fake_candidate_update(candidate_id: str, design_pdb: Path, manifest: Path) -> CandidateUpdate:
    from hashlib import sha256
    return CandidateUpdate(
        candidate_id=candidate_id,
        sequence="GFEWCK",
        length=6,
        source_route="A",
        source_batch="batch-1",
        cyclization_type="head-to-tail_amide",
        cyclization_bonds=(
            {"atom_1": "residue_6:C", "atom_2": "residue_1:N", "bond_type": "amide"},
        ),
        design_pdb_path=str(design_pdb),
        design_pdb_hash=sha256(design_pdb.read_bytes()).hexdigest(),
        manifest_path=str(manifest),
        manifest_sha256=sha256(manifest.read_bytes()).hexdigest(),
        monomer_plddt=87.3,
        notes='{"quality_score": 0.9}',
    )


class IterateDesignAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cycpep-adapter-test-"))
        self.staging = StagingArea(self.root / "staging", "tx-test").create()
        self.design_pdb = self.root / "C0001.pdb"
        self.design_pdb.write_text("FAKE PDB", encoding="utf-8")
        self.manifest = self.root / "C0001.json"
        self.manifest.write_text('{"candidate_id":"C0001"}', encoding="utf-8")
        self.cu = _fake_candidate_update("C0001", self.design_pdb, self.manifest)
        self.context = TransactionContext.create(
            workflow_id="w1", run_id="r1", task_id="T001", attempt_id="T001-A01",
        )

    def test_adapter_stages_artifacts_and_double_fills_result(self) -> None:
        """Adapter stages design_pdb + manifest and double-fills result."""
        fake_result = ExecutionActionResult(
            candidate_updates=(self.cu,),
            outputs=(("design_result", self.root / "design_task_result.json"),),
            processes=({"label": "design_job_01", "returncode": 0},),
        )
        adapter = make_iterate_design_adapter(
            packet={"task": {"action": "iterate_design"}},
            config=None,
            task_dir=self.root,
        )
        with patch.object(handlers, "iterate_design", return_value=fake_result) as mock_h:
            result = adapter(self.context, self.staging)
            mock_h.assert_called_once()
            self.assertIsInstance(mock_h.call_args[0][0], HandlerContext)

        # double-fill: candidate_updates + outputs + processes preserved
        self.assertEqual(result.candidate_updates, (self.cu,))
        self.assertEqual(len(result.outputs), 1)
        self.assertEqual(result.outputs[0][0], "design_result")
        self.assertEqual(len(result.processes), 1)

        # artifacts staged: design_pdb + manifest
        self.assertEqual(len(result.artifacts), 2)
        artifact_ids = {a.artifact_id for a in result.artifacts}
        self.assertEqual(artifact_ids, {"C0001-design-pdb", "C0001-manifest"})
        for staged in result.artifacts:
            self.assertTrue(Path(staged.staged_path).is_file())
            self.assertTrue(staged.sha256)

    def test_adapter_does_not_hand_assemble_candidate_dicts(self) -> None:
        """Adapter must pass through CandidateUpdate objects, not raw dicts."""
        fake_result = ExecutionActionResult(candidate_updates=(self.cu,))
        adapter = make_iterate_design_adapter(
            packet={"task": {"action": "iterate_design"}},
            config=None,
            task_dir=self.root,
        )
        with patch.object(handlers, "iterate_design", return_value=fake_result):
            result = adapter(self.context, self.staging)
        for item in result.candidate_updates:
            self.assertIsInstance(item, CandidateUpdate)


if __name__ == "__main__":
    unittest.main()
