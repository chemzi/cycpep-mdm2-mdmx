"""Run the pinned ColabDesign adapter on a GPU server and build artifacts.json.

This runner intentionally produces a partial evidence bundle: monomer and
per-target ColabDesign predictions (plus optional PRODIGY).  Rosetta
InterfaceAnalyzer, post-relax structures, and an independent second predictor
must be registered before those layers can clear.  Prediction will report the
remaining work as pending.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_layer import CandidateIndex, State  # noqa: E402
from prediction_pipeline.adapters import (  # noqa: E402
    build_colabdesign_command,
    prediction_environment,
    run_command,
)
from prediction_pipeline.contracts import (  # noqa: E402
    ContractError,
    PredictionConfig,
    candidate_from_row,
    file_sha256,
    validate_project,
)
from target_bootstrap import assert_project_approved  # noqa: E402
from execution.supervisor import atomic_json  # noqa: E402
from core.protocol import ProtocolError  # noqa: E402
from prediction_pipeline.protocol import (  # noqa: E402
    MIGRATE_LEGACY_HINT,
    PREDICTION_PROTOCOL,
    protocol_binding,
    validate_execution_compatibility,
)


DEFAULT_PYTHON = os.environ.get(
    "CYCPEP_PYTHON", "/root/damodel-tmp/envs/cycpep-prediction/bin/python"
)
DEFAULT_COLABDESIGN = os.environ.get(
    "COLABDESIGN_DIR", "/root/workspace/NovaPeptide/tools/ColabDesign"
)
DEFAULT_PARAMS = os.environ.get(
    "COLABDESIGN_PARAMS", f"{DEFAULT_COLABDESIGN}/params"
)
DEFAULT_CUDA = os.environ.get(
    "CUDA_DATA_DIR",
    "/root/damodel-tmp/envs/cycpep-prediction/"
    "lib/python3.10/site-packages/nvidia/cuda_nvcc",
)


PROTOCOL_BINDING_FILENAME = "protocol_binding.json"


def parse_ensemble_members(
    seeds_raw: str,
    model_numbers_raw: str | None = None,
) -> list[tuple[int, int]]:
    """Return distinct ``(seed, AF2 model_number)`` ensemble members.

    Fixed-weight ColabDesign inference with dropout disabled is deterministic:
    changing only ``seed`` produces byte-identical structures.  Production
    ensembles therefore pair seeds with distinct AF2 parameter models.
    """
    try:
        seeds = [int(value) for value in seeds_raw.split(",") if value.strip()]
    except ValueError as exc:
        raise ContractError("seed_list_invalid", "--seeds must contain integers") from exc
    if len(set(seeds)) != len(seeds) or not seeds:
        raise ContractError("seed_list_invalid", "--seeds must contain unique integers")

    if model_numbers_raw:
        try:
            models = [
                int(value) for value in model_numbers_raw.split(",") if value.strip()
            ]
        except ValueError as exc:
            raise ContractError(
                "model_list_invalid", "--model-numbers must contain integers"
            ) from exc
    else:
        # Default pairing pairs each seed with a distinct AF2 model, matching
        # the protocol ensemble; overriding requires explicit --model-numbers.
        models = list(range(len(seeds)))

    if len(models) != len(seeds):
        raise ContractError(
            "ensemble_length_mismatch",
            "--model-numbers must contain exactly one value per seed",
        )
    if any(model < 0 or model > 4 for model in models):
        raise ContractError(
            "model_list_invalid", "ColabDesign AF2 model numbers must be within 0-4"
        )
    if len(set(models)) != len(models):
        raise ContractError(
            "ensemble_model_duplicate",
            "dropout-free inference requires distinct AF2 model numbers; changing "
            "only the seed produces duplicate evidence",
        )
    return list(zip(seeds, models))


def _relative(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def _validated_target_coordinates(target: dict) -> tuple[Path, str]:
    structure = target.get("structure") or {}
    raw_path = structure.get("coordinate_path")
    declared_hash = str(structure.get("coordinate_sha256") or "").lower()
    if not raw_path or len(declared_hash) != 64:
        raise ContractError(
            "target_coordinates_not_ready",
            f"{target['id']} requires reviewed coordinate_path and full SHA-256",
        )
    path = Path(raw_path).expanduser().resolve()
    if not path.is_file():
        raise ContractError(
            "target_coordinates_missing", f"{target['id']} coordinates missing: {path}"
        )
    actual = file_sha256(path)
    if actual != declared_hash:
        raise ContractError(
            "target_coordinates_hash_mismatch",
            f"{target['id']} coordinate SHA-256 changed",
        )
    chain = str(structure.get("chain") or "").strip()
    if not chain:
        raise ContractError("target_chain_missing", f"{target['id']} chain is missing")
    return path, chain


def _prediction_entry(
    output_dir: Path, predictor: str, seed: int, *, primary: bool
) -> dict:
    pdb = output_dir / "prediction.pdb"
    pae = output_dir / "pae.npz"
    metadata = output_dir / "metadata.json"
    for path in (pdb, pae, metadata):
        if not path.is_file():
            raise ContractError("predictor_output_missing", f"missing predictor output: {path}")
    return {
        "predictor": predictor,
        "seed": seed,
        "primary": primary,
        "pdb": str(pdb),
        "pdb_sha256": file_sha256(pdb),
        "pae": str(pae),
        "pae_sha256": file_sha256(pae),
        "metadata": str(metadata),
        "metadata_sha256": file_sha256(metadata),
    }


def _read_protocol_binding(output_dir: Path) -> dict | None:
    """Return the protocol binding recorded next to a prediction output, or None."""
    path = output_dir / PROTOCOL_BINDING_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ContractError(
            "resume_protocol_corrupt", f"corrupt protocol binding: {path}"
        ) from exc
    if not isinstance(data, dict):
        raise ContractError(
            "resume_protocol_corrupt", f"invalid protocol binding: {path}"
        )
    return data


def _write_protocol_binding(output_dir: Path) -> None:
    atomic_json(output_dir / PROTOCOL_BINDING_FILENAME, protocol_binding())


def _require_resume_protocol(output_dir: Path) -> None:
    """Refuse to reuse evidence whose recorded protocol is unknown or stale."""
    recorded = _read_protocol_binding(output_dir)
    if recorded is None:
        raise ContractError(
            "resume_protocol_unrecorded",
            f"{output_dir} has no recorded prediction protocol; delete the "
            f"output and rerun, or {MIGRATE_LEGACY_HINT}",
        )
    if recorded != protocol_binding():
        raise ContractError(
            "resume_protocol_mismatch",
            f"{output_dir} was produced under a different prediction "
            f"protocol; delete the output and rerun, or {MIGRATE_LEGACY_HINT}",
        )


def _require_existing_bundle_protocol(bundle_path: Path) -> None:
    """Refuse to rewrite a candidate bundle whose protocol is unknown or stale."""
    try:
        raw = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ContractError(
            "artifact_bundle_malformed", f"corrupt artifacts.json: {bundle_path}"
        ) from exc
    if not isinstance(raw, dict):
        raise ContractError(
            "artifact_bundle_type", f"invalid artifacts.json: {bundle_path}"
        )
    try:
        validate_execution_compatibility(raw)
    except ProtocolError as exc:
        if raw.get("protocol") is None:
            raise ContractError(
                "bundle_protocol_unrecorded",
                f"{bundle_path} has no recorded prediction protocol; delete "
                f"the candidate output and rerun, or {MIGRATE_LEGACY_HINT}",
            ) from exc
        raise ContractError(
            "bundle_protocol_mismatch",
            f"{bundle_path} was produced under a different prediction "
            f"protocol; delete the candidate output and rerun, or "
            f"{MIGRATE_LEGACY_HINT}",
        ) from exc


def _require_resume_parameters(recorded: dict, expected: dict) -> None:
    """Refuse to reuse a prediction computed under different parameters.

    ``--resume`` may only reuse an output whose recorded execution parameters
    (the designed sequence, AF2 model, seed and recycle count written by the
    ColabDesign worker) are identical to the requested run.  A candidate_id
    can be recycled after a state reset while the artifacts_root was left in
    place; without this check the stale prediction would be silently attached
    to the new candidate's bundle under a valid protocol binding.
    """
    if not isinstance(recorded, dict):
        raise ContractError(
            "predictor_metadata_corrupt",
            "recorded metadata.json is not an object; delete the output and rerun",
        )
    mismatches = []
    for field, expected_value in expected.items():
        actual = recorded.get(field)
        if actual != expected_value:
            mismatches.append(
                f"{field}: recorded={actual!r} expected={expected_value!r}"
            )
    if mismatches:
        raise ContractError(
            "resume_parameter_mismatch",
            "recorded prediction parameters differ from the requested run; "
            "delete the output and rerun: " + "; ".join(mismatches),
        )


def _run_one(
    command: list[str],
    output_dir: Path,
    args,
    *,
    expected: dict | None = None,
) -> dict:
    metadata = output_dir / "metadata.json"
    if args.resume and metadata.is_file():
        _require_resume_protocol(output_dir)
        try:
            recorded = json.loads(metadata.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ContractError(
                "predictor_metadata_corrupt", f"corrupt metadata.json: {metadata}"
            ) from exc
        if expected is not None:
            _require_resume_parameters(recorded, expected)
        return recorded
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ContractError(
            "predictor_output_exists",
            f"output exists; use --resume after inspecting it: {output_dir}",
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    result = run_command(
        command,
        timeout=args.timeout,
        cwd=ROOT,
        env=prediction_environment(args.cuda_data_dir),
    )
    (output_dir / "stdout.log").write_text(result.stdout, encoding="utf-8")
    (output_dir / "stderr.log").write_text(result.stderr, encoding="utf-8")
    if result.exit_code:
        raise ContractError(
            "colabdesign_failed",
            f"ColabDesign exited {result.exit_code}; see {output_dir / 'stderr.log'}",
        )
    if not metadata.is_file():
        raise ContractError("colabdesign_metadata_missing", f"missing {metadata}")
    _write_protocol_binding(output_dir)
    try:
        return json.loads(metadata.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractError(
            "predictor_metadata_corrupt", f"corrupt metadata.json: {metadata}"
        ) from exc


def require_design_references(candidates) -> None:
    """Fail before any GPU work when L7 cannot possibly be evaluated."""
    missing = sorted(
        candidate.candidate_id
        for candidate in candidates
        if candidate.design_reference_pdb is None
    )
    if missing:
        raise ContractError(
            "design_reference_missing_preflight",
            "independent Design reference is missing for "
            f"{missing}; regenerate these candidates in Design before Prediction",
        )


def _require_protocol_parameters(
    ensemble: list[tuple[int, int]], num_recycles: int
) -> None:
    """Execution parameters must be provably identical to the protocol file.

    The artifact binding records the protocol SHA-256; a run that silently
    used different seeds / models / recycles would attach that digest to
    evidence it did not actually follow.  Overriding parameters is therefore
    refused: change ``protocols/prediction_v1.json`` (and bump version)
    instead.
    """
    af2 = PREDICTION_PROTOCOL["parameters"]["af2_prodigy"]
    seeds = [seed for seed, _ in ensemble]
    models = [model for _, model in ensemble]
    if num_recycles != af2["num_recycles"]:
        raise ContractError(
            "protocol_parameter_mismatch",
            f"--num-recycles {num_recycles} != protocol {af2['num_recycles']}; "
            "bump the protocol version instead of overriding parameters",
        )
    if seeds != af2["seeds"] or models != af2["model_numbers"]:
        raise ContractError(
            "protocol_parameter_mismatch",
            "--seeds/--model-numbers differ from protocols/prediction_v1.json; "
            "bump the protocol version instead of overriding parameters",
        )


def run(args) -> dict:
    state = State.load()
    project = state.get("project_config") or State._project_config
    assert_project_approved(project)
    required_targets = validate_project(project)
    config = PredictionConfig()
    target_by_id = {target["id"]: target for target in project["targets"]}
    target_inputs = {
        target_id: _validated_target_coordinates(target_by_id[target_id])
        for target_id in required_targets
    }
    requested = set(args.candidate or [])
    rows = [
        row for row in CandidateIndex.load()
        if not requested or row["candidate_id"] in requested
    ]
    if not rows:
        raise ContractError("no_candidates", "no matching Design candidates")
    ensemble = parse_ensemble_members(args.seeds, args.model_numbers)
    _require_protocol_parameters(ensemble, args.num_recycles)

    candidates = [candidate_from_row(row) for row in rows]
    require_design_references(candidates)

    artifacts_root = Path(args.artifacts_root).expanduser().resolve()
    summaries = []
    for candidate in candidates:
        candidate_dir = artifacts_root / candidate.candidate_id
        candidate_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = candidate_dir / "artifacts.json"
        if bundle_path.is_file():
            _require_existing_bundle_protocol(bundle_path)

        primary_seed, primary_model = ensemble[0]
        monomer_dir = (
            candidate_dir / "colabdesign_monomer"
            / f"model_{primary_model}_seed_{primary_seed}"
        )
        monomer_command = build_colabdesign_command(
            python=args.python,
            sequence=candidate.sequence,
            output_dir=monomer_dir,
            data_dir=args.data_dir,
            colabdesign_dir=args.colabdesign_dir,
            expected_commit=config.colabdesign_commit,
            seed=primary_seed,
            model_number=primary_model,
            num_recycles=args.num_recycles,
        )
        _run_one(
            monomer_command,
            monomer_dir,
            args,
            expected={
                "requested_sequence": candidate.sequence,
                "seed": primary_seed,
                "model_number": primary_model,
                "num_recycles": args.num_recycles,
            },
        )
        global_artifacts = {
            "monomer_predictions": [
                _prediction_entry(
                    monomer_dir, "ColabDesign", primary_seed, primary=True
                )
            ],
            "design_reference_pdb": str(candidate.design_reference_pdb),
            "design_reference_pdb_sha256": candidate.design_reference_sha256,
        }

        target_artifacts = {}
        for target_id in required_targets:
            target_pdb, target_chain = target_inputs[target_id]
            predictions = []
            for index, (seed, model_number) in enumerate(ensemble):
                output_dir = (
                    candidate_dir / "colabdesign_complex" / target_id
                    / f"model_{model_number}_seed_{seed}"
                )
                command = build_colabdesign_command(
                    python=args.python,
                    sequence=candidate.sequence,
                    output_dir=output_dir,
                    data_dir=args.data_dir,
                    colabdesign_dir=args.colabdesign_dir,
                    expected_commit=config.colabdesign_commit,
                    seed=seed,
                    model_number=model_number,
                    num_recycles=args.num_recycles,
                    target_pdb=target_pdb,
                    target_chain=target_chain,
                    use_multimer=True,
                )
                _run_one(
                    command,
                    output_dir,
                    args,
                    expected={
                        "requested_sequence": candidate.sequence,
                        "seed": seed,
                        "model_number": model_number,
                        "num_recycles": args.num_recycles,
                    },
                )
                predictions.append(_prediction_entry(
                    output_dir, "ColabDesign", seed, primary=index == 0
                ))
            target_artifacts[target_id] = {
                "target_chain": target_chain,
                "complex_predictions": predictions,
            }
            if args.prodigy:
                prodigy_outputs = []
                for prediction in predictions:
                    prediction_pdb = Path(prediction["pdb"])
                    metadata = json.loads(Path(prediction["metadata"]).read_text())
                    binder_chain = metadata["binder_chain"]
                    result = run_command(
                        [
                            args.prodigy, "-q", str(prediction_pdb),
                            "--selection", target_chain, binder_chain,
                        ],
                        timeout=300,
                    )
                    if result.exit_code:
                        raise ContractError(
                            "prodigy_failed",
                            f"PRODIGY failed for {candidate.candidate_id}/{target_id}/"
                            f"{metadata['model_id']}: {result.stderr[-500:]}",
                        )
                    prodigy_path = (
                        candidate_dir
                        / f"{target_id}_{metadata['model_id']}_seed_{prediction['seed']}_prodigy.txt"
                    )
                    prodigy_path.write_text(result.stdout, encoding="utf-8")
                    prodigy_outputs.append({
                        "predictor": prediction["predictor"],
                        "model_id": metadata["model_id"],
                        "seed": prediction["seed"],
                        "prediction_pdb_sha256": prediction["pdb_sha256"],
                        "output": str(prodigy_path),
                        "output_sha256": file_sha256(prodigy_path),
                    })
                target_artifacts[target_id]["prodigy_outputs"] = prodigy_outputs

        bundle = {
            "schema_version": 1,
            "candidate_id": candidate.candidate_id,
            "sequence": candidate.sequence,
            "protocol": protocol_binding(),
            "global": global_artifacts,
            "targets": target_artifacts,
        }
        bundle_path = candidate_dir / "artifacts.json"
        atomic_json(bundle_path, bundle)
        summaries.append({
            "candidate_id": candidate.candidate_id,
            "artifact_bundle": str(bundle_path),
            "artifact_bundle_sha256": file_sha256(bundle_path),
        })
    return {"candidate_count": len(summaries), "candidates": summaries}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts-root", required=True)
    parser.add_argument("--candidate", action="append")
    parser.add_argument("--python", default=DEFAULT_PYTHON)
    parser.add_argument("--data-dir", default=DEFAULT_PARAMS)
    parser.add_argument("--colabdesign-dir", default=DEFAULT_COLABDESIGN)
    parser.add_argument("--cuda-data-dir", default=DEFAULT_CUDA)
    parser.add_argument(
        "--seeds",
        default=",".join(str(v) for v in PREDICTION_PROTOCOL["parameters"]["af2_prodigy"]["seeds"]),
    )
    parser.add_argument(
        "--model-numbers",
        default=",".join(
            str(value) for value in PREDICTION_PROTOCOL["parameters"]["af2_prodigy"]["model_numbers"]
        ),
        help="comma-separated AF2 models paired with --seeds; "
        "default from protocols/prediction_v1.json",
    )
    parser.add_argument(
        "--num-recycles",
        type=int,
        default=PREDICTION_PROTOCOL["parameters"]["af2_prodigy"]["num_recycles"],
    )
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--prodigy", help="path/name of the PRODIGY executable")
    parser.add_argument("--resume", action="store_true")
    return parser


def main() -> int:
    try:
        summary = run(build_parser().parse_args())
    except Exception as exc:
        code = getattr(exc, "code", exc.__class__.__name__)
        print(json.dumps(
            {"status": "error", "code": code, "message": str(exc)},
            ensure_ascii=False,
        ))
        return 2
    print(json.dumps({"status": "complete", "summary": summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
