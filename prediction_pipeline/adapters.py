"""Tool adapters and the versioned raw-artifact ingestion boundary."""

from __future__ import annotations

import json
import math
import os
import shlex
import statistics
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .contracts import (
    SCHEMA_VERSION,
    ContractError,
    file_sha256,
    object_sha256,
)
from .metrics import parse_prodigy_output, parse_rosetta_interface_output


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_sec: float

    def trace(self, tool_name: str, tool_version: str = "") -> dict:
        return {
            "tool_name": tool_name,
            "tool_version": tool_version,
            "input_params": {"argv": list(self.argv)},
            "exit_code": self.exit_code,
            "duration_sec": self.duration_sec,
            "stdout_snippet": self.stdout[-500:],
            "stderr_snippet": self.stderr[-500:],
        }


def run_command(
    argv: Iterable[str],
    *,
    timeout: int,
    cwd: str | Path | None = None,
    env: dict | None = None,
) -> CommandResult:
    """Run an explicit argv list.  Shell expansion is intentionally disabled."""
    command = tuple(str(value) for value in argv)
    if not command:
        raise ContractError("empty_command", "adapter command cannot be empty")
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ContractError("tool_unavailable", f"tool not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ContractError(
            "tool_timeout", f"command timed out after {timeout}s: {shlex.join(command)}"
        ) from exc
    return CommandResult(
        argv=command,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        duration_sec=time.monotonic() - started,
    )


def _resolve_artifact_path(raw: str, base: Path, label: str) -> Path:
    if not str(raw or "").strip():
        raise ContractError("artifact_path_missing", f"{label} path is missing")
    path = Path(raw).expanduser()
    path = path.resolve() if path.is_absolute() else (base / path).resolve()
    if not path.is_file():
        raise ContractError("artifact_missing", f"{label} not found: {path}")
    return path


def _materialize_file(entry: dict, key: str, base: Path, label: str) -> dict:
    path = _resolve_artifact_path(entry.get(key), base, label)
    actual = file_sha256(path)
    declared = str(entry.get(f"{key}_sha256") or "").strip().lower()
    if declared and declared != actual:
        raise ContractError(
            "artifact_hash_mismatch",
            f"{label} hash mismatch: declared={declared}, actual={actual}",
        )
    return {"path": path, "sha256": actual}


def _validate_prediction_entry(entry: dict, base: Path, label: str) -> dict:
    if not isinstance(entry, dict):
        raise ContractError("artifact_entry_type", f"{label} must be an object")
    allowed = {
        "predictor", "seed", "primary", "pdb", "pdb_sha256", "pae",
        "pae_sha256", "metadata", "metadata_sha256", "binder_chain",
    }
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise ContractError(
            "artifact_unknown_keys", f"{label} contains unknown keys: {unknown}"
        )
    predictor = str(entry.get("predictor") or "").strip()
    if not predictor:
        raise ContractError("predictor_missing", f"{label} requires predictor")
    if isinstance(entry.get("seed"), bool) or not isinstance(entry.get("seed"), int):
        raise ContractError("seed_invalid", f"{label} requires an integer seed")
    result = dict(entry)
    result["predictor"] = predictor
    result["pdb"] = _materialize_file(entry, "pdb", base, f"{label}.pdb")
    if entry.get("pae"):
        result["pae"] = _materialize_file(entry, "pae", base, f"{label}.pae")
    metadata = entry.get("metadata")
    if metadata:
        result["metadata"] = _materialize_file(
            entry, "metadata", base, f"{label}.metadata"
        )
    return result


