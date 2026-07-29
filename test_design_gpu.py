"""Opt-in 4090 regression for the fixed-sequence AfCycDesign invariant.

Run only in the deployed model environment:

    CYCPEP_RUN_GPU_TESTS=1 \
    /root/damodel-tmp/envs/cycpep-prediction/bin/python \
    -m unittest -v test_design_gpu.py
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


GPU_TESTS_ENABLED = os.environ.get("CYCPEP_RUN_GPU_TESTS") == "1"


@unittest.skipUnless(
    GPU_TESTS_ENABLED,
    "set CYCPEP_RUN_GPU_TESTS=1 in the deployed GPU environment",
)
class FixedSequenceGPURegressionTests(unittest.TestCase):
    def test_refold_preserves_sequence_and_head_to_tail_geometry(self):
        from agents import design

        gpu_probe = subprocess.run(
            [
                design.CYCPEP_PYTHON,
                "-c",
                (
                    "import jax; "
                    "devices=jax.devices(); "
                    "assert any(d.platform == 'gpu' for d in devices), devices"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            gpu_probe.returncode,
            0,
            f"JAX GPU probe failed: {gpu_probe.stderr[-500:]}",
        )

        sequence = "ACDEFGHI"
        with tempfile.TemporaryDirectory(prefix="design-fixed-seq-gpu-") as root:
            output_pdb = Path(root) / "refold.pdb"
            plddt = design._run_refold(sequence, str(output_pdb))

            self.assertIsNotNone(plddt, "fixed-sequence GPU refold failed")
            self.assertGreaterEqual(plddt, 0.0)
            self.assertLessEqual(plddt, 1.0)
            self.assertTrue(output_pdb.is_file())
            observed = design._verify_fixed_sequence_pdb(output_pdb, sequence)
            self.assertEqual(len(observed), 1)
            self.assertEqual(list(observed.values()), [sequence])
            closure = design._ring_closure_check(
                output_pdb, "head-to-tail_amide", sequence=sequence
            )
            self.assertEqual(closure["atom_1"], "last:C")
            self.assertEqual(closure["atom_2"], "first:N")
            self.assertTrue(
                closure["pass"],
                f"real GPU refold is not head-to-tail compatible: {closure}",
            )


if __name__ == "__main__":
    unittest.main()
