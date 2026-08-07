"""Metric collection mixin for PredictionPipeline.

Split from prediction_pipeline/pipeline.py (PR8) so the pipeline module stays
under the architecture-gate file-size limit. The mixin only depends on
instance state set by ``PredictionPipeline.__init__`` plus the module-level
imports below; ``PredictionPipeline`` inherits from it unchanged.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .adapters import ArtifactBundle, parse_metadata, parse_target_physics
from .contracts import CandidateInput, ContractError
from .metrics import calculate_ipsae, load_pae, pose_convergence
from .relax_worker import POST_RELAX_PROTOCOL, POST_RELAX_TOOL
from .rosetta_worker import PYROSETTA_VERSION
from .structures import (
    backbone_rmsd,
    exact_sequence_chain,
    infer_chain_by_length,
    interface_hotspot_metrics,
    mean_plddt,
    parse_pdb,
    terminal_bond_distance,
)


class MetricCollectorsMixin:
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
        primary_monomer = self._collect_l1_monomer(
            candidate, bundle, metrics, issues, provenance
        )
        self._collect_l4_post_relax(
            candidate, bundle, metrics, issues, provenance, primary_monomer
        )
        self._collect_l7_reference(
            candidate, bundle, metrics, issues, provenance, primary_monomer
        )
        self._collect_target_metrics(candidate, bundle, metrics, issues, provenance)
        return metrics, issues, provenance


    def _collect_l1_monomer(
        self,
        candidate: CandidateInput,
        bundle: ArtifactBundle,
        metrics: dict[str, Any],
        issues: list[dict],
        provenance: list[dict],
    ) -> dict | None:
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
        return primary_monomer


    def _collect_l4_post_relax(
        self,
        candidate: CandidateInput,
        bundle: ArtifactBundle,
        metrics: dict[str, Any],
        issues: list[dict],
        provenance: list[dict],
        primary_monomer: dict | None,
    ) -> None:
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
            self._collect_l4_post_relax_evidence(
                candidate, metrics, issues, provenance,
                primary_monomer, post_relax, post_relax_metadata_entry,
            )
        else:
            self._issue(
                issues, "l4_post_relax_missing", "post-relax PDB is required", layer=4
            )


    def _collect_l4_post_relax_evidence(
        self,
        candidate: CandidateInput,
        metrics: dict[str, Any],
        issues: list[dict],
        provenance: list[dict],
        primary_monomer: dict | None,
        post_relax: dict,
        post_relax_metadata_entry: dict | None,
    ) -> None:
        extracted = self._extract_relax_metadata(
            candidate, post_relax, post_relax_metadata_entry
        )
        if not post_relax_metadata_entry or extracted["missing_relax_metadata"]:
            self._issue(
                issues,
                "l4_post_relax_provenance_missing",
                "post-relax metadata is required and lacks "
                f"{extracted['missing_relax_metadata'] or ['metadata file']}",
                layer=4,
            )
            return
        numeric_metadata = self._validate_relax_evidence_consistency(
            candidate, post_relax, primary_monomer, extracted
        )
        self._record_l4_metrics(
            metrics, provenance, candidate, post_relax, post_relax_metadata_entry,
            primary_monomer, extracted, numeric_metadata,
        )


    def _extract_relax_metadata(
        self,
        candidate: CandidateInput,
        post_relax: dict,
        post_relax_metadata_entry: dict | None,
    ) -> dict:
        structure = parse_pdb(post_relax["path"])
        chain = exact_sequence_chain(structure, candidate.sequence)
        relax_metadata = parse_metadata(post_relax_metadata_entry)
        coordinate_constraints = relax_metadata.get("coordinate_constraints")
        if not isinstance(coordinate_constraints, dict):
            coordinate_constraints = {}
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
            "topology_geometry_constraints_applied": relax_metadata.get(
                "topology_geometry_constraints_applied"
            ),
            "input_chain": relax_metadata.get("input_chain"),
            "output_chain": relax_metadata.get("output_chain"),
            "seed": relax_metadata.get("seed"),
            "repeats": relax_metadata.get("repeats"),
            "pre_distance": relax_metadata.get(
                "terminal_c_to_n_distance_pre_angstrom"
            ),
            "post_distance": relax_metadata.get(
                "terminal_c_to_n_distance_post_angstrom"
            ),
            "backbone_rmsd": relax_metadata.get(
                "backbone_rmsd_to_input_angstrom"
            ),
            "pre_score": relax_metadata.get("pre_total_score_ref2015"),
            "post_score": relax_metadata.get("post_total_score_ref2015"),
            "coordinate_constraints_enabled": coordinate_constraints.get("enabled"),
            "coordinate_constraints_to_start": coordinate_constraints.get(
                "to_start_coordinates"
            ),
            "coordinate_constraints_sidechains": coordinate_constraints.get(
                "sidechains"
            ),
            "coordinate_constraints_ramp_down": coordinate_constraints.get(
                "ramp_down"
            ),
            "coordinate_constraints_stdev": coordinate_constraints.get(
                "stdev_angstrom"
            ),
            "design_enabled": relax_metadata.get("design_enabled"),
        }
        missing_relax_metadata = [
            key for key, value in required_relax_metadata.items()
            if value is None or value == ""
        ]
        return {
            "structure": structure,
            "chain": chain,
            "relax_metadata": relax_metadata,
            "coordinate_constraints": coordinate_constraints,
            "required_relax_metadata": required_relax_metadata,
            "missing_relax_metadata": missing_relax_metadata,
        }


    def _validate_relax_evidence_consistency(
        self,
        candidate: CandidateInput,
        post_relax: dict,
        primary_monomer: dict | None,
        extracted: dict,
    ) -> dict:
        relax_metadata = extracted["relax_metadata"]
        coordinate_constraints = extracted["coordinate_constraints"]
        required_relax_metadata = extracted["required_relax_metadata"]
        chain = extracted["chain"]
        self._validate_relax_provenance(
            relax_metadata,
            post_relax,
            primary_monomer,
            candidate,
            chain,
            required_relax_metadata,
        )
        self._validate_relax_runtime_parameters(relax_metadata, coordinate_constraints)
        return _parse_numeric_relax_metadata(relax_metadata, coordinate_constraints)

    def _validate_relax_provenance(
        self,
        relax_metadata: dict,
        post_relax: dict,
        primary_monomer: dict | None,
        candidate: CandidateInput,
        chain: str,
        required_relax_metadata: dict,
    ) -> None:
        """校验 post-relax 的工具、版本与输入输出指纹与候选一致。"""
        expected_input_sha = (
            primary_monomer["pdb"]["sha256"] if primary_monomer else None
        )
        input_chain = primary_monomer["binder_chain"] if primary_monomer else None
        if relax_metadata["tool"] != POST_RELAX_TOOL:
            raise ContractError(
                "post_relax_tool_mismatch",
                f"L4 requires {POST_RELAX_TOOL}; found {relax_metadata['tool']!r}",
            )
        if required_relax_metadata["tool_revision"] != PYROSETTA_VERSION:
            raise ContractError(
                "post_relax_version_mismatch",
                f"L4 requires PyRosetta {PYROSETTA_VERSION}",
            )
        if relax_metadata["protocol"] != POST_RELAX_PROTOCOL:
            raise ContractError(
                "post_relax_protocol_mismatch",
                f"L4 requires protocol {POST_RELAX_PROTOCOL}",
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
        if relax_metadata["topology_geometry_constraints_applied"] is not True:
            raise ContractError(
                "post_relax_topology_constraints_missing",
                "post-relax did not attest peptide-bond geometry constraints",
            )
        if (
            relax_metadata["input_chain"] != input_chain
            or relax_metadata["output_chain"] != chain
            or chain != input_chain
        ):
            raise ContractError(
                "post_relax_chain_mismatch",
                "post-relax metadata/PDB chain differs from primary monomer",
            )

    def _validate_relax_runtime_parameters(
        self, relax_metadata: dict, coordinate_constraints: dict
    ) -> None:
        """校验 post-relax 运行参数（seed/repeats/约束开关）合法。"""
        seed = relax_metadata["seed"]
        repeats = relax_metadata["repeats"]
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ContractError(
                "post_relax_seed_invalid", "post-relax seed must be an integer"
            )
        if (
            isinstance(repeats, bool)
            or not isinstance(repeats, int)
            or repeats < 1
        ):
            raise ContractError(
                "post_relax_repeats_invalid",
                "post-relax repeats must be a positive integer",
            )
        if (
            coordinate_constraints.get("enabled") is not True
            or coordinate_constraints.get("to_start_coordinates") is not True
            or coordinate_constraints.get("sidechains") is not False
            or coordinate_constraints.get("ramp_down") is not False
            or relax_metadata.get("design_enabled") is not False
        ):
            raise ContractError(
                "post_relax_constraint_invalid",
                "L4 requires fixed start-coordinate constraints and design disabled",
            )


    def _record_l4_metrics(
        self,
        metrics: dict[str, Any],
        provenance: list[dict],
        candidate: CandidateInput,
        post_relax: dict,
        post_relax_metadata_entry: dict | None,
        primary_monomer: dict | None,
        extracted: dict,
        numeric_metadata: dict,
    ) -> None:
        relax_metadata = extracted["relax_metadata"]
        coordinate_constraints = extracted["coordinate_constraints"]
        required_relax_metadata = extracted["required_relax_metadata"]
        structure = extracted["structure"]
        chain = extracted["chain"]
        computed_pre = metrics["global"].get("nc_distance_pre")
        computed_post = terminal_bond_distance(structure, chain)
        computed_drift = backbone_rmsd(
            structure,
            chain,
            primary_monomer["structure"],
            primary_monomer["binder_chain"],
        )
        for label, declared, observed in (
            (
                "pre distance",
                numeric_metadata["terminal_c_to_n_distance_pre_angstrom"],
                computed_pre,
            ),
            (
                "post distance",
                numeric_metadata["terminal_c_to_n_distance_post_angstrom"],
                computed_post,
            ),
            (
                "backbone RMSD",
                numeric_metadata["backbone_rmsd_to_input_angstrom"],
                computed_drift,
            ),
        ):
            if observed is None or abs(declared - observed) > 1e-3:
                raise ContractError(
                    "post_relax_geometry_mismatch",
                    f"metadata {label} does not match the bound PDB artifacts",
                )
        metrics["global"].update({
            "nc_distance_post": computed_post,
            "post_relax_backbone_rmsd": computed_drift,
            "post_relax_score_pre": numeric_metadata[
                "pre_total_score_ref2015"
            ],
            "post_relax_score_post": numeric_metadata[
                "post_total_score_ref2015"
            ],
            "post_relax_score_delta": (
                numeric_metadata["post_total_score_ref2015"]
                - numeric_metadata["pre_total_score_ref2015"]
            ),
        })
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
            "backbone_rmsd_to_input_angstrom": computed_drift,
            "pre_total_score_ref2015": numeric_metadata[
                "pre_total_score_ref2015"
            ],
            "post_total_score_ref2015": numeric_metadata[
                "post_total_score_ref2015"
            ],
            "coordinate_constraints": coordinate_constraints,
        })


    def _collect_l7_reference(
        self,
        candidate: CandidateInput,
        bundle: ArtifactBundle,
        metrics: dict[str, Any],
        issues: list[dict],
        provenance: list[dict],
        primary_monomer: dict | None,
    ) -> None:
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


    def _collect_target_metrics(
        self,
        candidate: CandidateInput,
        bundle: ArtifactBundle,
        metrics: dict[str, Any],
        issues: list[dict],
        provenance: list[dict],
    ) -> None:
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
            self._collect_target_ipsae(
                target_id, configured_chain, predictions, target_metrics, issues, provenance
            )
            self._collect_target_hotspots(
                target_id, target_config, configured_chain, predictions,
                target_metrics, issues, provenance,
            )
            self._collect_target_physics(
                target_id, values, target_metrics, issues, provenance
            )
            self._collect_target_convergence(
                target_id, predictions, configured_chain, candidate,
                target_metrics, issues, provenance,
            )


    def _collect_target_ipsae(
        self,
        target_id: str,
        configured_chain: str,
        predictions: list[dict],
        target_metrics: dict[str, Any],
        issues: list[dict],
        provenance: list[dict],
    ) -> None:
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


    def _collect_target_hotspots(
        self,
        target_id: str,
        target_config: dict,
        configured_chain: str,
        predictions: list[dict],
        target_metrics: dict[str, Any],
        issues: list[dict],
        provenance: list[dict],
    ) -> None:
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


    def _collect_target_physics(
        self,
        target_id: str,
        values: dict,
        target_metrics: dict[str, Any],
        issues: list[dict],
        provenance: list[dict],
    ) -> None:
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


    def _collect_target_convergence(
        self,
        target_id: str,
        predictions: list[dict],
        configured_chain: str,
        candidate: CandidateInput,
        target_metrics: dict[str, Any],
        issues: list[dict],
        provenance: list[dict],
    ) -> None:
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


def _parse_numeric_relax_metadata(
    relax_metadata: dict, coordinate_constraints: dict
) -> dict:
    """解析 post-relax 数值字段，校验类型与有限性。"""
    numeric_metadata = {}
    for key in (
        "terminal_c_to_n_distance_pre_angstrom",
        "terminal_c_to_n_distance_post_angstrom",
        "backbone_rmsd_to_input_angstrom",
        "pre_total_score_ref2015",
        "post_total_score_ref2015",
    ):
        try:
            value = float(relax_metadata[key])
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "post_relax_metadata_invalid", f"{key} must be numeric"
            ) from exc
        if not math.isfinite(value):
            raise ContractError(
                "post_relax_metadata_invalid", f"{key} must be finite"
            )
        numeric_metadata[key] = value
    try:
        constraint_stdev = float(coordinate_constraints["stdev_angstrom"])
    except (TypeError, ValueError) as exc:
        raise ContractError(
            "post_relax_constraint_invalid", "constraint stdev must be numeric"
        ) from exc
    if not math.isfinite(constraint_stdev) or constraint_stdev <= 0:
        raise ContractError(
            "post_relax_constraint_invalid", "constraint stdev must be positive"
        )
    return numeric_metadata
