"""Experimental -> predicted structure resolution and quality grading.

The module selects metadata and records provenance. It does not silently claim
that a predicted target is equivalent to an experimental complex, and it does
not run an expensive predictor itself. Local AF3/Boltz adapters can implement
``PredictedStructureProvider`` later without changing the bootstrap contract.
"""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Protocol


class StructureNotReadyError(RuntimeError):
    """Raised when design is requested without a reviewed usable structure."""


class ExperimentalStructureProvider(Protocol):
    def find(self, target: dict) -> list[dict]: ...


class PredictedStructureProvider(Protocol):
    def find(self, target: dict) -> list[dict]: ...


def _read_json(url: str, *, data: dict | None = None, timeout: int = 30):
    encoded = json.dumps(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(
        url, data=encoded,
        headers={"Content-Type": "application/json", "User-Agent": "cycpep-agent/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class RCSBStructureProvider:
    SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
    ENTRY_URL = "https://data.rcsb.org/rest/v1/core/entry/{pdb_id}"

    def __init__(self, *, max_entries: int = 5, timeout: int = 10):
        self.max_entries = max(1, int(max_entries))
        self.timeout = timeout

    def _entry_record(self, pdb_id: str) -> dict | None:
        try:
            entry = _read_json(
                self.ENTRY_URL.format(pdb_id=pdb_id), timeout=self.timeout,
            )
        except Exception:
            return None
        info = entry.get("rcsb_entry_info") or {}
        resolutions = info.get("resolution_combined") or []
        methods = entry.get("exptl") or []
        return {
            "source": "rcsb",
            "kind": "experimental",
            "id": pdb_id,
            "pdb_id": pdb_id,
            "method": (methods[0] if methods else {}).get("method"),
            "resolution": min(resolutions) if resolutions else None,
            "coverage": None,
            "has_bound_partner": info.get("polymer_entity_count_protein", 0) >= 2,
            "url": f"https://www.rcsb.org/structure/{pdb_id}",
            "pdb_url": f"https://files.rcsb.org/download/{pdb_id}.pdb",
        }

    def find(self, target: dict) -> list[dict]:
        uniprot = target.get("uniprot")
        configured = (target.get("structure") or {}).get("pdb_id")
        pdb_ids = []
        if configured:
            pdb_ids.append(str(configured).upper())
        if uniprot:
            query = {
                "query": {"type": "terminal", "service": "text", "parameters": {
                    "attribute": "rcsb_polymer_entity_container_identifiers.reference_sequence_identifiers.database_accession",
                    "operator": "exact_match", "value": uniprot,
                }},
                "return_type": "entry",
                "request_options": {"paginate": {"start": 0, "rows": 25}},
            }
            try:
                result = _read_json(self.SEARCH_URL, data=query, timeout=self.timeout)
                pdb_ids.extend(row.get("identifier") for row in result.get("result_set", []))
            except Exception:
                pass

        selected_ids = list(dict.fromkeys(value for value in pdb_ids if value))[:self.max_entries]
        records = []
        with ThreadPoolExecutor(max_workers=min(5, len(selected_ids) or 1)) as pool:
            futures = {pool.submit(self._entry_record, pdb_id): pdb_id for pdb_id in selected_ids}
            for future in as_completed(futures):
                record = future.result()
                if record:
                    records.append(record)
        return records


class AlphaFoldDBProvider:
    API_URL = "https://alphafold.ebi.ac.uk/api/prediction/{uniprot}"

    def find(self, target: dict) -> list[dict]:
        uniprot = target.get("uniprot")
        if not uniprot:
            return []
        try:
            payload = _read_json(self.API_URL.format(uniprot=urllib.parse.quote(uniprot)))
        except Exception:
            return []
        if not isinstance(payload, list):
            return []
        records = []
        for item in payload:
            records.append({
                "source": "alphafold_db",
                "kind": "predicted",
                "id": item.get("entryId") or item.get("modelEntityId") or uniprot,
                "model_version": item.get("latestVersion"),
                "mean_plddt": item.get("globalMetricValue"),
                "epitope_plddt": None,
                "pae_available": bool(item.get("paeDocUrl")),
                "pdb_url": item.get("pdbUrl"),
                "cif_url": item.get("cifUrl"),
                "pae_url": item.get("paeDocUrl"),
                "url": item.get("entryUrl"),
            })
        return records


def grade_structure(record: dict, *, epitope_required: bool = True) -> dict:
    """Grade fitness for design, not merely whether a coordinate file exists."""
    kind = record.get("kind")
    if kind == "experimental":
        resolution = record.get("resolution")
        coverage = record.get("coverage")
        if resolution is not None and float(resolution) <= 2.5 and (coverage is None or coverage >= 0.8):
            grade, reason = "A", "high-resolution experimental structure"
        elif resolution is None or float(resolution) <= 3.5:
            grade, reason = "B", "usable experimental structure; inspect local epitope quality"
        else:
            grade, reason = "C", "low-resolution experimental structure"
    elif kind == "predicted":
        mean_plddt = record.get("mean_plddt")
        epitope_plddt = record.get("epitope_plddt")
        local = epitope_plddt if epitope_plddt is not None else mean_plddt
        if local is not None and float(local) >= 80 and record.get("pae_available"):
            grade, reason = "B", "high-confidence predicted coordinates; binding mode remains unvalidated"
        elif local is not None and float(local) >= 70:
            grade, reason = "C", "moderate predicted confidence; ensemble validation required"
        else:
            grade, reason = "D", "predicted epitope confidence unavailable or low"
    else:
        grade, reason = "D", "unknown structure provenance"
    return {
        **record,
        "quality_grade": grade,
        "quality_reason": reason,
        "epitope_confidence_missing": bool(epitope_required and record.get("epitope_plddt") is None),
    }


def _materialized_coordinate_path(target: dict) -> Path | None:
    raw_path = (target.get("structure") or {}).get("coordinate_path")
    if not raw_path:
        return None
    path = Path(raw_path)
    return path if path.is_file() and path.stat().st_size > 0 else None


def refresh_target_structure_readiness(target: dict) -> dict:
    """Recompute derived structure gates without repeating public discovery.

    A selected database record is only metadata.  ``coordinates_ready`` becomes
    true after the selected coordinates have been materialized and validated on
    the backend, preventing a bootstrap draft from promising a file that Design
    cannot open.
    """
    updated = json.loads(json.dumps(target))
    plan = dict(updated.get("structure_plan") or {})
    selected = plan.get("selected") or {}
    selected_grade = selected.get("quality_grade") or plan.get("quality_grade") or "D"
    binding_site = updated.get("binding_site") or {}
    configured_structure = updated.get("structure") or {}
    site_reviewed = bool(
        binding_site.get("residues")
        and binding_site.get("status") in {"known", "user_reviewed"}
    )
    chain_reviewed = bool(configured_structure.get("chain") or selected.get("chain"))
    coordinates_selected = bool(selected and selected_grade in {"A", "B", "C"})
    coordinate_path = _materialized_coordinate_path(updated)
    coordinates_ready = bool(coordinates_selected and coordinate_path)
    ready_for_design = bool(coordinates_ready and site_reviewed and chain_reviewed)

    if not selected:
        next_step = "run_target_structure_prediction"
    elif not coordinates_ready:
        next_step = "materialize_selected_coordinates"
    elif not site_reviewed or not chain_reviewed:
        next_step = "review_target_chain_and_epitope_coordinates"
    elif selected.get("kind") == "predicted" or selected_grade == "C":
        next_step = "inspect_epitope_and_generate_conformational_ensemble"
    else:
        next_step = "ready_for_design"

    plan.update({
        "quality_grade": selected_grade,
        "coordinates_selected": coordinates_selected,
        "coordinates_ready": coordinates_ready,
        "coordinate_artifact_sha256": (
            configured_structure.get("coordinate_sha256") if coordinates_ready else None
        ),
        "binding_site_reviewed": site_reviewed,
        "chain_reviewed": chain_reviewed,
        "ready_for_design": ready_for_design,
        "required_next_step": next_step,
    })
    updated["structure_plan"] = plan
    return updated


def resolve_target_structure(
    target: dict,
    *,
    experimental_provider: ExperimentalStructureProvider | None = None,
    predicted_provider: PredictedStructureProvider | None = None,
) -> dict:
    experimental_provider = experimental_provider or RCSBStructureProvider()
    predicted_provider = predicted_provider or AlphaFoldDBProvider()
    experimental = [grade_structure(row) for row in experimental_provider.find(target)]

    exp_rank = {"A": 0, "B": 1, "C": 2, "D": 3}
    experimental.sort(key=lambda row: (
        exp_rank.get(row["quality_grade"], 9),
        row.get("resolution") if row.get("resolution") is not None else 99,
        not row.get("has_bound_partner", False),
    ))
    usable_experimental = next(
        (row for row in experimental if row["quality_grade"] in {"A", "B", "C"}), None
    )

    # Prediction is a fallback, not a routine second source when usable
    # experimental coordinates already exist.
    predicted = [] if usable_experimental else [
        grade_structure(row) for row in predicted_provider.find(target)
    ]
    predicted.sort(key=lambda row: (
        exp_rank.get(row["quality_grade"], 9),
        -(float(row.get("epitope_plddt") or row.get("mean_plddt") or 0)),
    ))

    usable_predicted = next((row for row in predicted if row["quality_grade"] in {"B", "C"}), None)
    selected = usable_experimental or usable_predicted
    if usable_experimental:
        status = "experimental_selected"
    elif usable_predicted:
        status = "predicted_selected"
    elif predicted:
        status = "prediction_low_confidence"
    else:
        status = "prediction_required"

    selected_grade = selected.get("quality_grade") if selected else "D"
    plan = {
        "status": status,
        "selected": selected,
        "experimental_candidates": experimental,
        "predicted_candidates": predicted,
        "quality_grade": selected_grade,
        "needs_ensemble": bool(
            not selected or selected.get("kind") == "predicted" or selected_grade in {"C", "D"}
        ),
    }
    return refresh_target_structure_readiness({**target, "structure_plan": plan})["structure_plan"]


def resolve_project_structures(
    config: dict,
    *,
    experimental_provider: ExperimentalStructureProvider | None = None,
    predicted_provider: PredictedStructureProvider | None = None,
) -> dict:
    updated = json.loads(json.dumps(config))
    for target in updated["targets"]:
        target["structure_plan"] = resolve_target_structure(
            target,
            experimental_provider=experimental_provider,
            predicted_provider=predicted_provider,
        )
        selected = target["structure_plan"].get("selected")
        if selected and selected.get("kind") == "experimental":
            target["structure"] = {
                **(target.get("structure") or {}),
                "pdb_id": selected.get("pdb_id"),
                "source": "rcsb",
                "quality_grade": selected.get("quality_grade"),
            }
        elif selected:
            target["structure"] = {
                **(target.get("structure") or {}),
                "model_id": selected.get("id"),
                "pdb_url": selected.get("pdb_url"),
                "cif_url": selected.get("cif_url"),
                "source": selected.get("source"),
                "quality_grade": selected.get("quality_grade"),
            }
    return updated


def refresh_project_structure_readiness(config: dict) -> dict:
    """Refresh binding-site, chain, and materialization gates from existing plans."""
    updated = json.loads(json.dumps(config))
    updated["targets"] = [refresh_target_structure_readiness(target) for target in updated["targets"]]
    return updated


def _download_coordinate_bytes(url: str, *, timeout: int = 30) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "cycpep-agent/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def materialize_target_coordinates(
    config: dict,
    target_id: str,
    target_root: str | Path,
    *,
    structure_record_id: str | None = None,
    downloader=None,
) -> dict:
    """Download and validate the selected target coordinates into backend storage.

    ``target_root`` is controlled by the backend, never by a browser request.
    The returned config records the internal path and digest; an HTTP adapter
    must expose an opaque artifact ID instead of the local path.
    """
    updated = json.loads(json.dumps(config))
    target = next(
        (row for row in updated.get("targets", []) if str(row.get("id", "")).casefold() == str(target_id).casefold()),
        None,
    )
    if target is None:
        raise StructureNotReadyError(f"target is not configured: {target_id}")
    selected = (target.get("structure_plan") or {}).get("selected") or {}
    selected_refs = {
        str(value).casefold()
        for value in (selected.get("id"), selected.get("pdb_id"))
        if value
    }
    if structure_record_id is not None and str(structure_record_id).casefold() not in selected_refs:
        raise StructureNotReadyError(
            f"structure record {structure_record_id} is not the selected candidate for {target_id}"
        )
    source_url = selected.get("pdb_url")
    if not source_url:
        raise StructureNotReadyError(f"selected structure for {target_id} has no PDB coordinate URL")

    payload = (downloader or _download_coordinate_bytes)(source_url)
    if not isinstance(payload, bytes) or not payload.strip():
        raise StructureNotReadyError(f"downloaded coordinates for {target_id} are empty")
    text_head = payload[:200000].decode("utf-8", errors="ignore")
    if not any(line.startswith(("ATOM  ", "HETATM")) for line in text_head.splitlines()):
        raise StructureNotReadyError(f"downloaded artifact for {target_id} is not a PDB coordinate file")

    root = Path(target_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Design resolves experimental structures by PDB ID and predicted structures
    # by target ID, so materialize under the same stable lookup key.
    record_id = selected.get("pdb_id") or target.get("id") or selected.get("id")
    safe_name = "".join(char for char in str(record_id) if char.isalnum() or char in {"-", "_"})
    if not safe_name:
        raise StructureNotReadyError("selected structure has no safe artifact identifier")
    destination = (root / f"{safe_name}.pdb").resolve()
    if destination.parent != root:
        raise StructureNotReadyError("coordinate artifact path escaped target root")
    handle, temp_name = tempfile.mkstemp(prefix=f".{safe_name}.", suffix=".tmp", dir=root)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        os.replace(temp_name, destination)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)

    target["structure"] = {
        **(target.get("structure") or {}),
        "coordinate_path": str(destination),
        "coordinate_sha256": hashlib.sha256(payload).hexdigest(),
        "coordinate_format": "pdb",
    }
    refreshed = refresh_target_structure_readiness(target)
    target.clear()
    target.update(refreshed)
    return updated


def assert_target_structure_ready(config: dict, target_id: str) -> dict:
    """Return the configured target only when its structure can enter design.

    Curated legacy projects may have an explicit PDB ID but no generated
    ``structure_plan``. Newly bootstrapped projects always carry the richer
    provenance and quality record.
    """
    target = next(
        (
            item for item in config.get("targets", [])
            if str(item.get("id", "")).casefold() == str(target_id).casefold()
            or str((item.get("structure") or {}).get("pdb_id", "")).casefold()
            == str(target_id).casefold()
        ),
        None,
    )
    if target is None:
        raise StructureNotReadyError(f"target is not configured: {target_id}")
    plan = target.get("structure_plan")
    if plan is not None and not plan.get("ready_for_design"):
        raise StructureNotReadyError(
            f"target {target['id']} has no design-ready structure; next step: "
            f"{plan.get('required_next_step', 'review or predict target structure')}"
        )
    if plan is not None:
        coordinate_path = _materialized_coordinate_path(target)
        if coordinate_path is None:
            raise StructureNotReadyError(
                f"target {target['id']} coordinate artifact is missing or empty"
            )
        expected_hash = (target.get("structure") or {}).get("coordinate_sha256")
        if expected_hash:
            actual_hash = hashlib.sha256(coordinate_path.read_bytes()).hexdigest()
            if actual_hash != expected_hash:
                raise StructureNotReadyError(
                    f"target {target['id']} coordinate artifact hash does not match the approved config"
                )
    if plan is None and not (target.get("structure") or {}).get("pdb_id"):
        raise StructureNotReadyError(
            f"target {target['id']} has neither a reviewed structure plan nor an explicit PDB ID"
        )
    return target