def _validate_scored_output(
    entry: dict,
    base: Path,
    label: str,
    predictions_by_hash: dict[str, dict],
) -> dict:
    """Validate a per-prediction scalar-tool output and its structure link."""
    if not isinstance(entry, dict):
        raise ContractError("artifact_entry_type", f"{label} must be an object")
    allowed = {
        "predictor", "model_id", "seed", "prediction_pdb_sha256",
        "output", "output_sha256", "metadata", "metadata_sha256",
    }
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise ContractError(
            "artifact_unknown_keys", f"{label} contains unknown keys: {unknown}"
        )
    predictor = str(entry.get("predictor") or "").strip()
    model_id = str(entry.get("model_id") or "").strip()
    if not predictor or not model_id:
        raise ContractError(
            "scored_output_identity_missing",
            f"{label} requires predictor and model_id",
        )
    if isinstance(entry.get("seed"), bool) or not isinstance(entry.get("seed"), int):
        raise ContractError("seed_invalid", f"{label} requires an integer seed")
    prediction_sha = str(entry.get("prediction_pdb_sha256") or "").strip().lower()
    prediction = predictions_by_hash.get(prediction_sha)
    if prediction is None:
        raise ContractError(
            "scored_output_prediction_mismatch",
            f"{label} does not reference a declared complex prediction PDB",
        )
    if predictor != prediction["predictor"] or entry["seed"] != prediction["seed"]:
        raise ContractError(
            "scored_output_prediction_mismatch",
            f"{label} predictor/seed does not match its linked complex prediction",
        )
    metadata = parse_metadata(prediction.get("metadata"))
    if not metadata or str(metadata.get("model_id") or "").strip() != model_id:
        raise ContractError(
            "scored_output_model_mismatch",
            f"{label} model_id does not match linked prediction metadata",
        )
    result = dict(entry)
    result.update({
        "predictor": predictor,
        "model_id": model_id,
        "prediction_pdb_sha256": prediction_sha,
        "output": _materialize_file(entry, "output", base, f"{label}.output"),
    })
    if entry.get("metadata"):
        result["metadata"] = _materialize_file(
            entry, "metadata", base, f"{label}.metadata"
        )
    return result


def _validate_rosetta_metadata(
    entry: dict,
    *,
    label: str,
    prediction: dict,
    target_chain: str,
    sequence: str,
) -> None:
    """Require topology-aware, structure-bound provenance for Rosetta scores."""
    if not entry.get("metadata"):
        raise ContractError(
            "rosetta_metadata_missing", f"{label} requires metadata"
        )
    values = parse_metadata(entry["metadata"])
    expected = {
        "predictor": entry["predictor"],
        "model_id": entry["model_id"],
        "seed": entry["seed"],
        "prediction_pdb_sha256": entry["prediction_pdb_sha256"],
        "target_chain": target_chain,
        "binder_sequence": sequence,
        "protocol": "declare_head_to_tail_then_interface_analyzer_ref2015",
        "scorefunction": "ref2015",
    }
    mismatches = {
        key: (values.get(key), expected_value)
        for key, expected_value in expected.items()
        if values.get(key) != expected_value
    }
    prediction_metadata = parse_metadata(prediction.get("metadata"))
    binder_chain = str(
        prediction.get("binder_chain")
        or prediction_metadata.get("binder_chain")
        or ""
    ).strip()
    if values.get("binder_chain") != binder_chain:
        mismatches["binder_chain"] = (values.get("binder_chain"), binder_chain)
    if mismatches:
        raise ContractError(
            "rosetta_metadata_mismatch", f"{label} mismatches: {mismatches}"
        )
    tool = str(values.get("tool") or "")
    if tool not in {
        "PyRosetta InterfaceAnalyzerMover",
        "RosettaScripts InterfaceAnalyzerMover",
    }:
        raise ContractError(
            "rosetta_tool_invalid", f"{label} has unsupported tool {tool!r}"
        )
    if not str(values.get("tool_version_output") or "").strip():
        raise ContractError(
            "rosetta_version_missing", f"{label} lacks tool version"
        )
    if len(str(values.get("xml_sha256") or "")) != 64:
        raise ContractError(
            "rosetta_protocol_hash_missing", f"{label} lacks protocol XML SHA-256"
        )
    declared = values.get("declared_bond") or {}
    if (
        declared.get("atom1") != "C"
        or declared.get("atom2") != "N"
        or not isinstance(declared.get("res1"), int)
        or not isinstance(declared.get("res2"), int)
    ):
        raise ContractError(
            "rosetta_topology_invalid", f"{label} lacks a residue-bound C--N bond"
        )
    closure = values.get("terminal_c_to_n_distance_angstrom")
    if (
        isinstance(closure, bool)
        or not isinstance(closure, (int, float))
        or not math.isfinite(float(closure))
        or float(closure) > 2.0
    ):
        raise ContractError(
            "rosetta_topology_invalid", f"{label} has invalid C--N distance {closure!r}"
        )
    parsed = parse_rosetta_interface_output(
        entry["output"]["path"].read_text(encoding="utf-8", errors="replace")
    )
    declared_metrics = values.get("metrics") or {}
    for key, value in parsed.items():
        observed = declared_metrics.get(key)
        if (
            isinstance(observed, bool)
            or not isinstance(observed, (int, float))
            or not math.isclose(float(observed), value, rel_tol=1e-9, abs_tol=1e-9)
        ):
            raise ContractError(
                "rosetta_metadata_metric_mismatch",
                f"{label} metadata {key}={observed!r}, scorefile={value}",
            )


