"""7B migration test: design emits CandidateUpdate instead of writing CandidateIndex.

Verifies the PR36 migration contract: when --candidate-updates-path is set,
``_emit_candidate_update`` collects CandidateUpdate records WITHOUT touching
CandidateIndex (the transaction boundary is not bypassed). Also verifies the
legacy fallback (no path -> CandidateIndex.add) for backward compatibility.

This is the 7B acceptance test required before the handler adapter (task 8)
can trust that design subprocess no longer writes the formal candidate store.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agents.design as design
from contracts.candidate_update import CandidateUpdate, CandidateUpdateBatch


def _fake_candidate(candidate_id: str = "C0001") -> dict:
    """Build a candidate dict shaped like _candidate_from_manifest output."""
    return {
        "candidate_id": candidate_id,
        "sequence": "GFEWCK",
        "length": 6,
        "source_route": "A",
        "source_batch": "batch-1",
        "cyclization_type": "head-to-tail_amide",
        "cyclization_bonds": [
            {"atom_1": "residue_6:C", "atom_2": "residue_1:N", "bond_type": "amide"}
        ],
        "design_pdb_path": "/tmp/design/C0001.pdb",
        "design_pdb_hash": "a" * 64,
        "manifest_path": __file__,  # real file so file_hash works
        "monomer_plddt": 87.3,
        "notes": '{"quality_score": 0.9}',
    }


class DesignEmitCandidateUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        # Reset module globals between tests for isolation.
        design._CANDIDATE_UPDATES_PATH = None
        design._PENDING_CANDIDATE_UPDATES = []

    def test_emit_mode_does_not_write_candidateindex(self) -> None:
        """7B core: emit mode must NOT call CandidateIndex.add (no bypass)."""
        design._CANDIDATE_UPDATES_PATH = "/tmp/does-not-matter.json"
        with patch.object(design.CandidateIndex, "add") as mock_add:
            design._emit_candidate_update(_fake_candidate())
            mock_add.assert_not_called()
        self.assertEqual(len(design._PENDING_CANDIDATE_UPDATES), 1)
        update = design._PENDING_CANDIDATE_UPDATES[0]
        self.assertIsInstance(update, CandidateUpdate)
        self.assertEqual(update.candidate_id, "C0001")
        self.assertEqual(update.sequence, "GFEWCK")
        self.assertEqual(update.length, 6)
        self.assertEqual(update.source_route, "A")
        self.assertEqual(update.cyclization_type, "head-to-tail_amide")
        self.assertEqual(update.manifest_sha256, design.file_hash(__file__))

    def test_legacy_fallback_writes_candidateindex_when_no_path(self) -> None:
        """Backward compat: no --candidate-updates-path -> CandidateIndex.add."""
        candidate = _fake_candidate()
        with patch.object(design.CandidateIndex, "add") as mock_add:
            design._emit_candidate_update(candidate)
            mock_add.assert_called_once_with(candidate)
        self.assertEqual(design._PENDING_CANDIDATE_UPDATES, [])

    def test_route_bodies_have_no_direct_candidateindex_add(self) -> None:
        """All 3 route add sites route through _emit_candidate_update;
        CandidateIndex.add(candidate) appears only in the fallback."""
        source = (ROOT / "agents" / "design.py").read_text(encoding="utf-8")
        count = source.count("CandidateIndex.add(candidate)")
        self.assertEqual(
            count,
            1,
            "CandidateIndex.add(candidate) must appear only in "
            "_emit_candidate_update fallback, not in route bodies",
        )

    def test_pending_updates_round_trip_through_batch(self) -> None:
        """Staged updates serialize to CandidateUpdateBatch JSON and back."""
        design._CANDIDATE_UPDATES_PATH = "/tmp/x.json"
        design._emit_candidate_update(_fake_candidate("C0001"))
        design._emit_candidate_update(_fake_candidate("C0002"))
        self.assertEqual(len(design._PENDING_CANDIDATE_UPDATES), 2)
        batch = CandidateUpdateBatch(
            schema_version=1,
            emitter="design",
            source_route="A",
            generated_at="2026-08-05T18:46:00+00:00",
            candidate_updates=tuple(design._PENDING_CANDIDATE_UPDATES),
        )
        payload = json.loads(json.dumps(batch.to_dict()))
        batch2 = CandidateUpdateBatch.from_dict(payload)
        self.assertEqual(len(batch2.candidate_updates), 2)
        self.assertEqual(batch2.candidate_updates[0].candidate_id, "C0001")
        self.assertEqual(batch2.candidate_updates[1].candidate_id, "C0002")


if __name__ == "__main__":
    unittest.main()
