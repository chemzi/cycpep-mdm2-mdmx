"""Public-contract tests for the recoverable initial Design invocation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import mock_open, patch

from agents.design import (
    Design,
    DesignContext,
    InitialDesignContractError,
    InitialDesignCorrelation,
    design_initial_invocation_id,
)
from agents.design import candidates as candidate_module
from agents.design.runtime import (
    ScientificToolExecutionError,
    _run_ligandmpnn,
    _run_refold,
    _run_refold_subprocess,
    _run_rfdiff,
)
from contracts.candidate_update import CandidateUpdate
from agents.design.service import _load_existing_sequences
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

    def _route_result(self, *, candidate_updates=None, **_kwargs):
        candidate = {
            "candidate_id": "C0001",
            "sequence": "ACDEFGHI",
            "source_route": "route_A_target_a",
            "status": "designed",
        }
        if candidate_updates is None:
            raise AssertionError("Initial Design must provide a CandidateUpdate stage")
        candidate_updates.append(CandidateUpdate(candidate))
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

        def route(**kwargs):
            starts = self.store.query(
                agent="design", event_type="design_initial_invocation_started"
            )
            observed.append(starts)
            return self._route_result(**kwargs)

        with patch.object(self.design, "design_rfpeptides_initial", side_effect=route) as route_call:
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
        with patch.object(self.design, "design_rfpeptides_initial") as route_call:
            with self.assertRaises(OSError):
                self.design.run_initial(self.correlation, store=failing_store)
        route_call.assert_not_called()
        self.assertEqual(
            self.store.query(agent="design", event_type="design_initial_invocation_started"),
            [],
        )

    def test_durable_start_without_completion_fails_closed_and_never_retries(self):
        with patch.object(
            self.design, "design_rfpeptides_initial", side_effect=RuntimeError("GPU stopped")
        ) as route_call:
            with self.assertRaisesRegex(RuntimeError, "GPU stopped"):
                self.design.run_initial(self.correlation, store=self.store)
            self.assertEqual(route_call.call_count, 1)

        validation = self.design.validate_initial_invocation(
            self.correlation, store=self.store
        )
        self.assertEqual(validation.status, "started_without_completion")
        self.assertEqual(validation.blocker_code, "design_recovery_ambiguous")

        with patch.object(self.design, "design_rfpeptides_initial") as retry:
            with self.assertRaises(InitialDesignContractError) as raised:
                self.design.run_initial(self.correlation, store=self.store)
        retry.assert_not_called()
        self.assertEqual(raised.exception.code, "design_recovery_ambiguous")

    def test_normal_zero_result_has_durable_distinct_blocker_and_never_retries(self):
        with patch.object(
            self.design, "design_rfpeptides_initial", return_value=[]
        ) as route_call:
            with self.assertRaises(InitialDesignContractError) as raised:
                self.design.run_initial(self.correlation, store=self.store)
        self.assertEqual(raised.exception.code, "initial_design_no_valid_candidates")
        route_call.assert_called_once()
        self.assertEqual(
            self.store.query(agent="design", event_type="design_initial_completion"),
            [],
        )
        validation = self.design.validate_initial_invocation(
            self.correlation, store=self.store
        )
        self.assertEqual(validation.status, "blocked")
        self.assertEqual(validation.blocker_code, "initial_design_no_valid_candidates")
        with patch.object(self.design, "design_rfpeptides_initial") as retry:
            with self.assertRaises(InitialDesignContractError) as repeated:
                self.design.run_initial(self.correlation, store=self.store)
        retry.assert_not_called()
        self.assertEqual(repeated.exception.code, "initial_design_no_valid_candidates")

    def test_valid_generation_eliminated_by_scientific_filter_is_normal_zero_result(self):
        generation = ([(Path("bb.pdb"), "B", ["ACDEFGHI"])], 1)
        with patch(
            "experience.apply_experience_preference",
            side_effect=lambda design_config, **_kwargs: (design_config, None),
        ), patch(
            "agents.design.route_a._route_a_generate_backbones",
            return_value=generation,
        ), patch(
            "agents.design.route_a._load_existing_sequences", return_value=set()
        ), patch(
            "agents.design.route_a._cheap_filter_sequences", return_value=[]
        ), patch("agents.design.route_a.EvidenceLogger.design_batch"):
            with self.assertRaises(InitialDesignContractError) as raised:
                self.design.run_initial(self.correlation, store=self.store)
        self.assertEqual(raised.exception.code, "initial_design_no_valid_candidates")
        self.assertEqual(self.store.list(), [])

    def test_classified_tool_failure_is_never_reported_as_zero_result(self):
        failure = ScientificToolExecutionError("rfdiffusion", "exit=1")
        with patch.object(
            self.design, "design_rfpeptides_initial", side_effect=failure
        ):
            with self.assertRaises(InitialDesignContractError) as raised:
                self.design.run_initial(self.correlation, store=self.store)
        self.assertEqual(raised.exception.code, "initial_design_scientific_tool_failed")
        validation = self.design.validate_initial_invocation(
            self.correlation, store=self.store
        )
        self.assertEqual(validation.status, "blocked")
        self.assertEqual(
            validation.blocker_code, "initial_design_scientific_tool_failed"
        )
        self.assertNotEqual(
            validation.blocker_code, "initial_design_no_valid_candidates"
        )

    def test_later_tool_failure_discards_staged_candidate_effects_and_never_retries(self):
        candidate_a = {
            "candidate_id": "C0001",
            "sequence": "ACDEFGHI",
            "source_route": "route_A_target_a",
            "status": "designed",
        }
        generation = ([
            (Path("bb.pdb"), "B", ["ACDEFGHI", "LMNPQRST"]),
        ], 1)

        def manifest(candidate_id, sequence, *_args, **_kwargs):
            return {"candidate_id": candidate_id, "sequence": sequence}

        refold_calls = 0

        def refold(sequence, output_pdb, **_kwargs):
            nonlocal refold_calls
            refold_calls += 1
            if refold_calls == 2:
                raise ScientificToolExecutionError(
                    "afcycdesign_refold", "candidate B"
                )
            Path(output_pdb).parent.mkdir(parents=True, exist_ok=True)
            Path(output_pdb).write_text("MODEL\n", encoding="utf-8")
            return 0.9

        with patch(
            "experience.apply_experience_preference",
            side_effect=lambda design_config, **_kwargs: (design_config, None),
        ), patch(
            "agents.design.route_a._route_a_generate_backbones",
            return_value=generation,
        ), patch(
            "agents.design.route_a._load_existing_sequences", return_value=set()
        ), patch(
            "agents.design.route_a._cheap_filter_sequences",
            return_value=[("ACDEFGHI", 0.9), ("LMNPQRST", 0.8)],
        ), patch(
            "agents.design.route_a._next_candidate_id",
            side_effect=["C0001", "C0002"],
        ), patch(
            "agents.design.candidates._run_refold",
            side_effect=refold,
        ), patch(
            "agents.design.candidates._ring_closure_check",
            return_value={"pass": True},
        ), patch(
            "agents.design.candidates._write_manifest", side_effect=manifest
        ), patch(
            "agents.design.candidates._candidate_from_manifest",
            return_value=candidate_a,
        ), patch(
            "agents.design.candidates._publish_candidate",
            wraps=candidate_module._publish_candidate,
        ) as publisher, patch(
            "agents.design.candidates.CandidateIndex.add"
        ) as legacy_publish:
            with self.assertRaises(InitialDesignContractError) as raised:
                self.design.run_initial(self.correlation, store=self.store)
        self.assertEqual(raised.exception.code, "initial_design_scientific_tool_failed")
        publisher.assert_called_once()
        legacy_publish.assert_not_called()
        self.assertIsNone(self.store.get("C0001"))
        self.assertEqual(
            self.store.query(agent="design", event_type="candidate_registered"), []
        )
        with patch(
            "agents.design.service.CandidateIndex.load",
            return_value=self.store.list(),
        ):
            self.assertNotIn("ACDEFGHI", _load_existing_sequences())

        with patch.object(self.design, "design_rfpeptides_initial") as retry:
            with self.assertRaises(InitialDesignContractError) as repeated:
                self.design.run_initial(self.correlation, store=self.store)
        retry.assert_not_called()
        self.assertEqual(
            repeated.exception.code, "initial_design_scientific_tool_failed"
        )

    def test_success_atomically_publishes_candidate_registration_and_completion(self):
        with patch.object(
            self.design, "design_rfpeptides_initial", side_effect=self._route_result
        ):
            result = self.design.run_initial(self.correlation, store=self.store)

        self.assertEqual(result.candidate_ids, ("C0001",))
        self.assertIsNotNone(self.store.get("C0001"))
        registrations = self.store.query(
            agent="design", event_type="candidate_registered"
        )
        completions = self.store.query(
            agent="design", event_type="design_initial_completion"
        )
        self.assertEqual(len(registrations), 1)
        self.assertEqual(len(completions), 1)
        self.assertTrue(completions[0].get("transaction_id"))
        self.assertEqual(
            registrations[0].get("transaction_id"),
            completions[0].get("transaction_id"),
        )

    def test_failure_receipt_persistence_failure_remains_recovery_ambiguous(self):
        failing_store = _FailingAppendStore(
            self.store, fail_event_type="design_initial_failure"
        )
        with patch.object(
            self.design, "design_rfpeptides_initial", return_value=[]
        ):
            with self.assertRaises(OSError):
                self.design.run_initial(self.correlation, store=failing_store)
        validation = self.design.validate_initial_invocation(
            self.correlation, store=self.store
        )
        self.assertEqual(validation.status, "started_without_completion")
        self.assertEqual(validation.blocker_code, "design_recovery_ambiguous")

    def test_durable_completion_survives_caller_bookkeeping_crash_without_gpu_rerun(self):
        with patch.object(
            self.design, "design_rfpeptides_initial", side_effect=self._route_result
        ) as route_call:
            first = self.design.run_initial(self.correlation, store=self.store)
            self.assertEqual(route_call.call_count, 1)

        # Simulate the caller crashing before it mirrors ``first`` to diagnostics.
        del first

        validation = self.design.validate_initial_invocation(
            self.correlation, store=self.store
        )
        self.assertEqual(validation.status, "completed")
        with patch.object(self.design, "design_rfpeptides_initial") as rerun:
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

    def test_empty_legacy_completion_and_conflicting_terminal_receipts_fail_closed(self):
        jobs = self.design.materialize_initial_jobs()
        start_id = self.store.append({
            "agent": "design",
            "event_type": "design_initial_invocation_started",
            **self.correlation.to_payload(),
            "jobs": list(jobs),
        })
        self.store.append({
            "agent": "design",
            "event_type": "design_initial_completion",
            **self.correlation.to_payload(),
            "jobs": list(jobs),
            "candidate_ids": [],
            "artifact_ids": [],
            "evidence_ids": [start_id],
        })
        validation = self.design.validate_initial_invocation(
            self.correlation, store=self.store
        )
        self.assertEqual(validation.status, "conflict")
        self.assertEqual(validation.blocker_code, "design_recovery_ambiguous")

        self.store.append({
            "agent": "design",
            "event_type": "design_initial_failure",
            **self.correlation.to_payload(),
            "jobs": list(jobs),
            "code": "initial_design_no_valid_candidates",
            "message": "normal empty result",
            "component": "design",
            "retryable": False,
        })
        conflict = self.design.validate_initial_invocation(
            self.correlation, store=self.store
        )
        self.assertEqual(conflict.status, "conflict")
        self.assertEqual(conflict.blocker_code, "design_recovery_ambiguous")

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
        with patch.object(design, "design_rfpeptides_initial") as route_call:
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

    def test_launcher_route_adapter_alone_enables_strict_tool_semantics(self):
        with patch(
            "agents.design.route_a._design_rfpeptides", return_value=[]
        ) as route_core:
            self.design.design_rfpeptides_initial()
            strict_call = route_core.call_args
            self.design.design_rfpeptides()
            legacy_call = route_core.call_args
        self.assertTrue(strict_call.kwargs["strict_tools"])
        self.assertEqual(strict_call.kwargs["candidate_updates"], [])
        self.assertNotIn("strict_tools", legacy_call.kwargs)

    def test_scientific_tool_fallbacks_are_legacy_only(self):
        failed = SimpleNamespace(returncode=1, stderr="tool failed", stdout="")
        with patch("agents.design.runtime.subprocess.run", return_value=failed), patch(
            "agents.design.runtime.EvidenceLogger.error"
        ), patch("agents.design.runtime._cleanup_partial_rfdiff_output"):
            legacy = _run_rfdiff(
                "target.pdb", 8, 1, str(Path(self.temp.name) / "bb"), "contig"
            )
            with self.assertRaises(ScientificToolExecutionError):
                _run_rfdiff(
                    "target.pdb",
                    8,
                    1,
                    str(Path(self.temp.name) / "bb"),
                    "contig",
                    strict_tools=True,
                )
        self.assertFalse(legacy)

        with patch("agents.design.runtime.subprocess.run", return_value=failed), patch(
            "agents.design.runtime.EvidenceLogger.error"
        ), patch(
            "agents.design.runtime._pdb_chain_residue_layout",
            return_value={"B": [1]},
        ), patch(
            "agents.design.runtime._pdb_chain_sequences",
            return_value={"B": "A"},
        ), patch("agents.design.runtime.shutil.rmtree"), patch(
            "agents.design.runtime.os.makedirs"
        ):
            legacy = _run_ligandmpnn("bb.pdb", self.temp.name, binder_chain="B")
            with self.assertRaises(ScientificToolExecutionError):
                _run_ligandmpnn(
                    "bb.pdb", self.temp.name, binder_chain="B", strict_tools=True
                )
        self.assertEqual(legacy, [])

        with patch("agents.design.runtime.subprocess.run", return_value=failed), patch(
            "agents.design.runtime.EvidenceLogger.error"
        ):
            legacy = _run_refold_subprocess(
                "refold.py", "refold.pdb", "refold.plddt", "ACDEFGHI"
            )
            with self.assertRaises(ScientificToolExecutionError):
                _run_refold_subprocess(
                    "refold.py",
                    "refold.pdb",
                    "refold.plddt",
                    "ACDEFGHI",
                    strict_tools=True,
                )
        self.assertIsNone(legacy)

    def test_strict_rfdiff_exit_zero_requires_expected_backbone_output(self):
        succeeded = SimpleNamespace(returncode=0, stderr="", stdout="")
        prefix = str(Path(self.temp.name) / "missing-backbones" / "bb")
        Path(prefix).parent.mkdir(parents=True)
        with patch("agents.design.runtime.subprocess.run", return_value=succeeded):
            self.assertTrue(_run_rfdiff("target.pdb", 8, 2, prefix, "contig"))
            with self.assertRaises(ScientificToolExecutionError):
                _run_rfdiff(
                    "target.pdb", 8, 2, prefix, "contig", strict_tools=True
                )
            Path(f"{prefix}_0.pdb").write_text("ATOM\n", encoding="utf-8")
            with self.assertRaises(ScientificToolExecutionError):
                _run_rfdiff(
                    "target.pdb", 8, 2, prefix, "contig", strict_tools=True
                )

    def test_strict_ligandmpnn_rejects_unavailable_model_and_exit_zero_no_output(self):
        with patch(
            "agents.design.runtime.config.LIGANDMPNN_MODEL_TYPE", "unsupported"
        ):
            self.assertEqual(
                _run_ligandmpnn("bb.pdb", self.temp.name, binder_chain="B"), []
            )
            with self.assertRaises(ScientificToolExecutionError):
                _run_ligandmpnn(
                    "bb.pdb", self.temp.name, binder_chain="B", strict_tools=True
                )

        with patch(
            "agents.design.runtime.Path.is_file",
            side_effect=PermissionError("runtime path is inaccessible"),
        ):
            with self.assertRaises(ScientificToolExecutionError):
                _run_ligandmpnn(
                    "bb.pdb", self.temp.name, binder_chain="B", strict_tools=True
                )

        runtime_root = Path(self.temp.name) / "ligandmpnn-runtime"
        runtime_root.mkdir()
        run_py = runtime_root / "run.py"
        checkpoint = runtime_root / "checkpoint.pt"
        run_py.write_text("# test entrypoint\n", encoding="utf-8")
        checkpoint.write_text("test checkpoint\n", encoding="utf-8")
        succeeded = SimpleNamespace(returncode=0, stderr="", stdout="")
        output_dir = Path(self.temp.name) / "empty-mpnn-output"
        with patch(
            "agents.design.runtime.config.LIGANDMPNN_DIR", str(runtime_root)
        ), patch(
            "agents.design.runtime.config.LIGANDMPNN_CHECKPOINT", str(checkpoint)
        ), patch(
            "agents.design.runtime._pdb_chain_residue_layout",
            return_value={"B": [1]},
        ), patch(
            "agents.design.runtime._pdb_chain_sequences",
            return_value={"B": "A"},
        ), patch("agents.design.runtime.subprocess.run", return_value=succeeded):
            self.assertEqual(
                _run_ligandmpnn("bb.pdb", str(output_dir), binder_chain="B"), []
            )
            with self.assertRaises(ScientificToolExecutionError):
                _run_ligandmpnn(
                    "bb.pdb",
                    str(output_dir),
                    binder_chain="B",
                    strict_tools=True,
                )

        def succeed_with_malformed_output(*_args, **_kwargs):
            malformed_dir = output_dir / "seqs"
            malformed_dir.mkdir(parents=True, exist_ok=True)
            (malformed_dir / "bb.fa").write_text(
                ">reference, id=0\nA\n>design, id=1\n?\n",
                encoding="utf-8",
            )
            return succeeded

        with patch(
            "agents.design.runtime.config.LIGANDMPNN_DIR", str(runtime_root)
        ), patch(
            "agents.design.runtime.config.LIGANDMPNN_CHECKPOINT", str(checkpoint)
        ), patch(
            "agents.design.runtime._pdb_chain_residue_layout",
            return_value={"B": [1]},
        ), patch(
            "agents.design.runtime._pdb_chain_sequences",
            return_value={"B": "A"},
        ), patch(
            "agents.design.runtime.subprocess.run",
            side_effect=succeed_with_malformed_output,
        ), patch("agents.design.runtime.EvidenceLogger.error"):
            self.assertEqual(
                _run_ligandmpnn("bb.pdb", str(output_dir), binder_chain="B"), []
            )
            with self.assertRaises(ScientificToolExecutionError):
                _run_ligandmpnn(
                    "bb.pdb",
                    str(output_dir),
                    binder_chain="B",
                    strict_tools=True,
                )

    def test_strict_ligandmpnn_rejects_malformed_backbone_and_missing_binder_chain(self):
        runtime_root = Path(self.temp.name) / "ligandmpnn-validation-runtime"
        runtime_root.mkdir()
        (runtime_root / "run.py").write_text("# test entrypoint\n", encoding="utf-8")
        checkpoint = runtime_root / "checkpoint.pt"
        checkpoint.write_text("test checkpoint\n", encoding="utf-8")
        runtime_patches = (
            patch("agents.design.runtime.config.LIGANDMPNN_DIR", str(runtime_root)),
            patch(
                "agents.design.runtime.config.LIGANDMPNN_CHECKPOINT",
                str(checkpoint),
            ),
        )

        with runtime_patches[0], runtime_patches[1], patch(
            "agents.design.runtime._pdb_chain_residue_layout",
            side_effect=ValueError("malformed PDB"),
        ), patch("agents.design.runtime.EvidenceLogger.error"):
            self.assertEqual(
                _run_ligandmpnn("bb.pdb", self.temp.name, binder_chain="B"), []
            )
            with self.assertRaises(ScientificToolExecutionError):
                _run_ligandmpnn(
                    "bb.pdb", self.temp.name, binder_chain="B", strict_tools=True
                )

        with patch(
            "agents.design.runtime.config.LIGANDMPNN_DIR", str(runtime_root)
        ), patch(
            "agents.design.runtime.config.LIGANDMPNN_CHECKPOINT", str(checkpoint)
        ), patch(
            "agents.design.runtime._pdb_chain_residue_layout",
            return_value={"A": [1]},
        ), patch(
            "agents.design.runtime._pdb_chain_sequences", return_value={"A": "A"}
        ), patch("agents.design.runtime.EvidenceLogger.error"):
            self.assertEqual(
                _run_ligandmpnn("bb.pdb", self.temp.name, binder_chain="B"), []
            )
            with self.assertRaises(ScientificToolExecutionError):
                _run_ligandmpnn(
                    "bb.pdb", self.temp.name, binder_chain="B", strict_tools=True
                )

    def test_strict_refold_rejects_malformed_required_output(self):
        succeeded = SimpleNamespace(returncode=0, stderr="", stdout="")
        with patch("agents.design.runtime.subprocess.run", return_value=succeeded), patch(
            "agents.design.runtime.os.path.isfile", return_value=True
        ), patch(
            "agents.design.runtime._verify_fixed_sequence_pdb",
            side_effect=ValueError("malformed refold PDB"),
        ), patch("builtins.open", mock_open(read_data="0.91")), patch(
            "agents.design.runtime.EvidenceLogger.error"
        ):
            self.assertIsNone(
                _run_refold_subprocess(
                    "refold.py", "refold.pdb", "refold.plddt", "ACDEFGHI"
                )
            )
            with self.assertRaises(ScientificToolExecutionError):
                _run_refold_subprocess(
                    "refold.py",
                    "refold.pdb",
                    "refold.plddt",
                    "ACDEFGHI",
                    strict_tools=True,
                )

    def test_strict_refold_classifies_preparation_failure(self):
        failure = OSError("cannot prepare refold runtime")
        with patch(
            "agents.design.runtime.config.get_verified_runtime_signature",
            return_value=None,
        ), patch(
            "agents.design.runtime._verify_colabdesign_runtime",
            side_effect=failure,
        ):
            with self.assertRaises(OSError):
                _run_refold("ACDEFGHI", "refold.pdb")
            with self.assertRaises(ScientificToolExecutionError):
                _run_refold(
                    "ACDEFGHI", "refold.pdb", strict_tools=True
                )

    def test_strict_refold_rejects_failed_runtime_verification(self):
        failed_smoke = SimpleNamespace(returncode=1, stderr="smoke failed", stdout="")
        with patch(
            "agents.design.runtime.config.get_verified_runtime_signature",
            return_value=None,
        ), patch(
            "agents.design.runtime.subprocess.run", return_value=failed_smoke
        ), patch(
            "agents.design.runtime._build_refold_script"
        ) as build_script, patch("agents.design.runtime.EvidenceLogger.error"):
            with self.assertRaises(ScientificToolExecutionError):
                _run_refold(
                    "ACDEFGHI", "refold.pdb", strict_tools=True
                )
        build_script.assert_not_called()


if __name__ == "__main__":
    unittest.main()