@dataclass(frozen=True)
class ArtifactBundle:
    path: Path
    sha256: str
    candidate_id: str
    sequence: str
    global_artifacts: dict
    target_artifacts: dict[str, dict]
    digest: str


def load_artifact_bundle(
    path: str | Path,
    *,
    candidate_id: str,
    sequence: str,
    required_targets: tuple[str, ...],
) -> ArtifactBundle:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ContractError("artifact_bundle_missing", f"artifact bundle not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractError("artifact_bundle_malformed", f"invalid JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ContractError("artifact_bundle_type", "artifacts.json must be an object")
    unknown = sorted(set(raw) - {
        "schema_version", "candidate_id", "sequence", "global", "targets",
        "protocol", "enrichment",
    })
    if unknown:
        raise ContractError(
            "artifact_unknown_keys", f"artifacts.json contains unknown keys: {unknown}"
        )
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(
            "artifact_schema_unsupported",
            f"artifacts.json schema_version must be {SCHEMA_VERSION}",
        )
    if str(raw.get("candidate_id") or "") != candidate_id:
        raise ContractError(
            "artifact_candidate_mismatch",
            f"artifact bundle candidate does not match {candidate_id}",
        )
    if str(raw.get("sequence") or "").upper() != sequence:
        raise ContractError(
            "artifact_sequence_mismatch",
            f"artifact bundle sequence does not match {candidate_id}",
        )
    protocol_raw = raw.get("protocol")
    if protocol_raw is not None:
        if not isinstance(protocol_raw, dict):
            raise ContractError(
                "artifact_protocol_type", "artifacts.json protocol must be an object"
            )
        missing = sorted({"name", "version", "sha256"} - set(protocol_raw))
        if missing:
            raise ContractError(
                "artifact_protocol_incomplete",
                f"artifacts.json protocol must contain {missing}",
            )
        if not all(isinstance(protocol_raw[key], str) for key in ("name", "version", "sha256")):
            raise ContractError(
                "artifact_protocol_type",
                "artifacts.json protocol name/version/sha256 must be strings",
            )
    base = path.parent
    global_raw = raw.get("global") or {}
    if not isinstance(global_raw, dict):
        raise ContractError("artifact_global_type", "global artifacts must be an object")
    unknown = sorted(set(global_raw) - {
        "monomer_predictions",
        "post_relax_pdb", "post_relax_pdb_sha256",
        "post_relax_metadata", "post_relax_metadata_sha256",
        "design_reference_pdb", "design_reference_pdb_sha256",
    })
    if unknown:
        raise ContractError(
            "artifact_unknown_keys", f"global artifacts contain unknown keys: {unknown}"
        )
    global_artifacts = dict(global_raw)
    predictions = global_raw.get("monomer_predictions") or []
    if not isinstance(predictions, list):
        raise ContractError(
            "artifact_prediction_type", "monomer_predictions must be a list"
        )
    global_artifacts["monomer_predictions"] = [
        _validate_prediction_entry(item, base, f"global.monomer_predictions[{index}]")
        for index, item in enumerate(predictions)
    ]
    for key in ("post_relax_pdb", "post_relax_metadata", "design_reference_pdb"):
        if global_raw.get(key):
            global_artifacts[key] = _materialize_file(
                global_raw, key, base, f"global.{key}"
            )

    target_raw = raw.get("targets") or {}
    if not isinstance(target_raw, dict):
        raise ContractError("artifact_targets_type", "targets artifacts must be an object")
    unexpected = sorted(set(target_raw) - set(required_targets))
    if unexpected:
        raise ContractError(
            "artifact_target_unexpected", f"unexpected target artifacts: {unexpected}"
        )
    target_artifacts: dict[str, dict] = {}
    for target_id in required_targets:
        values = target_raw.get(target_id) or {}
        if not isinstance(values, dict):
            raise ContractError(
                "artifact_target_type", f"target {target_id} artifacts must be an object"
            )
        unknown = sorted(set(values) - {
            "target_chain", "complex_predictions",
            "prodigy_output", "prodigy_output_sha256",
            "prodigy_outputs",
            "rosetta_output", "rosetta_output_sha256",
            "rosetta_outputs",
        })
        if unknown:
            raise ContractError(
                "artifact_unknown_keys",
                f"target {target_id} artifacts contain unknown keys: {unknown}",
            )
        result = dict(values)
        predictions = values.get("complex_predictions") or []
        if not isinstance(predictions, list):
            raise ContractError(
                "artifact_prediction_type",
                f"{target_id}.complex_predictions must be a list",
            )
        result["complex_predictions"] = [
            _validate_prediction_entry(
                item, base, f"targets.{target_id}.complex_predictions[{index}]"
            )
            for index, item in enumerate(predictions)
        ]
        prodigy_outputs = values.get("prodigy_outputs") or []
        if not isinstance(prodigy_outputs, list):
            raise ContractError(
                "artifact_scored_output_type",
                f"{target_id}.prodigy_outputs must be a list",
            )
        if values.get("prodigy_output") and prodigy_outputs:
            raise ContractError(
                "prodigy_evidence_ambiguous",
                f"{target_id} cannot declare both prodigy_output and prodigy_outputs",
            )
        predictions_by_hash = {
            item["pdb"]["sha256"]: item
            for item in result["complex_predictions"]
        }
        result["prodigy_outputs"] = [
            _validate_scored_output(
                item,
                base,
                f"targets.{target_id}.prodigy_outputs[{index}]",
                predictions_by_hash,
            )
            for index, item in enumerate(prodigy_outputs)
        ]
        identities = [
            (item["predictor"], item["model_id"], item["seed"])
            for item in result["prodigy_outputs"]
        ]
        if len(set(identities)) != len(identities):
            raise ContractError(
                "scored_output_identity_duplicate",
                f"{target_id}.prodigy_outputs contains duplicate model identities",
            )
        linked_prediction_hashes = {
            item["prediction_pdb_sha256"] for item in result["prodigy_outputs"]
        }
        if result["prodigy_outputs"] and (
            len(result["prodigy_outputs"]) != len(result["complex_predictions"])
            or linked_prediction_hashes != set(predictions_by_hash)
        ):
            raise ContractError(
                "prodigy_coverage_mismatch",
                f"{target_id}.prodigy_outputs must cover every complex prediction once",
            )
        rosetta_outputs = values.get("rosetta_outputs") or []
        if not isinstance(rosetta_outputs, list):
            raise ContractError(
                "artifact_scored_output_type",
                f"{target_id}.rosetta_outputs must be a list",
            )
        if values.get("rosetta_output") and rosetta_outputs:
            raise ContractError(
                "rosetta_evidence_ambiguous",
                f"{target_id} cannot declare both rosetta_output and rosetta_outputs",
            )
        result["rosetta_outputs"] = [
            _validate_scored_output(
                item,
                base,
                f"targets.{target_id}.rosetta_outputs[{index}]",
                predictions_by_hash,
            )
            for index, item in enumerate(rosetta_outputs)
        ]
        target_chain = str(values.get("target_chain") or "").strip()
        if result["rosetta_outputs"] and not target_chain:
            raise ContractError(
                "target_chain_missing",
                f"{target_id}.target_chain is required with Rosetta evidence",
            )
        for index, item in enumerate(result["rosetta_outputs"]):
            _validate_rosetta_metadata(
                item,
                label=f"targets.{target_id}.rosetta_outputs[{index}]",
                prediction=predictions_by_hash[item["prediction_pdb_sha256"]],
                target_chain=target_chain,
                sequence=sequence,
            )
        identities = [
            (item["predictor"], item["model_id"], item["seed"])
            for item in result["rosetta_outputs"]
        ]
        if len(set(identities)) != len(identities):
            raise ContractError(
                "scored_output_identity_duplicate",
                f"{target_id}.rosetta_outputs contains duplicate model identities",
            )
        linked_prediction_hashes = {
            item["prediction_pdb_sha256"] for item in result["rosetta_outputs"]
        }
        if result["rosetta_outputs"] and (
            len(result["rosetta_outputs"]) != len(result["complex_predictions"])
            or linked_prediction_hashes != set(predictions_by_hash)
        ):
            raise ContractError(
                "rosetta_coverage_mismatch",
                f"{target_id}.rosetta_outputs must cover every complex prediction once",
            )
        for key in ("prodigy_output", "rosetta_output"):
            if values.get(key):
                result[key] = _materialize_file(
                    values, key, base, f"targets.{target_id}.{key}"
                )
        target_artifacts[target_id] = result

    file_inventory = []
    for entry in global_artifacts["monomer_predictions"]:
        file_inventory.extend(
            value["sha256"] for key, value in entry.items()
            if key in {"pdb", "pae", "metadata"} and isinstance(value, dict)
        )
    for key in ("post_relax_pdb", "post_relax_metadata", "design_reference_pdb"):
        if isinstance(global_artifacts.get(key), dict):
            file_inventory.append(global_artifacts[key]["sha256"])
    for values in target_artifacts.values():
        for entry in values["complex_predictions"]:
            file_inventory.extend(
                value["sha256"] for key, value in entry.items()
                if key in {"pdb", "pae", "metadata"} and isinstance(value, dict)
            )
        for key in ("prodigy_output", "rosetta_output"):
            if isinstance(values.get(key), dict):
                file_inventory.append(values[key]["sha256"])
        file_inventory.extend(
            item["output"]["sha256"] for item in values.get("prodigy_outputs", [])
        )
        for item in values.get("rosetta_outputs", []):
            file_inventory.append(item["output"]["sha256"])
            if isinstance(item.get("metadata"), dict):
                file_inventory.append(item["metadata"]["sha256"])
    bundle_sha = file_sha256(path)
    digest = object_sha256({"bundle": bundle_sha, "files": sorted(file_inventory)})
    return ArtifactBundle(
        path=path,
        sha256=bundle_sha,
        candidate_id=candidate_id,
        sequence=sequence,
        global_artifacts=global_artifacts,
        target_artifacts=target_artifacts,
        digest=digest,
    )


def parse_metadata(path_entry: dict | None) -> dict:
    if not path_entry:
        return {}
    try:
        value = json.loads(path_entry["path"].read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractError(
            "prediction_metadata_malformed",
            f"invalid prediction metadata JSON: {path_entry['path']}",
        ) from exc
    if not isinstance(value, dict):
        raise ContractError(
            "prediction_metadata_type", "prediction metadata must be an object"
        )
    return value


def parse_target_physics(target_artifacts: dict) -> tuple[dict, list[dict]]:
    metrics, provenance = {}, []
    prodigy_outputs = target_artifacts.get("prodigy_outputs") or []
    prodigy = target_artifacts.get("prodigy_output")
    if prodigy_outputs:
        samples = []
        for entry in prodigy_outputs:
            parsed = parse_prodigy_output(
                entry["output"]["path"].read_text(
                    encoding="utf-8", errors="replace"
                )
            )
            samples.append({
                "predictor": entry["predictor"],
                "model_id": entry["model_id"],
                "seed": entry["seed"],
                "prediction_pdb_sha256": entry["prediction_pdb_sha256"],
                "artifact": str(entry["output"]["path"]),
                "sha256": entry["output"]["sha256"],
                "metrics": parsed,
            })
        methods = {sample["metrics"].get("dg_method") for sample in samples}
        if len(methods) != 1:
            raise ContractError(
                "prodigy_method_inconsistent",
                f"PRODIGY outputs use inconsistent methods: {sorted(methods)}",
            )
        metrics["dg"] = float(statistics.median(
            sample["metrics"]["dg"] for sample in samples
        ))
        metrics["dg_method"] = methods.pop()
        provenance.append({
            "tool": "PRODIGY",
            "aggregation": "median_across_declared_predictions",
            "samples": samples,
            "metrics": ["dg", "dg_method"],
        })
    elif prodigy:
        parsed = parse_prodigy_output(
            prodigy["path"].read_text(encoding="utf-8", errors="replace")
        )
        metrics.update(parsed)
        provenance.append({
            "tool": "PRODIGY",
            "aggregation": "legacy_single_prediction",
            "artifact": str(prodigy["path"]),
            "sha256": prodigy["sha256"],
            "metrics": sorted(parsed),
        })
    rosetta_outputs = target_artifacts.get("rosetta_outputs") or []
    rosetta = target_artifacts.get("rosetta_output")
    if rosetta_outputs:
        samples = []
        for entry in rosetta_outputs:
            parsed = parse_rosetta_interface_output(
                entry["output"]["path"].read_text(
                    encoding="utf-8", errors="replace"
                )
            )
            samples.append({
                "predictor": entry["predictor"],
                "model_id": entry["model_id"],
                "seed": entry["seed"],
                "prediction_pdb_sha256": entry["prediction_pdb_sha256"],
                "artifact": str(entry["output"]["path"]),
                "sha256": entry["output"]["sha256"],
                "metadata_artifact": (
                    str(entry["metadata"]["path"]) if entry.get("metadata") else None
                ),
                "metadata_sha256": (
                    entry["metadata"]["sha256"] if entry.get("metadata") else None
                ),
                "metrics": parsed,
            })
        metrics["sc"] = float(statistics.median(
            sample["metrics"]["sc"] for sample in samples
        ))
        metrics["dsasa"] = float(statistics.median(
            sample["metrics"]["dsasa"] for sample in samples
        ))
        rosetta_dg = [
            sample["metrics"].get("rosetta_dg_separated") for sample in samples
        ]
        if all(value is not None for value in rosetta_dg):
            metrics["rosetta_dg_separated"] = float(statistics.median(rosetta_dg))
        provenance.append({
            "tool": "Rosetta InterfaceAnalyzer",
            "aggregation": "median_across_declared_predictions",
            "samples": samples,
            "metrics": [
                key for key in ("sc", "dsasa", "rosetta_dg_separated")
                if key in metrics
            ],
        })
    elif rosetta:
        parsed = parse_rosetta_interface_output(
            rosetta["path"].read_text(encoding="utf-8", errors="replace")
        )
        metrics.update(parsed)
        provenance.append({
            "tool": "Rosetta InterfaceAnalyzer",
            "artifact": str(rosetta["path"]),
            "sha256": rosetta["sha256"],
            "metrics": sorted(parsed),
        })
    return metrics, provenance


def run_prodigy(
    complex_pdb: str | Path,
    target_chain: str,
    binder_chain: str,
    *,
    executable: str = "prodigy",
    timeout: int = 300,
) -> tuple[dict, dict]:
    result = run_command(
        [
            executable,
            "-q",
            str(Path(complex_pdb).resolve()),
            "--selection",
            target_chain,
            binder_chain,
        ],
        timeout=timeout,
    )
    if result.exit_code != 0:
        raise ContractError(
            "prodigy_failed",
            f"PRODIGY exited {result.exit_code}: {result.stderr[-500:]}",
        )
    return parse_prodigy_output(result.stdout), result.trace("PRODIGY")


def build_colabdesign_command(
    *,
    python: str,
    sequence: str,
    output_dir: str | Path,
    data_dir: str | Path,
    colabdesign_dir: str | Path,
    expected_commit: str,
    seed: int,
    model_number: int,
    num_recycles: int,
    target_pdb: str | Path | None = None,
    target_chain: str | None = None,
    use_multimer: bool = True,
) -> list[str]:
    command = [
        python,
        "-m",
        "prediction_pipeline.colabdesign_worker",
        "--sequence", sequence,
        "--output-dir", str(Path(output_dir).resolve()),
        "--data-dir", str(Path(data_dir).resolve()),
        "--colabdesign-dir", str(Path(colabdesign_dir).resolve()),
        "--expected-commit", expected_commit,
        "--seed", str(seed),
        "--model-number", str(model_number),
        "--num-recycles", str(num_recycles),
    ]
    if target_pdb:
        if not target_chain:
            raise ContractError(
                "target_chain_missing", "target_chain is required with target_pdb"
            )
        command.extend([
            "--target-pdb", str(Path(target_pdb).resolve()),
            "--target-chain", target_chain,
        ])
        if use_multimer:
            command.append("--use-multimer")
    return command


def prediction_environment(cuda_data_dir: str | None = None) -> dict:
    env = dict(os.environ)
    if cuda_data_dir:
        env["XLA_FLAGS"] = f"--xla_gpu_cuda_data_dir={cuda_data_dir}"
    return env
