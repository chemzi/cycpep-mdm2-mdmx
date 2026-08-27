# KEAP1 canonical cyclic-peptide benchmark

This benchmark uses the human KEAP1 Kelch domain and the canonical
head-to-tail cyclic peptide structures reported under DOI
`10.1021/jacs.0c09799` (PDB 7K2E, 7K2F, 7K2G, 7K2H, 7K2I, and 7K2M).

`runtime_manifest.json` contains sequences and structural references only.
Affinity labels are intentionally absent so Research, Design, and Prediction
cannot read them before the blind evaluation is complete.

The first full-agent run uses the ligand-depleted 7K2E chain A receptor.  It is
a holo-receptor redocking and workflow-transfer benchmark, not a claim of
prospective binding activity.

To regenerate normalized inputs after downloading the PDB files into
`structures/raw/`:

```bash
python scripts/prepare_keap1_benchmark.py \
  --raw-root benchmarks/keap1/structures/raw \
  --output-root benchmarks/keap1/structures/reference \
  --manifest benchmarks/keap1/runtime_manifest.json
```
