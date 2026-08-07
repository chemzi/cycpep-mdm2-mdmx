"""Add Boltz-2 and optional Rosetta evidence without mutating source artifacts.

The output bundle may reference immutable source prediction files by absolute
path.  New Boltz/PRODIGY/Rosetta files are written below ``--output-root``.
State and CandidateIndex are read-only in this command.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_layer import CandidateIndex, State  # noqa: E402
from execution.supervisor import atomic_json  # noqa: E402
from core.protocol import ProtocolError  # noqa: E402
from prediction_pipeline.protocol import (  # noqa: E402
    PREDICTION_PROTOCOL,
    validate_bundle_protocol,
    validate_execution_compatibility,
)
from prediction_pipeline.adapters import (  # noqa: E402
    load_artifact_bundle,
    run_command,
)
from prediction_pipeline.boltz_worker import run_boltz_prediction  # noqa: E402
from prediction_pipeline.contracts import (  # noqa: E402
    ContractError,
    candidate_from_row,
    file_sha256,
    validate_project,
)
from prediction_pipeline.rosetta_worker import run_rosetta_interface  # noqa: E402
from prediction_pipeline.relax_worker import run_post_relax  # noqa: E402
from prediction_pipeline.structures import (  # noqa: E402
    exact_sequence_chain,
    parse_pdb,
)
from target_bootstrap import assert_project_approved  # noqa: E402


FILE_KEYS = {
    "pdb", "pae", "metadata", "post_relax_pdb", "post_relax_metadata",
    "design_reference_pdb", "prodigy_output", "rosetta_output", "output",
}


def _absolutize_artifact_paths(value, base: Path) -> None:
    if isinstance(value, list):
        for item in value:
            _absolutize_artifact_paths(item, base)
        return
    if not isinstance(value, dict):
        return
    for key, item in list(value.items()):
        if key in FILE_KEYS and isinstance(item, str) and item.strip():
            path = Path(item).expanduser()
            value[key] = str(path.resolve() if path.is_absolute() else (base / path).resolve())
        else:
            _absolutize_artifact_paths(item, base)


def _target_coordinates(target: dict) -> tuple[Path, str, str]:
    structure = target.get("structure") or {}
    path = Path(str(structure.get("coordinate_path") or "")).expanduser().resolve()
    declared = str(structure.get("coordinate_sha256") or "").strip().lower()
    chain = str(structure.get("chain") or "").strip()
    if not path.is_file() or len(declared) != 64 or file_sha256(path) != declared:
        raise ContractError(
            "target_coordinates_not_ready",
            f"{target.get('id')} requires hash-verified coordinate_path",
        )
    parsed = parse_pdb(path)
    if chain not in parsed.chains:
        raise ContractError(
            "target_chain_missing", f"{target.get('id')} chain {chain!r} is absent"
        )
    return path, chain, parsed.sequence(chain)


def _prediction_paths(prediction: dict, base: Path) -> tuple[Path, Path, dict]:
    pdb = Path(prediction["pdb"]).expanduser()
    metadata = Path(prediction["metadata"]).expanduser()
    if not pdb.is_absolute():
        pdb = (base / pdb).resolve()
    if not metadata.is_absolute():
        metadata = (base / metadata).resolve()
    if not pdb.is_file() or not metadata.is_file():
        raise ContractError("predictor_output_missing", f"missing {pdb} or {metadata}")
    try:
        metadata_values = json.loads(metadata.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractError("prediction_metadata_malformed", str(metadata)) from exc
    return pdb, metadata, metadata_values


def _run_prodigy_for_prediction(
    *,
    executable: str,
    prediction: dict,
    source_base: Path,
    target_chain: str,
    binder_sequence: str,
    output_path: Path,
) -> dict:
    pdb, _, metadata = _prediction_paths(prediction, source_base)
    binder_chain = str(
        prediction.get("binder_chain") or metadata.get("binder_chain") or ""
    ).strip()
    if not binder_chain:
        binder_chain = exact_sequence_chain(parse_pdb(pdb), binder_sequence)
    result = run_command(
        [
            executable, "-q", str(pdb), "--selection", target_chain, binder_chain,
        ],
        timeout=300,
    )
    if result.exit_code:
        raise ContractError(
            "prodigy_failed", f"PRODIGY failed for {pdb}: {result.stderr[-500:]}"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(result.stdout, encoding="utf-8")
    return {
        "predictor": prediction["predictor"],
        "model_id": str(metadata.get("model_id") or ""),
        "seed": prediction["seed"],
        "prediction_pdb_sha256": file_sha256(pdb),
        "output": str(output_path),
        "output_sha256": file_sha256(output_path),
    }


def preflight_bundle_protocol(source_bundle: Path, raw: object) -> None:
    """Refuse legacy or stale bundles before any GPU enrichment work.

    Enrichment may run hours of Boltz/Rosetta compute; the bundle protocol
    must be verified up front so a legacy bundle (no ``protocol`` binding) or
    a bundle bound to a different protocol is rejected before burning GPU
    time, not after the work is done.
    """
    if not isinstance(raw, dict):
        raise ContractError(
            "artifact_bundle_type", f"artifacts.json must be an object: {source_bundle}"
        )
    try:
        validate_execution_compatibility(raw)
    except ProtocolError as exc:
        raise ContractError("bundle_protocol_mismatch", str(exc)) from exc


def _require_enrichment_protocol(args) -> None:
    """Enrichment parameters must be provably derived from the protocol file.

    Per-candidate seeds are the protocol seed bases shifted by a candidate
    offset; only the offset may vary.  The repeats count and the seed-base
    difference must match ``protocols/prediction_v1.json`` exactly, otherwise
    the resulting bundle would carry the protocol digest while having been
    computed with different parameters.
    """
    enrichment = PREDICTION_PROTOCOL["parameters"]["enrichment"]
    if args.post_relax_repeats != enrichment["post_relax_repeats"]:
        raise ContractError(
            "protocol_parameter_mismatch",
            f"--post-relax-repeats {args.post_relax_repeats} != protocol "
            f"{enrichment['post_relax_repeats']}",
        )
    if args.seed < enrichment["seed_base"]:
        raise ContractError(
            "protocol_parameter_mismatch",
            f"--seed {args.seed} < protocol seed_base "
            f"{enrichment['seed_base']}; seeds must be the protocol base "
            "shifted by a non-negative candidate offset",
        )
    if args.post_relax_seed < enrichment["post_relax_seed_base"]:
        raise ContractError(
            "protocol_parameter_mismatch",
            f"--post-relax-seed {args.post_relax_seed} < protocol "
            f"post_relax_seed_base {enrichment['post_relax_seed_base']}",
        )
    expected_diff = enrichment["post_relax_seed_base"] - enrichment["seed_base"]
    actual_diff = args.post_relax_seed - args.seed
    if actual_diff != expected_diff:
        raise ContractError(
            "protocol_parameter_mismatch",
            "--post-relax-seed must shift from the protocol seed base by the "
            "same candidate offset as --seed",
        )


def run(args) -> dict:
    boltz_values = (args.boltz, args.boltz_cache, args.boltz_checkpoint)
    if any(boltz_values) and not all(boltz_values):
        raise ContractError(
            "boltz_configuration_incomplete",
            "--boltz, --boltz-cache and --boltz-checkpoint must be supplied together",
        )
    add_boltz = all(boltz_values)
    add_rosetta = bool(args.rosetta_scripts or args.pyrosetta_python)
    add_post_relax = bool(args.post_relax_python)
    if args.rosetta_scripts and args.pyrosetta_python:
        raise ContractError(
            "rosetta_engine_invalid",
            "--rosetta-scripts and --pyrosetta-python are mutually exclusive",
        )
    if args.prodigy and not add_boltz:
        raise ContractError(
            "prodigy_without_new_prediction",
            "--prodigy is only used when this command adds a Boltz prediction",
        )
    if not add_boltz and not add_rosetta and not add_post_relax:
        raise ContractError(
            "enrichment_empty",
            "configure Boltz, Rosetta interface scoring and/or PyRosetta post-relax",
        )
    _require_enrichment_protocol(args)
    source_bundle = Path(args.source_bundle).expanduser().resolve()
    try:
        raw = json.loads(source_bundle.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ContractError("artifact_bundle_malformed", str(source_bundle)) from exc
    preflight_bundle_protocol(source_bundle, raw)
    candidate_id = str(raw.get("candidate_id") or "")
    rows = [
        row for row in CandidateIndex.load() if row.get("candidate_id") == candidate_id
    ]
    if len(rows) != 1:
        raise ContractError(
            "candidate_missing", f"expected one CandidateIndex row for {candidate_id}"
        )
    candidate = candidate_from_row(rows[0])
    if candidate.design_reference_pdb is None:
        raise ContractError(
            "design_reference_missing_preflight",
            f"{candidate.candidate_id} has no independent L7 Design reference; "
            "regenerate it in Design before running Boltz/Rosetta/post-relax",
        )
    state = State.load()
    project = state.get("project_config") or State._project_config
    assert_project_approved(project)
    required_targets = validate_project(project)
    load_artifact_bundle(
        source_bundle,
        candidate_id=candidate.candidate_id,
        sequence=candidate.sequence,
        required_targets=required_targets,
    )

    bundle = copy.deepcopy(raw)
    _absolutize_artifact_paths(bundle, source_bundle.parent)
    output_root = Path(args.output_root).expanduser().resolve()
    candidate_dir = output_root / candidate.candidate_id
    if candidate_dir.exists() and any(candidate_dir.iterdir()):
        raise ContractError(
            "enrichment_output_exists", f"output candidate directory exists: {candidate_dir}"
        )
    candidate_dir.mkdir(parents=True, exist_ok=True)
    target_by_id = {target["id"]: target for target in project["targets"]}

    if add_post_relax:
        monomer_predictions = bundle["global"].get("monomer_predictions") or []
        if not monomer_predictions:
            raise ContractError(
                "post_relax_input_missing", "source bundle has no monomer prediction"
            )
        primary_monomer = sorted(
            monomer_predictions,
            key=lambda item: (
                not bool(item.get("primary")),
                str(item.get("predictor") or ""),
                int(item.get("seed") or 0),
            ),
        )[0]
        monomer_pdb = Path(primary_monomer["pdb"]).expanduser().resolve()
        relax_result = run_post_relax(
            pyrosetta_python=args.post_relax_python,
            monomer_pdb=monomer_pdb,
            sequence=candidate.sequence,
            cyclization_type=candidate.cyclization_type,
            output_dir=candidate_dir / "post_relax",
            seed=args.post_relax_seed,
            repeats=args.post_relax_repeats,
            coordinate_stdev_angstrom=args.post_relax_coordinate_stdev,
            timeout=args.post_relax_timeout,
        )
        for key in (
            "post_relax_pdb",
            "post_relax_pdb_sha256",
            "post_relax_metadata",
            "post_relax_metadata_sha256",
        ):
            bundle["global"][key] = relax_result[key]

    for target_id in required_targets:
        _, target_chain, target_sequence = _target_coordinates(target_by_id[target_id])
        target_values = bundle["targets"][target_id]
        if str(target_values.get("target_chain") or target_chain) != target_chain:
            raise ContractError(
                "target_chain_mismatch", f"{target_id} source/config target chains differ"
            )
        if add_boltz:
            boltz_dir = candidate_dir / "boltz_complex" / target_id / f"seed_{args.seed}"
            boltz_prediction = run_boltz_prediction(
                boltz_executable=args.boltz,
                cache_dir=args.boltz_cache,
                checkpoint=args.boltz_checkpoint,
                target_sequence=target_sequence,
                binder_sequence=candidate.sequence,
                output_dir=boltz_dir,
                target_chain=target_chain,
                binder_chain=args.binder_chain,
                seed=args.seed,
                diffusion_samples=PREDICTION_PROTOCOL["parameters"]["boltz"]["diffusion_samples"],
                timeout=args.timeout,
                no_kernels=args.no_kernels,
            )
            target_values["complex_predictions"].append(boltz_prediction)

            if not args.prodigy:
                if target_values.get("prodigy_outputs"):
                    raise ContractError(
                        "prodigy_required_for_enrichment",
                        "adding Boltz requires PRODIGY coverage for the new prediction",
                    )
            else:
                existing_outputs = target_values.get("prodigy_outputs") or []
                if target_values.pop("prodigy_output", None):
                    existing_outputs = []
                    target_values.pop("prodigy_output_sha256", None)
                if not existing_outputs:
                    predictions_to_score = target_values["complex_predictions"]
                else:
                    predictions_to_score = [boltz_prediction]
                generated = []
                for prediction in predictions_to_score:
                    pdb, _, metadata = _prediction_paths(prediction, source_bundle.parent)
                    safe_model = str(metadata.get("model_id") or "model").replace("/", "_")
                    output = (
                        candidate_dir / "prodigy" / target_id
                        / f"{prediction['predictor']}_{safe_model}_seed_{prediction['seed']}.txt"
                    )
                    generated.append(_run_prodigy_for_prediction(
                        executable=args.prodigy,
                        prediction=prediction,
                        source_base=source_bundle.parent,
                        target_chain=target_chain,
                        binder_sequence=candidate.sequence,
                        output_path=output,
                    ))
                target_values["prodigy_outputs"] = existing_outputs + generated

        if add_rosetta:
            target_values.pop("rosetta_output", None)
            target_values.pop("rosetta_output_sha256", None)
            rosetta_outputs = []
            for prediction in target_values["complex_predictions"]:
                pdb, _, metadata = _prediction_paths(prediction, source_bundle.parent)
                binder_chain = str(
                    prediction.get("binder_chain")
                    or metadata.get("binder_chain")
                    or exact_sequence_chain(parse_pdb(pdb), candidate.sequence)
                )
                safe_model = str(metadata.get("model_id") or "model").replace("/", "_")
                rosetta_outputs.append(run_rosetta_interface(
                    executable=args.rosetta_scripts,
                    pyrosetta_python=args.pyrosetta_python,
                    complex_pdb=pdb,
                    target_chain=target_chain,
                    binder_chain=binder_chain,
                    binder_sequence=candidate.sequence,
                    predictor=prediction["predictor"],
                    model_id=str(metadata.get("model_id") or ""),
                    seed=prediction["seed"],
                    output_dir=(
                        candidate_dir / "rosetta_interface" / target_id
                        / f"{prediction['predictor']}_{safe_model}_seed_{prediction['seed']}"
                    ),
                    timeout=args.rosetta_timeout,
                ))
            target_values["rosetta_outputs"] = rosetta_outputs

    # Write-back guard: preflight already validated the source bundle for
    # execution compatibility; re-validate the assembled bundle immediately
    # before it becomes the formal evidence file so enrichment code can never
    # accidentally drop or rewrite the protocol binding.
    # P2-2 provenance: record the actual seeds used so a reader can verify the
    # candidate offset against the protocol seed bases; the protocol binding
    # alone only proves which protocol file was declared, not which seeds ran.
    bundle["enrichment"] = {
        "seed": args.seed,
        "post_relax_seed": args.post_relax_seed,
        "post_relax_repeats": args.post_relax_repeats,
    }
    try:
        validate_bundle_protocol(bundle)
    except ProtocolError as exc:
        raise ContractError("bundle_protocol_mismatch", str(exc)) from exc
    output_bundle = candidate_dir / "artifacts.json"
    atomic_json(output_bundle, bundle)
    validated = load_artifact_bundle(
        output_bundle,
        candidate_id=candidate.candidate_id,
        sequence=candidate.sequence,
        required_targets=required_targets,
    )
    return {
        "status": "complete",
        "candidate_id": candidate.candidate_id,
        "source_bundle": str(source_bundle),
        "output_bundle": str(output_bundle),
        "output_bundle_sha256": validated.sha256,
        "artifact_digest": validated.digest,
        "formal_state_mutated": False,
        "formal_candidate_index_mutated": False,
        "boltz_configured": add_boltz,
        "boltz_seed": args.seed if add_boltz else None,
        "rosetta_configured": add_rosetta,
        "post_relax_configured": add_post_relax,
        "post_relax_seed": args.post_relax_seed if add_post_relax else None,
        "post_relax_repeats": args.post_relax_repeats if add_post_relax else None,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-bundle", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--boltz")
    parser.add_argument("--boltz-cache")
    parser.add_argument("--boltz-checkpoint")
    parser.add_argument("--prodigy")
    parser.add_argument("--rosetta-scripts")
    parser.add_argument("--pyrosetta-python")
    parser.add_argument(
        "--post-relax-python",
        help="Pinned PyRosetta Python used only for cyclic monomer post-relax",
    )
    parser.add_argument(
        "--seed", type=int, default=PREDICTION_PROTOCOL["parameters"]["enrichment"]["seed_base"]
    )
    parser.add_argument(
        "--post-relax-seed",
        type=int,
        default=PREDICTION_PROTOCOL["parameters"]["enrichment"]["post_relax_seed_base"],
    )
    parser.add_argument(
        "--post-relax-repeats",
        type=int,
        default=PREDICTION_PROTOCOL["parameters"]["enrichment"]["post_relax_repeats"],
    )
    parser.add_argument(
        "--post-relax-coordinate-stdev",
        type=float,
        default=PREDICTION_PROTOCOL["parameters"]["enrichment"]["post_relax_coordinate_stdev"],
        help="cyclic post-relax coordinate constraint stdev; default from "
        "protocols/prediction_v1.json",
    )
    parser.add_argument("--post-relax-timeout", type=int, default=3600)
    parser.add_argument("--binder-chain", default="B")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--rosetta-timeout", type=int, default=1800)
    parser.add_argument("--no-kernels", action="store_true")
    return parser


def main() -> int:
    try:
        summary = run(build_parser().parse_args())
    except Exception as exc:
        print(json.dumps({
            "status": "error",
            "code": getattr(exc, "code", exc.__class__.__name__),
            "message": str(exc),
        }, ensure_ascii=False))
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
