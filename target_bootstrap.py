"""Target bootstrap, human review gate, and CLI for project initialization."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from llm_client import ConfiguredLLM
from project_config import normalize_project_config, target_slug
from structure_resolution import (
    TRUSTED_COORDINATE_HOSTS,
    materialize_target_coordinates,
    refresh_project_structure_readiness,
    resolve_project_structures,
)


class BootstrapError(RuntimeError):
    pass


class ReviewRequiredError(BootstrapError):
    pass


class DiscoveryProvider(Protocol):
    def resolve(self, identifier: str, identifier_type: str, organism_id: int) -> dict: ...


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_json(url: str, *, data: dict | None = None, timeout: int = 30):
    encoded = json.dumps(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(
        url, data=encoded,
        headers={"Content-Type": "application/json", "User-Agent": "cycpep-agent/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class PublicTargetDiscovery:
    """Resolve gene/UniProt/PDB identifiers against UniProt and RCSB."""

    UNIPROT_ENTRY = "https://rest.uniprot.org/uniprotkb/{accession}.json"
    UNIPROT_SEARCH = "https://rest.uniprot.org/uniprotkb/search?{query}"
    RCSB_GRAPHQL = "https://data.rcsb.org/graphql"

    @staticmethod
    def _uniprot_summary(record: dict) -> dict:
        genes = record.get("genes") or []
        gene_name = ((genes[0].get("geneName") or {}).get("value") if genes else None)
        protein = record.get("proteinDescription") or {}
        recommended = ((protein.get("recommendedName") or {}).get("fullName") or {}).get("value")
        if not recommended:
            submissions = protein.get("submissionNames") or []
            recommended = ((submissions[0].get("fullName") or {}).get("value") if submissions else None)
        return {
            "id": gene_name or record.get("primaryAccession"),
            "gene_name": gene_name,
            "uniprot": record.get("primaryAccession"),
            "uniprot_id": record.get("uniProtkbId"),
            "protein_name": recommended,
            "organism": (record.get("organism") or {}).get("scientificName"),
            "length": (record.get("sequence") or {}).get("length"),
        }

    def _resolve_uniprot(self, accession: str) -> dict:
        record = _get_json(self.UNIPROT_ENTRY.format(accession=urllib.parse.quote(accession)))
        summary = self._uniprot_summary(record)
        return {
            "primary": summary,
            "candidates": [summary],
            "ambiguous": False,
            "evidence": [{
                "source": "UniProt", "id": summary.get("uniprot"),
                "url": f"https://www.uniprot.org/uniprotkb/{summary.get('uniprot')}",
            }],
        }

    def _resolve_gene(self, gene: str, organism_id: int) -> dict:
        params = urllib.parse.urlencode({
            "query": f"(gene_exact:{gene}) AND (organism_id:{organism_id})",
            "format": "json", "size": 5,
        })
        payload = _get_json(self.UNIPROT_SEARCH.format(query=params))
        candidates = [self._uniprot_summary(row) for row in payload.get("results", [])]
        if not candidates:
            raise BootstrapError(f"no UniProt target found for gene {gene}")
        return {
            "primary": candidates[0],
            "candidates": candidates,
            "ambiguous": len(candidates) > 1,
            "evidence": [{
                "source": "UniProt search", "id": item.get("uniprot"),
                "url": f"https://www.uniprot.org/uniprotkb/{item.get('uniprot')}",
            } for item in candidates],
        }

    def _resolve_pdb(self, pdb_id: str) -> dict:
        query = """
        query ($id: String!) {
          entry(entry_id: $id) {
            struct { title }
            polymer_entities {
              rcsb_polymer_entity_container_identifiers { uniprot_ids auth_asym_ids }
              entity_poly { pdbx_seq_one_letter_code_can }
            }
          }
        }
        """
        payload = _get_json(self.RCSB_GRAPHQL, data={
            "query": query, "variables": {"id": pdb_id.upper()},
        })
        entry = (payload.get("data") or {}).get("entry")
        if not entry:
            raise BootstrapError(f"PDB entry not found: {pdb_id}")
        accessions = []
        chains = {}
        for entity in entry.get("polymer_entities") or []:
            identifiers = entity.get("rcsb_polymer_entity_container_identifiers") or {}
            entity_uniprots = identifiers.get("uniprot_ids") or []
            for accession in entity_uniprots:
                accessions.append(accession)
                chains.setdefault(accession, []).extend(identifiers.get("auth_asym_ids") or [])
        accessions = list(dict.fromkeys(accessions))
        summaries = []
        for accession in accessions:
            try:
                summary = self._resolve_uniprot(accession)["primary"]
            except Exception:
                summary = {"id": accession, "uniprot": accession}
            summary["structure"] = {
                "pdb_id": pdb_id.upper(),
                "chain": (chains.get(accession) or [None])[0],
                "source": "user_input_pdb",
            }
            summaries.append(summary)
        if not summaries:
            summaries = [{
                "id": pdb_id.upper(), "uniprot": None,
                "structure": {"pdb_id": pdb_id.upper(), "source": "user_input_pdb"},
            }]
        return {
            "primary": summaries[0],
            "candidates": summaries,
            "ambiguous": len(summaries) > 1,
            "evidence": [{
                "source": "RCSB PDB", "id": pdb_id.upper(),
                "title": (entry.get("struct") or {}).get("title"),
                "url": f"https://www.rcsb.org/structure/{pdb_id.upper()}",
            }],
        }

    def resolve(self, identifier: str, identifier_type: str = "auto",
                organism_id: int = 9606) -> dict:
        identifier = identifier.strip()
        kind = identifier_type.lower()
        if kind == "auto":
            if re.fullmatch(r"[0-9][A-Za-z0-9]{3}", identifier):
                kind = "pdb"
            elif re.fullmatch(r"(?:[A-NR-Z][0-9][A-Z0-9]{3}[0-9]|[A-Z0-9]{10})", identifier.upper()):
                kind = "uniprot"
            else:
                kind = "gene"
        if kind == "pdb":
            return self._resolve_pdb(identifier)
        if kind == "uniprot":
            return self._resolve_uniprot(identifier.upper())
        if kind == "gene":
            return self._resolve_gene(identifier, organism_id)
        raise BootstrapError("identifier_type must be auto, gene, uniprot, or pdb")


BOOTSTRAP_SYSTEM_PROMPT = """You are a protein target research assistant.
Return one JSON object only. Use only the supplied database evidence and explicit user context.
Unknown facts must be null or listed under uncertainties. Do not invent residue numbers,
binders, structures, affinity values, or citations. Source references must point to an evidence
ID supplied in the prompt."""

BOOTSTRAP_USER_PROMPT = """Prepare a reviewable cyclic-peptide binder project draft.

