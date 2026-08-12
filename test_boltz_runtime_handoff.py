import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from prediction_pipeline.adapters import CommandResult
from prediction_pipeline.boltz_worker import (
    _prepare_boltz_environment,
    run_boltz_prediction,
)
from prediction_pipeline.protocol import PREDICTION_PROTOCOL
from scripts.enrich_prediction_evidence import _enrich_target


VALIDATED_RUNTIME = {
    "version": "2.2.1",
    "checkpoint_sha256": "validated-checkpoint-observation",
}


class _ObservedStructure:
    def __init__(self, binder_sequence: str):
        self._binder_sequence = binder_sequence

    def sequence(self, chain: str) -> str:
        return self._binder_sequence if chain == "B" else ""


class BoltzRuntimeObservationHandoffTests(unittest.TestCase):
    def _runtime_paths(self, root: Path) -> tuple[Path, Path, Path]:
        executable = root / "bin" / "boltz"
        executable.parent.mkdir()
        executable.write_text("", encoding="utf-8")
        checkpoint = root / "models" / "boltz.ckpt"
        checkpoint.parent.mkdir()
        checkpoint.write_text("checkpoint", encoding="utf-8")
        return executable, checkpoint, root / "cache"

    def test_preparation_preserves_validated_runtime_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable, checkpoint, cache = self._runtime_paths(root)

            with patch(
                "prediction_pipeline.boltz_worker.validate_boltz_runtime",
                return_value=VALIDATED_RUNTIME,
            ):
                environment = _prepare_boltz_environment(
                    executable,
                    cache,
                    checkpoint,
                    root / "output",
                    "ACD",
                    "EFG",
                    "A",
                    "B",
                    60,
                )

            self.assertEqual(environment["version"], "2.2.1")
            self.assertEqual(
                environment["checkpoint_sha"], "validated-checkpoint-observation"
            )

    def test_public_prediction_publishes_validated_runtime_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable, checkpoint, cache = self._runtime_paths(root)
            output = root / "output"

            def valid_outputs(destination, *_args):
                pdb = destination / "prediction.pdb"
                pae = destination / "pae.npz"
                pdb.write_text("MODEL\nEND\n", encoding="utf-8")
                pae.write_bytes(b"pae")
                return {
                    "pdb_path": pdb,
                    "pae_path": pae,
                    "structure": _ObservedStructure("EFG"),
                    "closure_distance": 1.4,
                    "iptm": 0.81,
                    "confidence": {"iptm": 0.81},
                }

            process = CommandResult((), 0, "ok", "", 1.0)
            with (
                patch(
                    "prediction_pipeline.boltz_worker.validate_boltz_runtime",
                    return_value=VALIDATED_RUNTIME,
                ),
                patch(
                    "prediction_pipeline.boltz_worker.run_command",
                    return_value=process,
                ) as run_command,
                patch(
                    "prediction_pipeline.boltz_worker._validate_boltz_outputs",
                    side_effect=valid_outputs,
                ),
            ):
                result = run_boltz_prediction(
                    boltz_executable=executable,
                    cache_dir=cache,
                    checkpoint=checkpoint,
                    target_sequence="ACD",
                    binder_sequence="EFG",
                    output_dir=output,
                    timeout=60,
                )

            metadata = json.loads(Path(result["metadata"]).read_text(encoding="utf-8"))
            invoked_command = run_command.call_args.args[0]
            self.assertEqual(metadata["tool_version"], "2.2.1")
            self.assertEqual(
                metadata["checkpoint_sha256"], "validated-checkpoint-observation"
            )
            self.assertEqual(metadata["command"], invoked_command)
            checkpoint_index = invoked_command.index("--checkpoint") + 1
            samples_index = invoked_command.index("--diffusion_samples") + 1
            seed_index = invoked_command.index("--seed") + 1
            self.assertEqual(invoked_command[checkpoint_index], str(checkpoint.resolve()))
            self.assertEqual(
                invoked_command[samples_index],
                str(PREDICTION_PROTOCOL["parameters"]["boltz"]["diffusion_samples"]),
            )
            self.assertEqual(
                invoked_command[seed_index],
                str(PREDICTION_PROTOCOL["parameters"]["enrichment"]["seed_base"]),
            )

    def test_enrichment_hands_successful_boltz_result_to_prodigy(self):
        boltz_result = {
            "predictor": "Boltz",
            "seed": 17,
            "pdb": "prediction.pdb",
            "metadata": "metadata.json",
            "binder_chain": "B",
        }
        prodigy_result = {"predictor": "Boltz", "output": "prodigy.txt"}
        args = SimpleNamespace(
            boltz="boltz",
            boltz_cache="cache",
            boltz_checkpoint="checkpoint",
            binder_chain="B",
            seed=17,
            timeout=60,
            no_kernels=False,
            prodigy="prodigy",
        )
        bundle = {
            "targets": {
                "MDM2": {
                    "target_chain": "A",
                    "complex_predictions": [],
                    "prodigy_outputs": [],
                }
            }
        }
        candidate = SimpleNamespace(sequence="EFG", cyclization_type="head_to_tail")

        with (
            patch(
                "scripts.enrich_prediction_evidence._target_coordinates",
                return_value=(Path("target.pdb"), "A", "ACD"),
            ),
            patch(
                "scripts.enrich_prediction_evidence.run_boltz_prediction",
                return_value=boltz_result,
            ) as run_boltz,
            patch(
                "scripts.enrich_prediction_evidence._prediction_paths",
                return_value=(
                    Path("prediction.pdb"),
                    Path("metadata.json"),
                    {"model_id": "boltz2_model_0"},
                ),
            ),
            patch(
                "scripts.enrich_prediction_evidence._run_prodigy_for_prediction",
                return_value=prodigy_result,
            ) as run_prodigy,
        ):
            _enrich_target(
                args,
                "MDM2",
                {"MDM2": {}},
                candidate,
                bundle,
                Path("candidate"),
                Path("source.json"),
                add_boltz=True,
                add_rosetta=False,
            )

        target = bundle["targets"]["MDM2"]
        self.assertEqual(target["complex_predictions"], [boltz_result])
        self.assertEqual(target["prodigy_outputs"], [prodigy_result])
        run_boltz.assert_called_once()
        self.assertIs(run_prodigy.call_args.kwargs["prediction"], boltz_result)


if __name__ == "__main__":
    unittest.main()
