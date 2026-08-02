# Positive/negative-control threshold calibration

Research still runs with its literature and provisional thresholds when no
control set is configured.  To calibrate the same seven-layer battery, provide
a JSON file and set `CYCPEP_CONTROL_DATA` (or
`selection.calibration_controls_path` in the approved project config).

The file is an envelope so it can be tied to the approved project and the
exact scoring protocol:

```json
{
  "schema_version": 1,
  "project_id": "mdm2_mdmx_reference",
  "approved_digest": "<approved project digest>",
  "protocol": {
    "tool": "prediction-stack-v1",
    "model": "<model/version>",
    "seeds": 5
  },
  "controls": [
    {
      "control_id": "positive-001",
      "label": "positive",
      "metrics": {
        "global": {
          "plddt": 0.91,
          "nc_distance_pre": 1.28,
          "nc_distance_post": 1.31,
          "scrmsd": 1.12
        },
        "targets": {
          "MDM2": {
            "ipsae": 0.72,
            "dg": -14.2,
            "sc": 0.71,
            "dsasa": 540,
            "hotspot_cov": 0.82,
            "pose_rmsd": 1.15
          },
          "MDMX": {}
        }
      }
    }
  ]
}
```

Each record must be labelled `positive` or `negative`.  At least 10 valid
negative controls and 3 valid positive controls are required by default for a
metric.  The calibrator chooses the highest-recall cutoff whose observed false
positive rate is at most 5%; these limits can be changed in the approved
project `selection` block:

```json
{
  "calibration_controls_path": "data/controls.json",
  "calibration_max_false_positive_rate": 0.05,
  "calibration_min_positive_recall": 0.50,
  "calibration_min_negative_controls": 10,
  "calibration_min_positive_controls": 3
}
```

Target-scoped metrics receive independent `targets.<target_id>` overrides.
The result records sample IDs, protocol hash, observed FPR/recall and Wilson
95% intervals in `_threshold_calibration.json`; only successfully calibrated
entries receive `evidence_grade=positive_control` and
`calibration_status=calibrated`.  Missing, undersized, or digest-mismatched
datasets leave existing thresholds unchanged and are logged as pending or
invalidated evidence.

For an offline preview without changing state:

```text
python -m scripts.calibrate_thresholds --controls data/controls.json
```