Database-resolved target:
{resolved}

User context:
{context}

Return this shape:
{{
  "project_name": "short descriptive name",
  "objective": "binder or multi_target_binder",
  "target_enrichment": {{
    "aliases": [],
    "function_summary": null,
    "biological_mechanism": null,
    "binding_site": {{
      "description": null,
      "residues": [],
      "status": "known|hypothesis|unknown",
      "confidence": "high|medium|low",
      "source_refs": []
    }},
    "natural_partners": [],
    "known_binders": [],
    "off_targets": [],
    "research_queries": [],
    "uncertainties": []
  }},
  "assumptions": []
}}
"""


def _content_for_digest(config: dict) -> dict:
    content = json.loads(json.dumps(config))
    content.pop("review", None)
    return content


def config_digest(config: dict) -> str:
    encoded = json.dumps(
        _content_for_digest(config), sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def review_project_config(config: dict) -> dict:
    blocking = []
    warnings = []
    targets = config.get("targets") or []
    if not targets:
        blocking.append("no_target")
    for target in targets:
        label = target.get("id") or "unknown"
        if not target.get("uniprot") and not (target.get("structure") or {}).get("pdb_id"):
            blocking.append(f"{label}:no_resolved_identifier")
        site = target.get("binding_site") or {}
        if not site.get("residues"):
            warnings.append(f"{label}:binding_site_residues_missing_or_hypothetical")
        elif site.get("status") not in {"known", "user_reviewed"}:
            warnings.append(f"{label}:binding_site_hypothesis_requires_user_review")
        plan = target.get("structure_plan") or {}
        if not plan.get("ready_for_design"):
            next_step = plan.get("required_next_step") or "manual_structure_review_required"
            warnings.append(f"{label}:structure_not_design_ready:{next_step}")
        if plan.get("coordinates_ready") and not plan.get("chain_reviewed"):
            warnings.append(f"{label}:target_chain_requires_user_review")
        selected = plan.get("selected") or {}
        if selected.get("epitope_confidence_missing"):
            warnings.append(f"{label}:structure_epitope_quality_unreviewed")
        if plan.get("needs_ensemble"):
            warnings.append(f"{label}:conformational_ensemble_recommended")
    bootstrap = config.get("bootstrap") or {}
    if bootstrap.get("ambiguous_identifier"):
        blocking.append("ambiguous_identifier_requires_user_selection")
    if bootstrap.get("llm_status") != "complete":
        warnings.append("llm_enrichment_incomplete")
    return {
        "blocking_issues": sorted(set(blocking)),
        "warnings": sorted(set(warnings)),
        "checklist": {
            "target_identity_resolved": not any("resolved_identifier" in item or "ambiguous" in item for item in blocking),
            "binding_site_reviewed": not any("binding_site" in item for item in warnings),
            "structure_reviewed": not any("structure_" in item for item in warnings),
            "ready_to_approve": not blocking,
        },
    }


def _write_json_atomic(path: str | Path, payload: dict):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(temp_name, destination)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _merge_patch(target: dict, patch: dict) -> dict:
    result = json.loads(json.dumps(target))
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_patch(result[key], value)
        else:
            result[key] = value
    return result


class TargetBootstrapper:
    def __init__(self, *, discovery: DiscoveryProvider | None = None, llm=None,
                 experimental_provider=None, predicted_provider=None):
        self.discovery = discovery or PublicTargetDiscovery()
        self.llm = llm or ConfiguredLLM()
        self.experimental_provider = experimental_provider
        self.predicted_provider = predicted_provider

    def create_draft(self, *, identifier: str, identifier_type: str = "auto",
                     organism_id: int = 9606, epitope: str | None = None,
                     objective: str = "binder", output_path: str | Path | None = None) -> dict:
        resolved = self.discovery.resolve(identifier, identifier_type, organism_id)
        primary = dict(resolved["primary"])
        evidence = []
        for index, item in enumerate(resolved.get("evidence", []), 1):
            evidence.append({"evidence_id": f"E{index:03d}", **item})

        context = {"epitope": epitope, "requested_objective": objective}
        llm_status = "complete"
        llm_error = None
        try:
            enrichment = self.llm.json(
                system_prompt=BOOTSTRAP_SYSTEM_PROMPT,
                user_prompt=BOOTSTRAP_USER_PROMPT.format(
                    resolved=json.dumps({"target": primary, "evidence": evidence}, ensure_ascii=False),
                    context=json.dumps(context, ensure_ascii=False),
                ),
            )
        except Exception as exc:
            enrichment = {"target_enrichment": {}, "assumptions": []}
            llm_status = "failed"
            llm_error = str(exc)

        target_enrichment = enrichment.get("target_enrichment") or {}
        binding_site = target_enrichment.get("binding_site") or {}
        if epitope:
            binding_site = {
                **binding_site,
                "description": epitope,
                "status": "user_supplied",
                "confidence": "user_review_required",
            }
        target = {
            **primary,
            "required": True,
            "aliases": target_enrichment.get("aliases", []),
            "function_summary": target_enrichment.get("function_summary"),
            "biological_mechanism": target_enrichment.get("biological_mechanism"),
            "binding_site": binding_site,
            "natural_partners": target_enrichment.get("natural_partners", []),
            "known_binders": target_enrichment.get("known_binders", []),
            "off_targets": target_enrichment.get("off_targets", []),
            "research_queries": target_enrichment.get("research_queries", []),
            "uncertainties": target_enrichment.get("uncertainties", []),
        }
        project_id = target_slug(f"{primary.get('id') or identifier}_cycpep")
        draft = normalize_project_config({
            "schema_version": 1,
            "project_id": project_id,
            "name": enrichment.get("project_name") or f"{primary.get('id') or identifier} cyclic peptide binder",
            "modality": "cyclic_peptide",
            "objective": enrichment.get("objective") or objective,
            "targets": [target],
            "selection": {"final_rule": "threshold_clearance_then_pareto"},
            "bootstrap": {
                "input": {"identifier": identifier, "identifier_type": identifier_type, "organism_id": organism_id},
                "resolved_candidates": resolved.get("candidates", []),
                "ambiguous_identifier": bool(resolved.get("ambiguous")),
                "evidence": evidence,
                "llm_status": llm_status,
                "llm_model": getattr(self.llm, "model", "custom"),
                "llm_error": llm_error,
                "assumptions": enrichment.get("assumptions", []),
            },
        })
        draft = resolve_project_structures(
            draft,
            experimental_provider=self.experimental_provider,
            predicted_provider=self.predicted_provider,
        )
        review = review_project_config(draft)
        draft["review"] = {
            "status": "draft",
            "revision": 1,
            "created_at": _utcnow(),
            "updated_at": _utcnow(),
            "content_digest": config_digest(draft),
            "blocking_issues": review["blocking_issues"],
            "warnings": review["warnings"],
            "checklist": review["checklist"],
            "history": [{"action": "created", "at": _utcnow()}],
        }
        if output_path:
            _write_json_atomic(output_path, draft)
        return draft


def _structure_discovery_inputs(config: dict) -> list[tuple]:
    return [
        (
            target.get("id"),
            target.get("uniprot"),
            (target.get("structure") or {}).get("pdb_id"),
            (target.get("structure") or {}).get("model_id"),
            (target.get("structure") or {}).get("source"),
        )
        for target in config.get("targets", [])
    ]


def _structure_readiness_inputs(config: dict) -> list[tuple]:
    return [
        (
            target.get("id"),
            target.get("binding_site"),
            target.get("structure"),
        )
        for target in config.get("targets", [])
    ]


def _finish_edit(
    draft_path: Path,
    config: dict,
    updated: dict,
    *,
    action: str,
    experimental_provider=None,
    predicted_provider=None,
) -> dict:
    previous_review = config.get("review") or {}
    updated = normalize_project_config(updated)
    previous_discovery_inputs = _structure_discovery_inputs(config)
    updated_discovery_inputs = _structure_discovery_inputs(updated)
    if previous_discovery_inputs != updated_discovery_inputs:
        changed_target_indexes = {
            index
            for index in range(max(len(previous_discovery_inputs), len(updated_discovery_inputs)))
            if index >= len(previous_discovery_inputs)
            or index >= len(updated_discovery_inputs)
            or previous_discovery_inputs[index] != updated_discovery_inputs[index]
        }
        updated = resolve_project_structures(
            updated,
            experimental_provider=experimental_provider,
            predicted_provider=predicted_provider,
            invalidate_target_indexes=changed_target_indexes,
        )
    elif _structure_readiness_inputs(config) != _structure_readiness_inputs(updated):
        updated = refresh_project_structure_readiness(updated)
    audit = review_project_config(updated)
    history = list(previous_review.get("history") or [])
    history.append({
        "action": action,
        "at": _utcnow(),
        "previous_revision": previous_review.get("revision"),
    })
    updated["review"] = {
        "status": "draft",
        "revision": int(previous_review.get("revision", 0)) + 1,
        "created_at": previous_review.get("created_at", _utcnow()),
        "updated_at": _utcnow(),
        "content_digest": config_digest(updated),
        "blocking_issues": audit["blocking_issues"],
        "warnings": audit["warnings"],
        "checklist": audit["checklist"],
        "history": history,
    }
    _write_json_atomic(draft_path, updated)
    return updated


def edit_draft(path: str | Path, patch: dict, *, experimental_provider=None,
               predicted_provider=None) -> dict:
    """Apply a project-level merge patch.

    HTTP adapters should use :func:`edit_target_draft` for target edits because
    RFC 7396 replaces arrays wholesale.  This generic helper remains available
    for trusted CLI callers that intentionally provide a complete target list.
    """
    draft_path = Path(path)
    config = json.loads(draft_path.read_text(encoding="utf-8"))
    updated = _merge_patch(config, patch)
    return _finish_edit(
        draft_path,
        config,
        updated,
        action="edited",
        experimental_provider=experimental_provider,
        predicted_provider=predicted_provider,
    )


def edit_target_draft(path: str | Path, target_id: str, patch: dict, *,
                      experimental_provider=None, predicted_provider=None) -> dict:
    """Safely merge one target without replacing the complete targets array."""
    if not isinstance(patch, dict):
        raise BootstrapError("target patch must be an object")
    server_managed_fields = {
        "uniprot", "uniprot_id", "gene_name", "protein_name",
        "organism", "length", "metric_slug", "structure_plan",
    }
    forbidden = server_managed_fields.intersection(patch)
    if forbidden:
        raise BootstrapError(
            "target patch contains server-managed fields: " + ", ".join(sorted(forbidden))
        )
    protected_structure_fields = {"coordinate_path", "coordinate_sha256", "coordinate_format"}
    structure_patch = patch.get("structure") or {}
    if not isinstance(structure_patch, dict):
        raise BootstrapError("target structure patch must be an object")
    if protected_structure_fields.intersection(structure_patch):
        raise BootstrapError("coordinate artifact metadata can only be written by materialization")
    draft_path = Path(path)
    config = json.loads(draft_path.read_text(encoding="utf-8"))
    updated = json.loads(json.dumps(config))
    matches = [
        (index, target) for index, target in enumerate(updated.get("targets", []))
        if str(target.get("id", "")).casefold() == str(target_id).casefold()
    ]
    if len(matches) != 1:
        raise BootstrapError(f"target is not configured or is ambiguous: {target_id}")
    index, target = matches[0]
    updated["targets"][index] = _merge_patch(target, patch)
    return _finish_edit(
        draft_path,
        config,
        updated,
        action="target_edited",
        experimental_provider=experimental_provider,
        predicted_provider=predicted_provider,
    )


def select_resolved_candidate(path: str | Path, candidate_ref: str, *,
                              experimental_provider=None, predicted_provider=None) -> dict:
    """Select one server-offered identity candidate and clear ambiguity safely."""
    draft_path = Path(path)
    config = json.loads(draft_path.read_text(encoding="utf-8"))
    candidates = (config.get("bootstrap") or {}).get("resolved_candidates") or []
    ref = str(candidate_ref).casefold()
    matches = [
        candidate for candidate in candidates
        if ref in {
            str(candidate.get("id", "")).casefold(),
            str(candidate.get("uniprot", "")).casefold(),
        }
    ]
    if len(matches) != 1:
        raise BootstrapError("candidate_ref must identify exactly one offered resolved candidate")
    if len(config.get("targets") or []) != 1:
        raise BootstrapError("candidate selection currently requires a single-target bootstrap draft")

    candidate = json.loads(json.dumps(matches[0]))
    updated = json.loads(json.dumps(config))
    old_target = updated["targets"][0]
    candidate_structure = candidate.get("structure") or {}
    identity_changed = (
        str(old_target.get("uniprot") or "").casefold()
        != str(candidate.get("uniprot") or "").casefold()
        or str(old_target.get("id") or "").casefold()
        != str(candidate.get("id") or "").casefold()
    )
    # Never carry a PDB selection, chain, or materialized coordinates from a
    # different identity. Discovery will populate a new structure plan.
    if identity_changed:
        updated["targets"][0] = {
            **candidate,
            "required": old_target.get("required", True),
            "aliases": [],
            "function_summary": None,
            "biological_mechanism": None,
            "binding_site": {
                "description": None,
                "residues": [],
                "status": "unknown",
                "confidence": "user_review_required",
                "source_refs": [],
            },
            "natural_partners": [],
            "known_binders": [],
            "off_targets": [],
            "research_queries": [],
            "uncertainties": ["Target identity changed; enrichment and binding site require review."],
            "structure": candidate_structure,
        }
    else:
        updated["targets"][0] = {
            **old_target,
            **candidate,
            "structure": candidate_structure,
        }
    updated["bootstrap"]["ambiguous_identifier"] = False
    updated["bootstrap"]["selected_candidate"] = candidate
    return _finish_edit(
        draft_path,
        config,
        updated,
        action="resolved_candidate_selected",
        experimental_provider=experimental_provider,
        predicted_provider=predicted_provider,
    )


def materialize_draft_coordinates(path: str | Path, target_id: str,
                                  target_root: str | Path, *,
                                  structure_record_id: str | None = None,
                                  downloader=None,
                                  allowed_hosts=TRUSTED_COORDINATE_HOSTS) -> dict:
    """Materialize selected coordinates and persist the refreshed review gate."""
    draft_path = Path(path)
    config = json.loads(draft_path.read_text(encoding="utf-8"))
    updated = materialize_target_coordinates(
        config, target_id, target_root,
        structure_record_id=structure_record_id,
        downloader=downloader,
        allowed_hosts=allowed_hosts,
    )
    return _finish_edit(
        draft_path,
        config,
        updated,
        action="coordinates_materialized",
    )


def approve_draft(path: str | Path, *, output_path: str | Path | None = None,
                  force: bool = False, justification: str | None = None) -> dict:
    draft_path = Path(path)
    config = normalize_project_config(json.loads(draft_path.read_text(encoding="utf-8")))
    audit = review_project_config(config)
    if audit["blocking_issues"] and not force:
        raise ReviewRequiredError(
            "cannot approve while blocking issues remain: " + ", ".join(audit["blocking_issues"])
        )
    if force and not justification:
        raise ReviewRequiredError("forced approval requires a justification")
    previous = config.get("review") or {}
    history = list(previous.get("history") or [])
    history.append({
        "action": "approved", "at": _utcnow(), "forced": force,
        "justification": justification,
    })
    config["review"] = {
        "status": "approved",
        "revision": int(previous.get("revision", 1)),
        "created_at": previous.get("created_at", _utcnow()),
        "updated_at": _utcnow(),
        "approved_at": _utcnow(),
        "approved_digest": config_digest(config),
        "content_digest": config_digest(config),
        "blocking_issues": audit["blocking_issues"],
        "warnings": audit["warnings"],
        "checklist": audit["checklist"],
        "forced": force,
        "justification": justification,
        "history": history,
    }
    _write_json_atomic(draft_path, config)
    if output_path and Path(output_path).resolve() != draft_path.resolve():
        _write_json_atomic(output_path, config)
    return config


def assert_project_approved(config: dict):
    review = config.get("review") or {}
    if review.get("status") != "approved":
        raise ReviewRequiredError("project config is not approved; review/edit/approve it before running")
    expected = review.get("approved_digest")
    actual = config_digest(config)
    if not expected or expected != actual:
        raise ReviewRequiredError("approved project config changed after approval; review it again")


def _print_review(config: dict):
    review = config.get("review") or review_project_config(config)
    print(json.dumps({
        "project_id": config.get("project_id"),
        "targets": config.get("targets"),
        "review": review,
    }, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap and approve a peptide-binder project")
    commands = parser.add_subparsers(dest="command", required=True)

    draft_cmd = commands.add_parser("draft")
    draft_cmd.add_argument("--identifier", required=True)
    draft_cmd.add_argument("--type", default="auto", choices=["auto", "gene", "uniprot", "pdb"])
    draft_cmd.add_argument("--organism-id", type=int, default=9606)
    draft_cmd.add_argument("--epitope")
    draft_cmd.add_argument("--objective", default="binder")
    draft_cmd.add_argument("--output", required=True)

    show_cmd = commands.add_parser("show")
    show_cmd.add_argument("--draft", required=True)

    edit_cmd = commands.add_parser("edit")
    edit_cmd.add_argument("--draft", required=True)
    edit_cmd.add_argument("--patch", required=True, help="RFC 7396-style JSON merge patch")

    approve_cmd = commands.add_parser("approve")
    approve_cmd.add_argument("--draft", required=True)
    approve_cmd.add_argument("--output")
    approve_cmd.add_argument("--force", action="store_true")
    approve_cmd.add_argument("--justification")

    args = parser.parse_args()
    if args.command == "draft":
        config = TargetBootstrapper().create_draft(
            identifier=args.identifier, identifier_type=args.type,
            organism_id=args.organism_id, epitope=args.epitope,
            objective=args.objective, output_path=args.output,
        )
    elif args.command == "show":
        config = json.loads(Path(args.draft).read_text(encoding="utf-8"))
    elif args.command == "edit":
        patch = json.loads(Path(args.patch).read_text(encoding="utf-8"))
        config = edit_draft(args.draft, patch)
    else:
        config = approve_draft(
            args.draft, output_path=args.output, force=args.force,
            justification=args.justification,
        )
    _print_review(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
