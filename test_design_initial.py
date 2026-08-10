"""Public-contract tests for the recoverable initial Design invocation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.design import (
    Design,
    DesignContext,
    InitialDesignContractError,
    InitialDesignCorrelation,
    design_initial_invocation_id,
)
from storage import SQLiteStore
from target_bootstrap import config_digest


LAUNCHER_RUN_ID = "launcher_12345678123456781234567812345678"
DESIGN_INVOCATION_ID = "design_initial_12345678123456781234567812345678"


class _FailingAppendStore:
    """Delegate reads to a real Store while failing selected appends."""

    def __init__(self, store, *, fail_event_type):
        self._store = store
        self._fail_event_type = fail_event_type

    def append(self, event):
        if event.get("event_type") == self._fail_event_type:
            raise OSError("injected receipt persistence failure")
        return self._store.append(event)

    def __getattr__(self, name):
        return getattr(self._store, name)


class InitialDesignContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        coordinate = root / "target.pdb"
        coordinate.write_text(
            "ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00  0.00           C\n",
            encoding="utf-8",
        )
        project = {
            "project_id": "initial_design_test",
            "modality": "head_to_tail_cyclic_peptide",
            "targets": [{
                "id": "TARGET_A",
                "structure": {
                    "pdb_id": "1ABC",
                    "chain": "A",
                    "coordinate_path": str(coordinate),
                    "coordinate_sha256": "approved-coordinate-digest",
                },
                "binding_site": {"residues": [1], "status": "user_reviewed"},
                "design": {"lengths": [8]},
            }],
        }
        project["review"] = {
            "status": "approved",
            "approved_digest": config_digest(project),
        }
        self.context = DesignContext(
            project_config=project,
            output_dir=str(root / "design-output"),
        )
        self.design = Design(self.context)
        self.store = SQLiteStore(root / "formal.sqlite3", project_id=project["project_id"])
        self.correlation = InitialDesignCorrelation.from_launcher(
            launcher_run_id=LAUNCHER_RUN_ID,
            project_id=project["project_id"],
            approved_content_binding=project["review"]["approved_digest"],
        )

    def tearDown(self):
        self.temp.cleanup()

    def _route_result(self, **_kwargs):
        candidate = {
            "candidate_id": "C0001",
            "sequence": "ACDEFGHI",
            "source_route": "route_A_target_a",
            "status": "designed",
        }
        self.store.upsert(candidate, duplicate_policy="insert_only")
        return [candidate]

    def test_identity_mapping_is_fixed_reversible_and_rejects_non_uuid_payload(self):
        self.assertEqual(
            design_initial_invocation_id(LAUNCHER_RUN_ID),
            DESIGN_INVOCATION_ID,
        )
        with self.assertRaisesRegex(ValueError, "launcher_run_id"):
            design_initial_invocation_id("launcher_not-a-uuid")

    def test_start_receipt_is_durable_before_route_and_completion_is_bound(self):
        observed = []

        def route(**_kwargs):
            starts = self.store.query(
                agent="design", event_type="design_initial_invocation_started"
            )
            observed.append(starts)
            return self._route_result()

        with patch.object(self.design, "design_rfpeptides", side_effect=route) as route_call:
            result = self.design.run_initial(self.correlation, store=self.store)

        route_call.assert_called_once()
        self.assertEqual(len(observed[0]), 1)
        self.assertEqual(observed[0][0]["design_invocation_id"], DESIGN_INVOCATION_ID)
        self.assertEqual(result.candidate_ids, ("C0001",))
        self.assertEqual(result.artifact_ids, ())
        self.assertEqual(result.status, "completed")

        validation = self.design.validate_initial_invocation(
            self.correlation, store=self.store
        )
        self.assertEqual(validation.status, "completed")
        self.assertEqual(validation.candidate_ids, ("C0001",))
        self.assertEqual(validation.jobs[0]["route"], "A")

    def test_start_persistence_failure_has_zero_route_side_effects(self):
        failing_store = _FailingAppendStore(
            self.store, fail_event_type="design_initial_invocation_started"
        )
        with patch.object(self.design, "design_rfpeptides") as route_call:
            with self.assertRaises(OSError):
                self.design.run_initial(self.correlation, store=failing_store)
        route_call.assert_not_called()
        self.assertEqual(
            self.store.query(agent="design", event_type="design_initial_invocation_started"),
            [],
        )

    def test_durable_start_without_completion_fails_closed_and_never_retries(self):
        with patch.object(
            self.design, "design_rfpeptides", side_effect=RuntimeError("GPU stopped")
        ) as route_call:
            with self.assertRaisesRegex(RuntimeError, "GPU stopped"):
                self.design.run_initial(self.correlation, store=self.store)
            self.assertEqual(route_call.call_count, 1)

        validation = self.design.validate_initial_invocation(
            self.correlation, store=self.store
        )
        self.assertEqual(validation.status, "started_without_completion")
        self.assertEqual(validation.blocker_code, "design_recovery_ambiguous")

        with patch.object(self.design, "design_rfpeptides") as retry:
            with self.assertRaises(InitialDesignContractError) as raised:
                self.design.run_initial(self.correlation, store=self.store)
        retry.assert_not_called()
        self.assertEqual(raised.exception.code, "design_recovery_ambiguous")

    def test_durable_completion_survives_caller_bookkeeping_crash_without_gpu_rerun(self):
        with patch.object(
            self.design, "design_rfpeptides", side_effect=self._route_result
        ) as route_call:
            first = self.design.run_initial(self.correlation, store=self.store)
            self.assertEqual(route_call.call_count, 1)

        # Simulate the caller crashing before it mirrors ``first`` to diagnostics.
        del first

        validation = self.design.validate_initial_invocation(
            self.correlation, store=self.store
        )
        self.assertEqual(validation.status, "completed")
        with patch.object(self.design, "design_rfpeptides") as rerun:
            recovered = self.design.run_initial(self.correlation, store=self.store)
        rerun.assert_not_called()
        self.assertEqual(recovered.status, "completed")
        self.assertEqual(recovered.candidate_ids, ("C0001",))

    def test_multiple_starts_are_a_conflict_not_completion_or_retry_authority(self):
        job = self.design.materialize_initial_jobs()
        event = {
            "agent": "design",
            "event_type": "design_initial_invocation_started",
            **self.correlation.to_payload(),
            "jobs": list(job),
        }
        self.store.append(event)
        self.store.append(event)

        validation = self.design.validate_initial_invocation(
            self.correlation, store=self.store
        )
        self.assertEqual(validation.status, "conflict")
        self.assertEqual(validation.blocker_code, "design_recovery_ambiguous")

    def test_contract_gap_is_reported_before_route_or_start_receipt(self):
        invalid_context = DesignContext(
            project_config={
                "project_id": "invalid_initial_design",
                "review": {"status": "approved", "approved_digest": "binding"},
                "targets": [],
            },
            output_dir=self.context.output_dir,
        )
        design = Design(invalid_context)
        correlation = InitialDesignCorrelation.from_launcher(
            launcher_run_id=LAUNCHER_RUN_ID,
            project_id="invalid_initial_design",
            approved_content_binding="binding",
        )
        invalid_store = SQLiteStore(
            Path(self.temp.name) / "invalid.sqlite3",
            project_id="invalid_initial_design",
        )
        with patch.object(design, "design_rfpeptides") as route_call:
            with self.assertRaises(InitialDesignContractError) as raised:
                design.run_initial(correlation, store=invalid_store)
        route_call.assert_not_called()
        self.assertEqual(raised.exception.code, "initial_design_contract_gap")
        self.assertEqual(
            invalid_store.query(
                agent="design", event_type="design_initial_invocation_started"
            ),
            [],
        )

    def test_legacy_route_public_interface_and_return_value_are_unchanged(self):
        sentinel = [{"candidate_id": "C0042"}]
        with patch(
            "agents.design.route_a.design_rfpeptides", return_value=sentinel
        ) as legacy_route:
            result = self.design.design_rfpeptides(
                target_spec={"target_name": "TARGET_A"},
                design_config={"n": 1, "seed": 7},
            )
        self.assertIs(result, sentinel)
        legacy_route.assert_called_once_with(
            target_spec={"target_name": "TARGET_A"},
            design_config={"n": 1, "seed": 7},
            context=self.context,
        )


if __name__ == "__main__":
    unittest.main()
