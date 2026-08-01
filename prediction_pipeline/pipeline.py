"""Restartable production orchestration for the seven-layer metric battery."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

import data_layer
from data_layer import CandidateIndex, EvidenceLogger, State, evaluate_battery
from project_config import target_slug

from .adapters import (
    ArtifactBundle,
    load_artifact_bundle,
    parse_metadata,
    parse_target_physics,
)
from .contracts import (
    CandidateInput,
    ContractError,
    PredictionConfig,
    candidate_from_row,
    file_sha256,
    object_sha256,
    validate_project,
)
from .metrics import calculate_ipsae, load_pae, pose_convergence
from .structures import (
    backbone_rmsd,
    canonical_target_residue_numbers,
    exact_sequence_chain,
    infer_chain_by_length,
    interface_hotspot_metrics,
    mean_plddt,
    parse_pdb,
    terminal_bond_distance,
)


PREDICTION_PIPELINE_VERSION = "1.3.0"
RUN_SCHEMA_VERSION = 2
RECORD_SCHEMA_VERSION = 2
LAYER_KEYS = tuple(f"l{number}_pass" for number in range(1, 8))
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _artifact_inventory(bundle: ArtifactBundle | None) -> list[dict]:
    if bundle is None:
        return []
    inventory = [{
        "role": "artifact_bundle",
        "path": str(bundle.path),
        "sha256": bundle.sha256,
    }]

    def add_entry(role: str, value: dict | None):
        if value:
            inventory.append({
                "role": role,
                "path": str(value["path"]),
                "sha256": value["sha256"],
            })

    for index, entry in enumerate(bundle.global_artifacts["monomer_predictions"]):
        for key in ("pdb", "pae", "metadata"):
            add_entry(f"global.monomer[{index}].{key}", entry.get(key))
    for key in ("post_relax_pdb", "post_relax_metadata", "design_reference_pdb"):
        add_entry(f"global.{key}", bundle.global_artifacts.get(key))
    for target_id, values in bundle.target_artifacts.items():
        for index, entry in enumerate(values["complex_predictions"]):
            for key in ("pdb", "pae", "metadata"):
                add_entry(f"{target_id}.complex[{index}].{key}", entry.get(key))
        for key in ("prodigy_output", "rosetta_output"):
            add_entry(f"{target_id}.{key}", values.get(key))
        for index, entry in enumerate(values.get("prodigy_outputs", [])):
            add_entry(f"{target_id}.prodigy[{index}]", entry.get("output"))
    return inventory


class PredictionPipeline:
    """Orchestrate artifact validation, metrics, battery evaluation, and handoff."""

    def __init__(
        self,
        *,
        candidate_rows: list[dict],
        project: dict,
        thresholds: dict,
        artifacts_root: str | Path,
        run_root: str | Path,
        config: PredictionConfig | None = None,
        candidate_ids: list[str] | None = None,
        run_id: str | None = None,
        resume: bool = False,
    ):
        self.config = config or PredictionConfig()
        self.project = project
        self.thresholds = dict(thresholds or {})
        self.required_targets = validate_project(project)
        self.artifacts_root = Path(artifacts_root).expanduser().resolve()
        self.run_root = Path(run_root).expanduser().resolve()
        self.resume = bool(resume)
        self.requested_ids = {
            value.strip() for value in (candidate_ids or []) if value.strip()
        }
        self.rows = [
            row for row in candidate_rows
            if not self.requested_ids
            or str(row.get("candidate_id") or "").strip() in self.requested_ids
        ]
        found = {str(row.get("candidate_id") or "").strip() for row in self.rows}
        missing = sorted(self.requested_ids - found)
        if missing:
            raise ContractError(
                "candidate_not_found", f"CandidateIndex does not contain: {missing}"
            )
        if not self.rows:
            raise ContractError("no_candidates", "Prediction received no Design candidates")
        ids = [str(row.get("candidate_id") or "").strip() for row in self.rows]
        duplicates = sorted(value for value, count in Counter(ids).items() if count > 1)
        if duplicates:
            raise ContractError(
                "duplicate_candidate", f"duplicate CandidateIndex rows: {duplicates}"
            )

        self.project_digest = object_sha256(project)
        self.thresholds_digest = object_sha256(self.thresholds)
        self.config_digest = object_sha256({
            "pipeline_version": PREDICTION_PIPELINE_VERSION,
            "method_config": self.config.to_dict(),
        })
        batch_identity = {
            "rows": [
                {
                    "candidate_id": row.get("candidate_id"),
                    "sequence": row.get("sequence"),
                    "manifest_path": row.get("manifest_path"),
                }
                for row in self.rows
            ],
            "project_digest": self.project_digest,
            "config_digest": self.config_digest,
        }
        self.batch_digest = object_sha256(batch_identity)
        if run_id is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            run_id = f"prediction_{stamp}_{self.batch_digest[:8]}"
        if not SAFE_RUN_ID.fullmatch(run_id):
            raise ContractError("run_id_invalid", f"unsafe run_id: {run_id!r}")
        self.run_id = run_id
        self.run_dir = self.run_root / run_id
        self.records_dir = self.run_dir / "records"
        self.handoff_path = self.run_dir / "prediction_handoff.json"
        self._target_reference_cache: dict[str, tuple[Any, Path, str]] = {}

    def _canonical_target_numbering(
        self,
        target_id: str,
        target_config: dict,
        prediction_structure,
        target_chain: str,
    ) -> tuple[list[int] | None, dict | None]:
        """Return reviewed PDB numbering for a predictor-renumbered target."""
        structure_config = target_config.get("structure") or {}
        raw_path = str(structure_config.get("coordinate_path") or "").strip()
        if not raw_path:
            return None, None
        if target_id not in self._target_reference_cache:
            path = Path(raw_path).expanduser().resolve()
            if not path.is_file():
                raise ContractError(
                    "target_coordinates_missing",
                    f"{target_id} reviewed coordinates missing: {path}",
                )
            observed_sha = file_sha256(path)
            declared_sha = str(
                structure_config.get("coordinate_sha256") or ""
            ).strip().lower()
            if declared_sha and declared_sha != observed_sha:
                raise ContractError(
                    "target_coordinates_hash_mismatch",
                    f"{target_id} reviewed coordinate SHA-256 changed",
                )
            self._target_reference_cache[target_id] = (
                parse_pdb(path), path, observed_sha
            )
        reference, path, observed_sha = self._target_reference_cache[target_id]
        numbers = canonical_target_residue_numbers(
            reference,
            target_chain,
            prediction_structure,
            target_chain,
        )
        return numbers, {
            "mapping": "reviewed_target_sequence_order",
            "reference_artifact": str(path),
            "reference_sha256": observed_sha,
        }

    def _run_manifest(self) -> dict:
        return {
            "schema_version": RUN_SCHEMA_VERSION,
            "pipeline_version": PREDICTION_PIPELINE_VERSION,
            "run_id": self.run_id,
            "batch_digest": self.batch_digest,
            "project_id": self.project.get("project_id"),
            "project_digest": self.project_digest,
            "thresholds_digest": self.thresholds_digest,
            "config": self.config.to_dict(),
            "config_digest": self.config_digest,
            "required_targets": list(self.required_targets),
            "artifacts_root": str(self.artifacts_root),
        }

    def _prepare_run(self) -> None:
        manifest_path = self.run_dir / "run_manifest.json"
        expected = self._run_manifest()
        if self.run_dir.exists():
            if not self.resume:
                raise ContractError(
                    "run_exists",
                    f"run directory exists; pass --resume to reuse it: {self.run_dir}",
                )
            try:
                observed = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError) as exc:
                raise ContractError(
                    "run_manifest_invalid", f"cannot resume invalid run: {self.run_dir}"
                ) from exc
            if observed != expected:
                raise ContractError(
                    "run_identity_mismatch",
                    "resume refused because project/config/candidate identity changed",
                )
        else:
            self.records_dir.mkdir(parents=True, exist_ok=False)
            _atomic_json(manifest_path, expected)
            _atomic_json(self.run_dir / "inputs" / "project.json", self.project)
            _atomic_json(self.run_dir / "inputs" / "thresholds.json", self.thresholds)
            _atomic_json(
                self.run_dir / "inputs" / "candidate_rows.json", self.rows
            )

    def _issue(
        self,
        issues: list[dict],
        code: str,
        message: str,
        *,
        layer: int | None = None,
    ) -> None:
        item = {"code": code, "message": message, "recoverable": True}
        if layer is not None:
            item["layer"] = layer
        issues.append(item)

    def _validate_prediction(
        self, entry: dict, candidate: CandidateInput, *, target_chain: str | None = None
    ) -> dict:
        structure = parse_pdb(entry["pdb"]["path"])
        binder_chain = str(entry.get("binder_chain") or "").strip()
        if binder_chain:
            if structure.sequence(binder_chain) != candidate.sequence:
                raise ContractError(
                    "artifact_sequence_mismatch",
                    f"{entry['pdb']['path']} chain {binder_chain} does not match "
                    f"{candidate.candidate_id}",
                )
        else:
            binder_chain = exact_sequence_chain(structure, candidate.sequence)
        if target_chain and (target_chain == binder_chain or target_chain not in structure.chains):
            raise ContractError(
                "target_chain_mismatch",
                f"target chain {target_chain!r} invalid in {entry['pdb']['path']}",
            )
        metadata = parse_metadata(entry.get("metadata"))
        declared_predictor = str(entry.get("predictor") or "").strip()
        metadata_tool = str(metadata.get("tool") or "").strip()
        if metadata_tool and metadata_tool.casefold() != declared_predictor.casefold():
            raise ContractError(
                "prediction_predictor_mismatch",
                f"artifact declares predictor {declared_predictor!r}, but metadata "
                f"declares tool {metadata_tool!r}: {entry['pdb']['path']}",
            )
        if "seed" in metadata:
            metadata_seed = metadata["seed"]
            if (
                isinstance(metadata_seed, bool)
                or not isinstance(metadata_seed, int)
                or metadata_seed != entry["seed"]
            ):
                raise ContractError(
                    "prediction_seed_mismatch",
                    f"artifact declares seed {entry['seed']!r}, but metadata declares "
                    f"seed {metadata_seed!r}: {entry['pdb']['path']}",
                )
        for key in ("requested_sequence", "observed_sequence"):
            if metadata.get(key) and str(metadata[key]).upper() != candidate.sequence:
                raise ContractError(
                    "prediction_sequence_drift",
                    f"{entry['pdb']['path']} metadata {key}={metadata[key]!r}",
                )
        if metadata.get("binder_chain") and metadata["binder_chain"] != binder_chain:
            raise ContractError(
                "prediction_chain_drift",
                f"metadata/PDB binder chain mismatch in {entry['pdb']['path']}",
            )
        return {
            **entry,
            "structure": structure,
            "binder_chain": binder_chain,
            "metadata_values": metadata,
        }

    @staticmethod
    def _primary(predictions: list[dict]) -> dict:
        return sorted(
            predictions,
            key=lambda item: (
                not bool(item.get("primary")),
                str(item.get("predictor")),
                int(item.get("seed")),
            ),
        )[0]

    def _collect_metrics(
        self,
        candidate: CandidateInput,
        bundle: ArtifactBundle | None,
    ) -> tuple[dict, list[dict], list[dict]]:
        metrics: dict[str, Any] = {"global": {}, "targets": {}}
        issues: list[dict] = []
        provenance: list[dict] = []
        if bundle is None:
            for layer, label in (
                (1, "monomer prediction"), (2, "complex PAE"), (3, "interface physics"),
                (4, "pre/post relax structures"), (5, "complex interface"),
                (6, "multi-seed/multi-predictor ensemble"), (7, "design reference"),
            ):
                self._issue(
                    issues, f"l{layer}_artifacts_missing", f"missing {label}", layer=layer
                )
            return metrics, issues, provenance

        monomer_predictions = [
            self._validate_prediction(entry, candidate)
            for entry in bundle.global_artifacts["monomer_predictions"]
        ]
        primary_monomer = None
        if monomer_predictions:
            primary_monomer = self._primary(monomer_predictions)
            plddt, scale = mean_plddt(
                primary_monomer["structure"], primary_monomer["binder_chain"]
            )
            metrics["global"]["plddt"] = plddt
            provenance.append({
                "metric": "global.plddt",
                "tool": primary_monomer["predictor"],
                "seed": primary_monomer["seed"],
                "artifact": str(primary_monomer["pdb"]["path"]),
                "sha256": primary_monomer["pdb"]["sha256"],
                "source_scale": scale,
            })
        else:
            self._issue(
                issues, "l1_monomer_missing", "no monomer prediction declared", layer=1
            )

        post_relax = bundle.global_artifacts.get("post_relax_pdb")
        post_relax_metadata_entry = bundle.global_artifacts.get("post_relax_metadata")
        if primary_monomer:
            metrics["global"]["nc_distance_pre"] = terminal_bond_distance(
                primary_monomer["structure"], primary_monomer["binder_chain"]
            )
        else:
            self._issue(
                issues, "l4_pre_relax_missing", "pre-relax monomer unavailable", layer=4
            )
        if post_relax:
            structure = parse_pdb(post_relax["path"])
            chain = exact_sequence_chain(structure, candidate.sequence)
            relax_metadata = parse_metadata(post_relax_metadata_entry)
            required_relax_metadata = {
                "tool": relax_metadata.get("tool"),
                "tool_revision": (
                    relax_metadata.get("tool_commit")
                    or relax_metadata.get("tool_version")
                ),
                "protocol": relax_metadata.get("protocol"),
                "input_pdb_sha256": relax_metadata.get("input_pdb_sha256"),
                "output_pdb_sha256": relax_metadata.get("output_pdb_sha256"),
                "sequence": relax_metadata.get("sequence"),
                "cyclization_type": relax_metadata.get("cyclization_type"),
                "bond_topology_applied": relax_metadata.get("bond_topology_applied"),
            }
            missing_relax_metadata = [
                key for key, value in required_relax_metadata.items()
                if value is None or value == ""
            ]
            if not post_relax_metadata_entry or missing_relax_metadata:
                self._issue(
                    issues,
                    "l4_post_relax_provenance_missing",
                    "post-relax metadata is required and lacks "
                    f"{missing_relax_metadata or ['metadata file']}",
                    layer=4,
                )
            else:
                expected_input_sha = (
                    primary_monomer["pdb"]["sha256"] if primary_monomer else None
                )
                if relax_metadata["input_pdb_sha256"] != expected_input_sha:
                    raise ContractError(
                        "post_relax_input_mismatch",
                        "post-relax metadata input hash does not match the primary "
                        "monomer prediction",
                    )
                if relax_metadata["output_pdb_sha256"] != post_relax["sha256"]:
                    raise ContractError(
                        "post_relax_output_mismatch",
                        "post-relax metadata output hash does not match post_relax_pdb",
                    )
                if str(relax_metadata["sequence"]).upper() != candidate.sequence:
                    raise ContractError(
                        "post_relax_sequence_mismatch",
                        "post-relax metadata sequence does not match the candidate",
                    )
                normalized_cyclization = str(
                    relax_metadata["cyclization_type"]
                ).replace("-", "_")
                if normalized_cyclization != candidate.cyclization_type:
                    raise ContractError(
                        "post_relax_cyclization_mismatch",
                        "post-relax metadata cyclization does not match Design",
                    )
                if relax_metadata["bond_topology_applied"] is not True:
                    raise ContractError(
                        "post_relax_topology_missing",
                        "post-relax protocol did not attest that the cyclic bond "
                        "topology was applied",
                    )
                metrics["global"]["nc_distance_post"] = terminal_bond_distance(
                    structure, chain
                )
                provenance.append({
                    "metric": "global.nc_distance_post",
                    "tool": relax_metadata["tool"],
                    "tool_revision": required_relax_metadata["tool_revision"],
                    "protocol": relax_metadata["protocol"],
                    "artifact": str(post_relax["path"]),
                    "sha256": post_relax["sha256"],
                    "metadata_artifact": str(post_relax_metadata_entry["path"]),
                    "metadata_sha256": post_relax_metadata_entry["sha256"],
                    "bond_topology_applied": True,
                })
        else:
            self._issue(
                issues, "l4_post_relax_missing", "post-relax PDB is required", layer=4
            )

        reference_entry = bundle.global_artifacts.get("design_reference_pdb")
        reference_path = reference_entry["path"] if reference_entry else candidate.design_reference_pdb
        reference_sha = (
            reference_entry["sha256"] if reference_entry
            else candidate.design_reference_sha256
        )
        if primary_monomer and reference_path:
            reference = parse_pdb(reference_path)
            reference_chain = infer_chain_by_length(reference, len(candidate.sequence))
            value = backbone_rmsd(
                primary_monomer["structure"],
                primary_monomer["binder_chain"],
                reference,
                reference_chain,
            )
            metrics["global"]["scrmsd"] = value
            provenance.append({
                "metric": "global.scrmsd",
                "tool": "Kabsch_backbone",
                "prediction_artifact": str(primary_monomer["pdb"]["path"]),
                "reference_artifact": str(reference_path),
                "reference_sha256": reference_sha,
            })
        else:
            self._issue(
                issues,
                "l7_reference_missing",
                "monomer prediction or Design reference backbone unavailable",
                layer=7,
            )

        project_targets = {target["id"]: target for target in self.project["targets"]}
        for target_id in self.required_targets:
            target_config = project_targets[target_id]
            values = bundle.target_artifacts[target_id]
            target_metrics: dict[str, Any] = {}
            metrics["targets"][target_id] = target_metrics
            configured_chain = str(
                values.get("target_chain")
                or (target_config.get("structure") or {}).get("chain")
                or ""
            ).strip()
            if not configured_chain:
                raise ContractError(
                    "target_chain_missing", f"{target_id} has no reviewed target chain"
                )
            predictions = [
                self._validate_prediction(entry, candidate, target_chain=configured_chain)
                for entry in values["complex_predictions"]
            ]
            ipsae_samples = []
            for prediction in predictions:
                pae_entry = prediction.get("pae")
                if not pae_entry:
                    raise ContractError(
                        "complex_pae_missing",
                        f"declared complex prediction lacks PAE: {prediction['pdb']['path']}",
                    )
                pae = load_pae(pae_entry["path"])
                labels = [
                    residue.chain for residue in prediction["structure"].residues
                ]
                result = calculate_ipsae(
                    pae,
                    labels,
                    configured_chain,
                    prediction["binder_chain"],
                    self.config.ipsae_pae_cutoff,
                )
                ipsae_samples.append({
                    "value": result["ipsae"],
                    "predictor": prediction["predictor"],
                    "seed": prediction["seed"],
                    "details": result,
                    "pdb": str(prediction["pdb"]["path"]),
                    "pae": str(pae_entry["path"]),
                })
            if ipsae_samples:
                target_metrics["ipsae"] = float(np.median(
                    [sample["value"] for sample in ipsae_samples]
                ))
                target_metrics["ipae"] = float(np.median([
                    sample["details"]["interchain_pae_median"]
                    for sample in ipsae_samples
                ]))
                iptm_values = []
                for prediction in predictions:
                    raw_iptm = prediction["metadata_values"].get("iptm")
                    if raw_iptm is None:
                        continue
                    try:
                        iptm = float(raw_iptm)
                    except (TypeError, ValueError) as exc:
                        raise ContractError(
                            "prediction_metadata_value_invalid",
                            f"invalid ipTM {raw_iptm!r} in "
                            f"{prediction['pdb']['path']}",
                        ) from exc
                    if not np.isfinite(iptm) or not 0.0 <= iptm <= 1.0:
                        raise ContractError(
                            "prediction_metadata_value_invalid",
                            f"ipTM must be finite and within [0, 1], got "
                            f"{raw_iptm!r} in {prediction['pdb']['path']}",
                        )
                    iptm_values.append(iptm)
                if iptm_values:
                    target_metrics["iptm"] = float(np.median(iptm_values))
                provenance.append({
                    "metric": f"targets.{target_id}.ipsae",
                    "tool": "DunbrackLab_IPSAE_v4_compatible",
                    "aggregation": "median_across_declared_predictions",
                    "pae_cutoff_angstrom": self.config.ipsae_pae_cutoff,
                    "samples": ipsae_samples,
                })
            else:
                self._issue(
                    issues, "l2_complex_missing",
                    f"{target_id} has no complex predictions", layer=2,
                )

            if predictions:
                hotspots = (target_config.get("binding_site") or {}).get("residues") or []
                interface_samples = []
                for prediction in predictions:
                    canonical_numbers, numbering_provenance = (
                        self._canonical_target_numbering(
                            target_id,
                            target_config,
                            prediction["structure"],
                            configured_chain,
                        )
                    )
                    interface = interface_hotspot_metrics(
                        prediction["structure"],
                        configured_chain,
                        prediction["binder_chain"],
                        hotspots,
                        self.config.interface_distance_angstrom,
                        target_residue_numbers=canonical_numbers,
                    )
                    interface_samples.append({
                        "predictor": prediction["predictor"],
                        "seed": prediction["seed"],
                        "artifact": str(prediction["pdb"]["path"]),
                        "sha256": prediction["pdb"]["sha256"],
                        "details": interface,
                        "target_numbering": numbering_provenance,
                    })
                hotspot_cov = float(np.median([
                    sample["details"]["hotspot_cov"]
                    for sample in interface_samples
                ]))
                site_fraction = float(np.mean([
                    bool(sample["details"]["site_consistency"])
                    for sample in interface_samples
                ]))
                target_metrics.update({
                    "hotspot_cov": hotspot_cov,
                    "site_consistency": site_fraction > 0.5,
                    "site_consistency_fraction": site_fraction,
                })
                provenance.append({
                    "metric": f"targets.{target_id}.hotspot_cov",
                    "tool": "heavy_atom_contact",
                    "aggregation": "median_hotspot_and_strict_majority_site",
                    "details": {
                        "hotspot_cov": hotspot_cov,
                        "site_consistency": site_fraction > 0.5,
                        "site_consistency_fraction": site_fraction,
                    },
                    "samples": interface_samples,
                })
            else:
                self._issue(
                    issues, "l5_complex_missing",
                    f"{target_id} has no complex structure", layer=5,
                )

            physics, physics_provenance = parse_target_physics(values)
            target_metrics.update(physics)
            provenance.extend(
                {"metric_target": target_id, **item} for item in physics_provenance
            )
            missing_physics = [
                key for key in ("dg", "sc", "dsasa") if key not in target_metrics
            ]
            if missing_physics:
                self._issue(
                    issues,
                    "l3_physics_missing",
                    f"{target_id} lacks {missing_physics}",
                    layer=3,
                )

            try:
                convergence = pose_convergence(
                    predictions,
                    configured_chain,
                    candidate.sequence,
                    self.config.seed_cluster_rmsd_angstrom,
                    self.config.minimum_predictions_per_target,
                    self.config.minimum_predictors_per_target,
                )
            except ContractError as exc:
                if exc.code.startswith("l6_"):
                    self._issue(issues, exc.code, f"{target_id}: {exc}", layer=6)
                else:
                    raise
            else:
                target_metrics.update({
                    "pose_rmsd": convergence["pose_rmsd"],
                    "seed_convergence": convergence["seed_convergence"],
                })
                provenance.append({
                    "metric": f"targets.{target_id}.pose_convergence",
                    "tool": "target_aligned_Kabsch",
                    "details": convergence,
                })
        return metrics, issues, provenance

    def _status_from_battery(self, battery: dict) -> str:
        if battery["competition_clearance"]:
            return "finalized"
        if battery["metric_clearance"]:
            return "awaiting_threshold_calibration"
        if battery["triage_status"] == "invalid":
            return "invalid"
        if battery["missing_evidence"] or battery["missing_thresholds"]:
            return "prediction_pending"
        return "needs_optimization"

    def _record_path(self, candidate_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", candidate_id) or "invalid"
        return self.records_dir / f"{safe}.json"

    def _cached_record(
        self, path: Path, *, input_digest: str, artifact_digest: str
    ) -> dict | None:
        if not path.is_file():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        cache = record.get("cache_key") or {}
        expected = {
            "input_digest": input_digest,
            "artifact_digest": artifact_digest,
            "config_digest": self.config_digest,
            "thresholds_digest": self.thresholds_digest,
        }
        if cache != expected:
            return None
        if (
            record.get("schema_version") != RECORD_SCHEMA_VERSION
            or record.get("run_id") != self.run_id
            or not isinstance(record.get("metrics"), dict)
            or not isinstance(record.get("battery"), dict)
            or not isinstance(record.get("issues"), list)
            or record.get("status") not in {
                "finalized", "awaiting_threshold_calibration",
                "prediction_pending", "needs_optimization", "invalid",
            }
        ):
            return None
        record["record_sha256"] = file_sha256(path)
        return record

    def _replace_prediction_metrics(
        self, row: dict, metrics: dict, prediction_meta: dict
    ) -> dict:
        """Replace fields owned by Prediction while preserving other agents' metrics."""
        try:
            existing = json.loads(row.get("metrics_json") or "{}")
        except json.JSONDecodeError:
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        global_owned = {"plddt", "nc_distance_pre", "nc_distance_post", "scrmsd"}
        target_owned = {
            "ipsae", "ipae", "iptm", "dg", "dg_method", "sc", "dsasa",
            "rosetta_dg_separated", "hotspot_cov", "site_consistency",
            "site_consistency_fraction",
            "pose_rmsd", "seed_convergence",
        }
        existing_global = existing.get("global")
        if not isinstance(existing_global, dict):
            existing_global = {}
        for key in global_owned:
            existing_global.pop(key, None)
        existing_global.update(metrics.get("global", {}))
        existing["global"] = existing_global

        existing_targets = existing.get("targets")
        if not isinstance(existing_targets, dict):
            existing_targets = {}
        for target_id in self.required_targets:
            matching_keys = [
                key for key in existing_targets
                if str(key).casefold() == target_id.casefold()
            ]
            target_values = {}
            for key in matching_keys:
                values = existing_targets.pop(key)
                if isinstance(values, dict):
                    target_values.update(values)
            if not isinstance(target_values, dict):
                target_values = {}
            for key in target_owned:
                target_values.pop(key, None)
            target_values.update((metrics.get("targets") or {}).get(target_id, {}))
            if target_values:
                existing_targets[target_id] = target_values
            else:
                existing_targets.pop(target_id, None)
        existing["targets"] = existing_targets
        existing["prediction"] = prediction_meta
        return existing

    def _writeback(self, candidate: CandidateInput, record: dict, row: dict) -> None:
        battery = record["battery"]
        metrics = record["metrics"]
        prediction_meta = {
            "schema_version": RECORD_SCHEMA_VERSION,
            "pipeline_version": PREDICTION_PIPELINE_VERSION,
            "run_id": self.run_id,
            "record_path": str(self._record_path(candidate.candidate_id)),
            "record_sha256": record["record_sha256"],
            "input_digest": candidate.input_digest,
            "artifact_digest": record["cache_key"]["artifact_digest"],
            "evidence_status": record["status"],
            "issues": record["issues"],
        }
        nested = self._replace_prediction_metrics(row, metrics, prediction_meta)
        scores: dict[str, Any] = {
            "metrics_json": json.dumps(
                nested, ensure_ascii=False, separators=(",", ":")
            ),
            **{key: battery[key] for key in LAYER_KEYS},
            "all_layers_pass": battery["all_layers_pass"],
            "metric_clearance": battery["metric_clearance"],
            "competition_clearance": battery["competition_clearance"],
            "triage_status": battery["triage_status"],
            "threshold_audit": battery["threshold_audit"],
        }
        global_metrics = metrics.get("global", {})
        for key in ("plddt", "nc_distance_pre", "nc_distance_post", "scrmsd"):
            scores[key] = global_metrics.get(key, "")
        scores["ring_closure_pre"] = (
            self._distance_pass(global_metrics["nc_distance_pre"])
            if "nc_distance_pre" in global_metrics else ""
        )
        scores["ring_closure_post"] = (
            self._distance_pass(global_metrics["nc_distance_post"])
            if "nc_distance_post" in global_metrics else ""
        )

        methods = {
            values.get("dg_method")
            for values in metrics.get("targets", {}).values()
            if values.get("dg_method")
        }
        scores["dg_method"] = methods.pop() if len(methods) == 1 else ""
        # Clear retired single-target/placeholder display fields. Authoritative
        # per-target values live in metrics_json and the target-suffixed columns.
        for column in (
            "site_consistency", "pose_rmsd", "seed_convergence",
        ):
            scores[column] = ""
        for target_id, values in metrics.get("targets", {}).items():
            slug = target_slug(target_id)
            legacy_iptm_column = f"colab_iptm_{slug}"
            if legacy_iptm_column in data_layer.INDEX_COLUMNS:
                scores[legacy_iptm_column] = ""
            for key in (
                "ipsae", "ipae", "iptm", "dg", "sc", "dsasa",
                "hotspot_cov", "site_consistency", "pose_rmsd", "seed_convergence",
            ):
                column = f"{key}_{slug}"
                if column in data_layer.INDEX_COLUMNS:
                    scores[column] = values.get(key, "")
        # Also clear legacy display columns when a required target currently has
        # no target metrics at all.
        for target_id in self.required_targets:
            if target_id in metrics.get("targets", {}):
                continue
            slug = target_slug(target_id)
            legacy_iptm_column = f"colab_iptm_{slug}"
            if legacy_iptm_column in data_layer.INDEX_COLUMNS:
                scores[legacy_iptm_column] = ""
            for key in (
                "ipsae", "ipae", "iptm", "dg", "sc", "dsasa",
                "hotspot_cov", "site_consistency", "pose_rmsd", "seed_convergence",
            ):
                column = f"{key}_{slug}"
                if column in data_layer.INDEX_COLUMNS:
                    scores[column] = ""
        CandidateIndex.update_score(candidate.candidate_id, scores)
        old_notes = str(row.get("notes") or "").strip()
        prediction_note = (
            f"prediction_run={self.run_id}; status={record['status']}; "
            f"record={self._record_path(candidate.candidate_id)}"
        )
        combined_notes = (
            old_notes if prediction_note in old_notes
            else f"{old_notes}; {prediction_note}".strip("; ")
        )
        CandidateIndex.update_status(
            candidate.candidate_id,
            record["status"],
            notes=combined_notes,
        )

    def _writeback_invalid(self, row: dict, record: dict) -> None:
        """Withdraw all Prediction-owned values after a contract failure."""
        candidate_id = record["candidate"]["candidate_id"]
        prediction_meta = {
            "schema_version": RECORD_SCHEMA_VERSION,
            "pipeline_version": PREDICTION_PIPELINE_VERSION,
            "run_id": self.run_id,
            "record_path": str(self._record_path(candidate_id)),
            "record_sha256": record["record_sha256"],
            "input_digest": record["cache_key"]["input_digest"],
            "artifact_digest": record["cache_key"]["artifact_digest"],
            "evidence_status": "invalid",
            "issues": record["issues"],
        }
        nested = self._replace_prediction_metrics(
            row, {"global": {}, "targets": {}}, prediction_meta
        )
        scores: dict[str, Any] = {
            "metrics_json": json.dumps(
                nested, ensure_ascii=False, separators=(",", ":")
            ),
            **{key: "" for key in LAYER_KEYS},
            "all_layers_pass": False,
            "metric_clearance": False,
            "competition_clearance": False,
            "triage_status": "invalid",
            "threshold_audit": {},
            "plddt": "",
            "nc_distance_pre": "",
            "nc_distance_post": "",
            "ring_closure_pre": "",
            "ring_closure_post": "",
            "scrmsd": "",
            "dg_method": "",
            "site_consistency": "",
            "pose_rmsd": "",
            "seed_convergence": "",
        }
        for target_id in self.required_targets:
            slug = target_slug(target_id)
            for key in (
                "ipsae", "ipae", "iptm", "colab_iptm", "dg", "sc", "dsasa",
                "hotspot_cov", "site_consistency", "pose_rmsd",
                "seed_convergence",
            ):
                column = f"{key}_{slug}"
                if column in data_layer.INDEX_COLUMNS:
                    scores[column] = ""
        CandidateIndex.update_score(candidate_id, scores)

        old_notes = str(row.get("notes") or "").strip()
        error_code = record["issues"][0]["code"]
        prediction_note = (
            f"prediction_run={self.run_id}; status=invalid; "
            f"error={error_code}; record={self._record_path(candidate_id)}"
        )
        combined_notes = (
            old_notes if prediction_note in old_notes
            else f"{old_notes}; {prediction_note}".strip("; ")
        )
        CandidateIndex.update_status(candidate_id, "invalid", notes=combined_notes)

    def _distance_pass(self, value: float) -> str:
        threshold = self.thresholds.get("L4_nc_term_dist") or {}
        if threshold.get("value") is None:
            return ""
        expected = float(threshold["value"])
        observed = float(value)
        comparisons = {
            "<": observed < expected,
            "<=": observed <= expected,
            ">": observed > expected,
            ">=": observed >= expected,
        }
        return str(bool(comparisons.get(threshold.get("operator", "<"), False)))

    def _process_valid_candidate(
        self, candidate: CandidateInput, row: dict
    ) -> tuple[dict, bool]:
        # The legacy Design refold is an input-integrity witness only.  It is
        # never reused as Prediction evidence.
        legacy = parse_pdb(candidate.legacy_refold_pdb)
        exact_sequence_chain(legacy, candidate.sequence)

        bundle_path = self.artifacts_root / candidate.candidate_id / "artifacts.json"
        bundle = None
        if bundle_path.is_file():
            bundle = load_artifact_bundle(
                bundle_path,
                candidate_id=candidate.candidate_id,
                sequence=candidate.sequence,
                required_targets=self.required_targets,
            )
        artifact_digest = bundle.digest if bundle else "missing"
        record_path = self._record_path(candidate.candidate_id)
        cached = self._cached_record(
            record_path,
            input_digest=candidate.input_digest,
            artifact_digest=artifact_digest,
        )
        if cached is not None:
            self._writeback(candidate, cached, row)
            return cached, True

        metrics, issues, provenance = self._collect_metrics(candidate, bundle)
        methods = {
            values.get("dg_method")
            for values in metrics["targets"].values()
            if values.get("dg_method")
        }
        if len(methods) > 1:
            raise ContractError(
                "dg_method_inconsistent",
                f"targets use inconsistent dG methods: {sorted(methods)}",
            )
        evaluation_input = {"metrics": metrics}
        if len(methods) == 1:
            evaluation_input["dg_method"] = next(iter(methods))
        battery = evaluate_battery(
            evaluation_input,
            self.thresholds,
            required_targets=self.required_targets,
        )
        status = self._status_from_battery(battery)
        record = {
            "schema_version": RECORD_SCHEMA_VERSION,
            "pipeline_version": PREDICTION_PIPELINE_VERSION,
            "run_id": self.run_id,
            "created_at": _utcnow(),
            "candidate": candidate.snapshot(),
            "cache_key": {
                "input_digest": candidate.input_digest,
                "artifact_digest": artifact_digest,
                "config_digest": self.config_digest,
                "thresholds_digest": self.thresholds_digest,
            },
            "status": status,
            "metrics": metrics,
            "battery": battery,
            "issues": issues,
            "provenance": provenance,
            "artifact_inventory": _artifact_inventory(bundle),
        }
        _atomic_json(record_path, record)
        record["record_sha256"] = file_sha256(record_path)
        self._writeback(candidate, record, row)

        tool_trace = {
            "tool_name": "prediction_pipeline",
            "tool_version": PREDICTION_PIPELINE_VERSION,
            "input_params": {
                "run_id": self.run_id,
                "artifact_digest": artifact_digest,
            },
            "output_path": str(record_path),
            "output_hash": record["record_sha256"],
            "exit_code": 0,
        }
        for layer, pass_key in enumerate(LAYER_KEYS, start=1):
            EvidenceLogger.candidate_scored(
                candidate.candidate_id,
                layer,
                {"metrics": metrics},
                tool_trace,
                bool(battery[pass_key]),
                target_ids=list(self.required_targets),
            )
        EvidenceLogger.log(
            "prediction",
            "prediction_recorded",
            {
                "candidate_id": candidate.candidate_id,
                "prediction_status": status,
                "record_path": str(record_path),
                "record_sha256": record["record_sha256"],
                "issues": issues,
            },
            targets=list(self.required_targets),
            phase="evaluate",
        )
        if status == "finalized":
            EvidenceLogger.log(
                "prediction",
                "candidate_finalized",
                {
                    "candidate_id": candidate.candidate_id,
                    "record_path": str(record_path),
                    "record_sha256": record["record_sha256"],
                },
                targets=list(self.required_targets),
                phase="evaluate",
            )
        return record, False

    def _invalid_record(self, row: dict, error: ContractError) -> dict:
        candidate_id = str(row.get("candidate_id") or "").strip() or "unknown"
        input_digest = object_sha256(row)
        record_path = self._record_path(candidate_id)
        record = {
            "schema_version": RECORD_SCHEMA_VERSION,
            "pipeline_version": PREDICTION_PIPELINE_VERSION,
            "run_id": self.run_id,
            "created_at": _utcnow(),
            "candidate": {
                "candidate_id": candidate_id,
                "sequence": row.get("sequence"),
                "manifest_path": row.get("manifest_path"),
                "input_digest": input_digest,
            },
            "cache_key": {
                "input_digest": input_digest,
                "artifact_digest": "contract_invalid",
                "config_digest": self.config_digest,
                "thresholds_digest": self.thresholds_digest,
            },
            "status": "invalid",
            "metrics": {"global": {}, "targets": {}},
            "battery": None,
            "issues": [{
                "code": error.code,
                "message": str(error),
                "recoverable": False,
            }],
            "provenance": [],
            "artifact_inventory": [],
        }
        _atomic_json(record_path, record)
        record["record_sha256"] = file_sha256(record_path)
        if candidate_id != "unknown":
            try:
                self._writeback_invalid(row, record)
            except KeyError:
                pass
        EvidenceLogger.log(
            "prediction",
            "prediction_recorded",
            {
                "candidate_id": candidate_id,
                "prediction_status": "invalid",
                "record_path": str(record_path),
                "record_sha256": record["record_sha256"],
                "issues": record["issues"],
            },
            targets=list(self.required_targets),
            phase="evaluate",
        )
        return record

    def run(self) -> dict:
        self._prepare_run()
        EvidenceLogger.log(
            "prediction",
            "prediction_run_started",
            {
                "run_id": self.run_id,
                "pipeline_version": PREDICTION_PIPELINE_VERSION,
                "run_dir": str(self.run_dir),
                "candidate_count": len(self.rows),
                "config_digest": self.config_digest,
            },
            targets=list(self.required_targets),
            phase="evaluate",
        )
        records, cache_hits = [], 0
        for row in self.rows:
            try:
                candidate = candidate_from_row(row)
                record, cached = self._process_valid_candidate(candidate, row)
                cache_hits += int(cached)
            except ContractError as exc:
                record = self._invalid_record(row, exc)
            records.append(record)

        categories: dict[str, list[dict]] = {}
        for record in records:
            categories.setdefault(record["status"], []).append({
                "candidate_id": record["candidate"]["candidate_id"],
                "sequence": record["candidate"].get("sequence"),
                "record_path": str(
                    self._record_path(record["candidate"]["candidate_id"])
                ),
                "record_sha256": record.get("record_sha256"),
                "issues": record.get("issues", []),
            })
        handoff = {
            "schema_version": RUN_SCHEMA_VERSION,
            "pipeline_version": PREDICTION_PIPELINE_VERSION,
            "run_id": self.run_id,
            "created_at": _utcnow(),
            "project_id": self.project.get("project_id"),
            "required_targets": list(self.required_targets),
            "selection_semantics": {
                "finalized": "competition_clearance=true",
                "awaiting_threshold_calibration": (
                    "all metrics pass, but one or more threshold evidence entries "
                    "are not justified"
                ),
                "prediction_pending": "required evidence or threshold value is missing",
                "needs_optimization": "complete evidence fails at least one metric gate",
                "invalid": "input, provenance, sequence, hash, or hard geometry failed",
            },
            "categories": categories,
            "downstream": {
                "critic_input_statuses": [
                    "finalized", "awaiting_threshold_calibration", "needs_optimization"
                ],
                "planner_feedback_statuses": [
                    "prediction_pending", "needs_optimization", "invalid"
                ],
                "authoritative_record_field": "record_path",
                "candidate_index_is_summary": True,
            },
        }
        _atomic_json(self.handoff_path, handoff)
        summary = {
            "run_id": self.run_id,
            "pipeline_version": PREDICTION_PIPELINE_VERSION,
            "run_dir": str(self.run_dir),
            "handoff_path": str(self.handoff_path),
            "evaluated": len(records),
            "cache_hits": cache_hits,
            "status_counts": dict(sorted(Counter(
                record["status"] for record in records
            ).items())),
            "finalized": sorted(
                record["candidate"]["candidate_id"]
                for record in records if record["status"] == "finalized"
            ),
        }
        updated_state = State.update({
            "phase": "evaluate",
            "prediction": summary,
        })
        if not any(
            entry.get("agent") == "prediction"
            and (entry.get("summary") or {}).get("run_id") == self.run_id
            for entry in updated_state.get("iteration_history", [])
        ):
            State.append_history({
                "phase": "evaluate",
                "agent": "prediction",
                "timestamp": _utcnow(),
                "summary": summary,
            })
        EvidenceLogger.log(
            "prediction",
            "prediction_handoff_ready",
            {
                "run_id": self.run_id,
                "handoff_path": str(self.handoff_path),
                "handoff_sha256": file_sha256(self.handoff_path),
                "status_counts": summary["status_counts"],
            },
            targets=list(self.required_targets),
            phase="evaluate",
        )
        return summary
