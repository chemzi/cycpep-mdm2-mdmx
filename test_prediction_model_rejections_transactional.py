"""Atomic Worker regression for a later Prediction tool failure."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from contracts.trace import TraceContext
from contracts.transaction import TransactionContext, TransactionStatus
from execution.adapters import adapter_for
from execution.config import ExecutionConfig
from execution.contracts import ExecutionContractError
from execution.handlers import evaluate_new_design_candidates
from execution.worker import ExecutionWorker, _validate_action_result
from prediction_pipeline.adapters import load_artifact_bundle
from prediction_pipeline.contracts import SCHEMA_VERSION, file_sha256
from prediction_pipeline.execution_identity import build_prediction_execution_identity
from prediction_pipeline.protocol import protocol_binding
from storage import SQLiteStore

from _prediction_test_utils import SEQUENCE, project_config, write_complex, write_monomer


class PredictionModelRejectionTransactionalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(
            prefix="prediction-rejection-transaction-"
        )
        self.root = Path(self.tmp.name)
        self.identity = build_prediction_execution_identity()
        self.project = project_config(("MDM2",))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _config(self) -> ExecutionConfig:
        tools = self.root / "tools"
        tools.mkdir()
        for name in ("boltz", "checkpoint", "prodigy", "python"):
            (tools / name).write_text("fixture", encoding="utf-8")
        for name in ("cache", "colabdesign", "params", "cuda"):
            (tools / name).mkdir()
        return ExecutionConfig(
            repo_root=Path(__file__).resolve().parent,
            execution_root=self.root / "execution",
            core_python=Path(sys.executable),
            design_python=Path(sys.executable),
            prediction_python=Path(sys.executable),
            prediction_artifacts_root=self.root / "published_prediction_artifacts",
            prediction_runs_root=self.root / "prediction_runs",
            colabdesign_dir=tools / "colabdesign",
            colabdesign_params=tools / "params",
            cuda_data_dir=tools / "cuda",
            boltz_executable=tools / "boltz",
            boltz_cache=tools / "cache",
            boltz_checkpoint=tools / "checkpoint",
            prodigy_executable=tools / "prodigy",
            pyrosetta_python=tools / "python",
            control_data_path=None,
        )

    def _packet(self) -> dict:
        return {
            "run_id": "orchestrator_model_rejection_transaction",
            "task_attempt": 1,
            "task": {
                "task_id": "T001",
                "action": "evaluate_new_design_candidates",
                "phase": "evaluate",
                "parameters": {
                    "reuse_complete_evidence": False,
                    "evidence_mode": "reuse_or_generate_full",
                    "predictor_protocol": protocol_binding(),
                    "execution_identity": self.identity,
                },
                "candidate_scope": {
                    "candidate_ids": ["C0001", "C0002"],
                    "from_task_id": None,
                },
                "resource_request": {
                    "class": "gpu",
                    "proposal_count": 0,
                    "candidate_limit": 2,
                },
                "outputs": ["prediction_handoff.json"],
            },
            "trace_context": {"project_id": "prediction_test"},
        }

    @staticmethod
    def _prediction_inputs(candidate_dir: Path) -> tuple[list[dict], list[dict]]:
        predictions = []
        identities = []
        for index, (predictor, seed, model_id) in enumerate((
            ("ColabDesign", 0, "alphafold2_model_0"),
            ("ColabDesign", 1, "alphafold2_model_1"),
            ("ColabDesign", 2, "alphafold2_model_2"),
            ("Boltz", 101, "boltz2_model_0"),
        )):
            pdb = candidate_dir / f"complex_{index}.pdb"
            metadata = candidate_dir / f"complex_{index}.json"
            write_complex(pdb, binder_shift=(0, 1.5 + index * 0.1, 0))
            metadata.write_text(json.dumps({
                "model_id": model_id,
                "binder_chain": "B",
            }), encoding="utf-8")
            prediction_sha = file_sha256(pdb)
            predictions.append({
                "predictor": predictor,
                "seed": seed,
                "pdb": pdb.name,
                "metadata": metadata.name,
                "binder_chain": "B",
            })
            identities.append({
                "predictor": predictor,
                "model_id": model_id,
                "seed": seed,
                "prediction_pdb_sha256": prediction_sha,
            })
        return predictions, identities

    @staticmethod
    def _prodigy_outputs(candidate_dir: Path, identities: list[dict]) -> list[dict]:
        outputs = []
        for index, identity in enumerate(identities):
            output = candidate_dir / f"prodigy_{index}.txt"
            output.write_text("-8.0\n", encoding="utf-8")
            outputs.append({**identity, "output": output.name})
        return outputs

    @staticmethod
    def _rosetta_output(candidate_dir: Path, identity: dict, index: int) -> dict:
        score = candidate_dir / f"rosetta_{index}.sc"
        metadata = candidate_dir / f"rosetta_{index}.json"
        score.write_text(
            "SCORE: dSASA_int sc_value dG_separated description\n"
            "SCORE: 550.0 0.75 -12.0 model\n",
            encoding="utf-8",
        )
        metadata.write_text(json.dumps({
            "tool": "PyRosetta InterfaceAnalyzerMover",
            "tool_version_output": "PyRosetta-4 2026.29",
            "protocol": "declare_head_to_tail_then_interface_analyzer_ref2015",
            **identity,
            "target_chain": "A",
            "binder_chain": "B",
            "binder_sequence": SEQUENCE,
            "terminal_c_to_n_distance_angstrom": 1.3,
            "declared_bond": {
                "res1": 11, "atom1": "C", "res2": 3, "atom2": "N",
            },
            "scorefunction": "ref2015",
            "metrics": {
                "dsasa": 550.0,
                "sc": 0.75,
                "rosetta_dg_separated": -12.0,
            },
            "xml_sha256": "a" * 64,
        }), encoding="utf-8")
        return {**identity, "output": score.name, "metadata": metadata.name}

    def _write_mixed_bundle(self, candidate_dir: Path) -> Path:
        candidate_dir.mkdir(parents=True)
        monomer = candidate_dir / "monomer.pdb"
        post_relax = candidate_dir / "post_relax.pdb"
        post_metadata = candidate_dir / "post_relax.json"
        write_monomer(monomer)
        write_monomer(post_relax)
        post_metadata.write_text("{}", encoding="utf-8")
        predictions, identities = self._prediction_inputs(candidate_dir)
        rosetta_outputs = [
            self._rosetta_output(candidate_dir, identities[index], index)
            for index in (0, 2, 3)
        ]

        rejected = identities[1]
        bundle = {
            "schema_version": SCHEMA_VERSION,
            "candidate_id": "C0001",
            "sequence": SEQUENCE,
            "protocol": protocol_binding(),
            "global": {
                "monomer_predictions": [{
                    "predictor": "ColabDesign",
                    "seed": 0,
                    "pdb": monomer.name,
                }],
                "post_relax_pdb": post_relax.name,
                "post_relax_metadata": post_metadata.name,
            },
            "targets": {"MDM2": {
                "target_chain": "A",
                "complex_predictions": predictions,
                "prodigy_outputs": self._prodigy_outputs(
                    candidate_dir, identities
                ),
                "rosetta_outputs": rosetta_outputs,
                "rosetta_rejections": [{
                    **rejected,
                    "target_chain": "A",
                    "binder_chain": "B",
                    "binder_sequence": SEQUENCE,
                    "code": "rosetta_cyclic_bond_open",
                    "observed_terminal_c_to_n_distance_angstrom": 2.57,
                    "maximum_terminal_c_to_n_distance_angstrom": 2.0,
                }],
            }},
        }
        path = candidate_dir / "artifacts.json"
        path.write_text(json.dumps(bundle), encoding="utf-8")
        return path

    def _execution(self, config: ExecutionConfig, packet: dict):
        store = SQLiteStore(self.root / "store.db", project_id="prediction_test")
        store.replace_state("prediction_test", {
            "project_id": "prediction_test",
            "phase": "design",
            "thresholds": {},
            "iteration_history": [],
            "project_config": self.project,
        })
        for candidate_id in ("C0001", "C0002"):
            store.upsert({
                "candidate_id": candidate_id,
                "sequence": SEQUENCE,
                "source_route": "test_route",
                "source_batch": "typed",
                "cyclization_type": "head-to-tail_amide",
                "final_status": "pending",
                "notes": "",
            }, duplicate_policy="insert_only")
        transaction = TransactionContext.create(
            workflow_id="workflow-model-rejection",
            run_id=packet["run_id"],
            task_id="T001",
            attempt_id="T001-A01",
            action="evaluate_new_design_candidates",
            metadata={"project_id": "prediction_test"},
        )
        adapter = adapter_for(
            "evaluate_new_design_candidates",
            evaluate_new_design_candidates,
            packet,
            config,
            self.root / "task",
            self.project,
        )
        worker = ExecutionWorker(
            store,
            self.root / "staging",
            config.execution_root / "artifacts",
        )
        return store, transaction, adapter, worker

    def _enter_runtime_patches(self, stack: ExitStack) -> None:
        expected = self.identity
        stack.enter_context(patch(
            "execution.handlers.State.load",
            return_value={"thresholds": {}, "project_config": self.project},
        ))
        stack.enter_context(patch(
            "prediction_pipeline.colabdesign_worker.validate_colabdesign_runtime",
            return_value=expected["colabdesign"]["commit"],
        ))
        stack.enter_context(patch(
            "prediction_pipeline.boltz_worker.validate_boltz_runtime",
            return_value={
                "version": expected["boltz"]["version"],
                "checkpoint_sha256": expected["boltz"]["checkpoint_sha256"],
            },
        ))
        stack.enter_context(patch(
            "prediction_pipeline.rosetta_worker.validate_pyrosetta_runtime",
            return_value=expected["pyrosetta"]["version"],
        ))
        stack.enter_context(patch(
            "prediction_pipeline.adapters.validate_prodigy_runtime",
            return_value=expected["prodigy"]["version"],
        ))

    def _assert_no_formal_effects(
        self,
        store: SQLiteStore,
        transaction: TransactionContext,
        candidate_snapshot: dict[str, dict],
    ) -> None:
        self.assertEqual(transaction.status, TransactionStatus.FAILED)
        self.assertEqual(store.get_transaction_status(transaction.transaction_id), "FAILED")
        for candidate_id in ("C0001", "C0002"):
            self.assertEqual(store.get(candidate_id), candidate_snapshot[candidate_id])
        events = store.query()
        self.assertEqual(
            [event["event_type"] for event in events],
            ["execution_transaction_failed"],
        )
        self.assertNotIn("candidate_id", events[0])
        transaction_record = store.get_transaction(transaction.transaction_id)
        self.assertFalse(transaction_record.get("candidate_effects"))
        self.assertFalse(transaction_record.get("evidence_event_ids"))
        self.assertFalse(transaction_record.get("artifact_ids"))
        self.assertFalse(
            (self.root / "task" / "prediction_transaction_effects.json").exists()
        )

    def test_later_tool_failure_rolls_back_earlier_mixed_candidate(self):
        config = self._config()
        packet = self._packet()
        store, transaction, adapter, worker = self._execution(config, packet)
        candidate_snapshot = {
            candidate_id: store.get(candidate_id)
            for candidate_id in ("C0001", "C0002")
        }
        task_dir = self.root / "task"
        process_labels = []

        def scientific_process(_argv, **kwargs):
            label = kwargs["label"]
            process_labels.append(label)
            if label == "prediction_enrichment[C0001]":
                self._write_mixed_bundle(
                    task_dir / "enriched_prediction_artifacts" / "C0001"
                )
            elif label == "prediction_enrichment[C0002]":
                raise ExecutionContractError(
                    "execution_process_failed",
                    "C0002 PyRosetta import failed",
                )
            return {"label": label, "elapsed_seconds": 0.01}

        with ExitStack() as stack:
            self._enter_runtime_patches(stack)
            stack.enter_context(patch(
                "execution.handlers.run_process", side_effect=scientific_process
            ))
            raised = stack.enter_context(self.assertRaises(ExecutionContractError))
            worker.run(transaction, adapter, validator=_validate_action_result,
                trace_context=TraceContext(
                    project_id="prediction_test",
                    workflow_id="workflow-model-rejection",
                    run_id=packet["run_id"], task_id="T001", attempt_id="T001-A01",
                ))

        self.assertEqual(raised.exception.code, "execution_process_failed")
        self.assertEqual(process_labels, [
            "prediction_af2_prodigy",
            "prediction_enrichment[C0001]",
            "prediction_enrichment[C0002]",
        ])
        loaded = load_artifact_bundle(
            task_dir / "enriched_prediction_artifacts" / "C0001" / "artifacts.json",
            candidate_id="C0001",
            sequence=SEQUENCE,
            required_targets=("MDM2",),
        )
        self.assertEqual(len(loaded.target_artifacts["MDM2"]["rosetta_outputs"]), 3)
        self.assertEqual(len(loaded.target_artifacts["MDM2"]["rosetta_rejections"]), 1)

        self._assert_no_formal_effects(store, transaction, candidate_snapshot)


if __name__ == "__main__":
    unittest.main()
