"""
Research Agent - MDM2/MDMX 靶点调研管线
8 步: RCSB Search -> GraphQL Enrich -> biotite interface -> aggregate pockets ->
      superpose analyze -> PubMed -> LLM extract -> threshold evidence
每步挂 EvidenceLogger tool_trace。biotite 失败时自动回退到预置常量。
"""

import functools
import json, os, subprocess, sys, time, hashlib, tempfile
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"

from data_layer import State, EvidenceLogger
import data_layer
from project_config import load_project_config, required_target_ids, target_slug
from target_bootstrap import assert_project_approved
from threshold_contract import normalize_threshold_entry, normalize_thresholds
from threshold_calibration import (
    CALIBRATION_SCHEMA_VERSION,
    ControlDataError,
    calibrate_thresholds,
    load_control_dataset,
)

# ============================================================
# 惰性项目运行时（Engineering Standard §7 / Roadmap PR5）
# ============================================================
# 项目配置与派生路径不再于 import 时解析。``run(project_config=...)`` 可以
# 注入显式项目配置（仅本次调用有效），未注入时回退到环境选定的默认项目。


@functools.lru_cache(maxsize=1)
def _load_default_project_config() -> dict:
    return load_project_config()


_injected_project_config = None


def _get_project_config() -> dict:
    if _injected_project_config is not None:
        return _injected_project_config
    return _load_default_project_config()


def _get_project_target_ids() -> tuple:
    return tuple(target["id"] for target in _get_project_config()["targets"])


def _get_is_mdm_reference() -> bool:
    return set(_get_project_target_ids()) == {"MDM2", "MDMX"}


def _get_cache_path() -> Path:
    config = _get_project_config()
    if _get_is_mdm_reference():
        return _module_attr("DATA_DIR") / "_research_cache.json"
    return _module_attr("DATA_DIR") / f"_research_cache_{target_slug(config['project_id'])}.json"


def _get_thresholds_cache() -> Path:
    return _module_attr("DATA_DIR") / "_thresholds_cache.json"


def _get_build_dynamic_pockets():
    from agents import research_steps
    return research_steps._build_dynamic_pockets


def _get_run_generic_pipeline():
    from agents import research_steps
    return research_steps._run_generic_pipeline


def _get_run_pipeline():
    from agents import research_steps
    return research_steps._run_pipeline


_LAZY_ATTRIBUTES = {
    "PROJECT_CONFIG": _get_project_config,
    "PROJECT_TARGET_IDS": _get_project_target_ids,
    "IS_MDM_REFERENCE": _get_is_mdm_reference,
    "CACHE_PATH": _get_cache_path,
    "THRESHOLDS_CACHE": _get_thresholds_cache,
    "DATA_DIR": lambda: data_layer.DATA_DIR,
    "EVIDENCE_DIR": lambda: data_layer.EVIDENCE_DIR,
    "_build_dynamic_pockets": _get_build_dynamic_pockets,
    "_run_generic_pipeline": _get_run_generic_pipeline,
    "_run_pipeline": _get_run_pipeline,
}


def __getattr__(name):
    """PEP 562: serve legacy module names lazily on first access (PR5)."""
    getter = _LAZY_ATTRIBUTES.get(name)
    if getter is not None:
        return getter()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _module_attr(name):
    """Read a lazy module name through the module object (PEP 562 does not
    apply to bare globals inside this module)."""
    return getattr(sys.modules[__name__], name)


def _cfg() -> dict:
    """Active approved project config (injected or environment default)."""
    return _module_attr("PROJECT_CONFIG")


# ===== 预置常量（biotite 失败时兜底）=====
TARGETS = {
    "MDM2": {"uniprot": "Q00987", "reference_pdb": ["1YCR", "4HG7", "3V3B"],
             "pocket_residues": {
                 "Phe19_pocket": ["Gly58","Ile61","Met62","Tyr67","Gln72","Val75","Val93"],
                 "Trp23_pocket": ["Leu54","Leu57","Gly58","Ile61","Val93"],
                 "Leu26_pocket": ["Leu54","Val93","His96","Ile99","Tyr100"]}},
    "MDMX": {"uniprot": "O15151", "reference_pdb": ["3DAB", "3LBK"],
             "pocket_residues": {
                 "Phe19_pocket": ["Gly57","Ile60","Met61","Tyr66","Gln71","Val74","Val92"],
                 "Trp23_pocket": ["Met53","Leu56","Gly57","Ile60","Val92","Leu98"],
                 "Leu26_pocket": ["Met53","Val92","Pro95","Leu98","Tyr99"]}},
}
POCKET_DIFFERENCES = {
    "_method": "curated fallback pocket definitions; dynamic structure analysis unavailable",
    "_source": "curated_fallback",
    "Phe19_pocket": {"MDM2_residues": TARGETS["MDM2"]["pocket_residues"]["Phe19_pocket"],
                     "MDMX_residues": TARGETS["MDMX"]["pocket_residues"]["Phe19_pocket"],
                     "design_rule": "Phe volume or smaller. Pocket conserved across MDM2/MDMX, no major steric difference."},
    "Trp23_pocket": {"MDM2_residues": TARGETS["MDM2"]["pocket_residues"]["Trp23_pocket"],
                     "MDMX_residues": TARGETS["MDMX"]["pocket_residues"]["Trp23_pocket"],
                     "design_rule": "L-Trp invariant shared anchor. MDMX Met53 (vs MDM2 Leu54) is bulkier, pocket tighter."},
    "Leu26_pocket": {"MDM2_residues": TARGETS["MDM2"]["pocket_residues"]["Leu26_pocket"],
                     "MDMX_residues": TARGETS["MDMX"]["pocket_residues"]["Leu26_pocket"],
                     "design_rule": "Downsize to small aliphatic (Leu/Val/Abu). MDMX Met53+Pro95 dual compression vs MDM2 Leu54+His96."},
}
KNOWN_DUAL_BINDERS = [
    {"name":"PMI","type":"linear peptide","sequence":"TSFAEYWNLLSP","kd_mdm2":"low nanomolar","kd_mdmx":"low nanomolar","pmid":"34589387"},
    {"name":"PMI-M3","type":"linear peptide","sequence":"LTFLEYWAQLMQ","kd_mdm2":"low picomolar","kd_mdmx":"low picomolar","pmid":"34589387"},
    {"name":"ATSP-7041","type":"stapled peptide","kd_mdm2":"Ki ~0.9 nM","kd_mdmx":"Ki ~2.3 nM","pmid":"23946421"},
    {"name":"ALRN-6924","type":"stapled peptide (clinical)","kd_mdm2":"nanomolar","kd_mdmx":"nanomolar","pmid":"37439511"},
    {"name":"pDI","type":"linear peptide","sequence":"LTFEHYWAQLTS","kd_mdm2":"~40 nM","kd_mdmx":"sub-micromolar","pmid":"19910468"},
    {"name":"pDI6W","type":"linear peptide","sequence":"LTFEHWWAQLTS","pmid":"19910468"},
    {"name":"pDIQ","type":"linear peptide","sequence":"ETFEHWWSQLLS","kd_mdm2":"IC50 8 nM","kd_mdmx":"IC50 110 nM","pmid":"19910468"},
    {"name":"M3-2K","type":"linear peptide","sequence":"KLTFLEYWAQLMQK","pmid":"34589387"},
]
LITERATURE_REFS = [
    {"pmid":"34589387"},{"pmid":"23946421"},{"pmid":"37439511"},{"pmid":"34301750"},{"pmid":"19910468"},
]
DESIGN_STRATEGY_SUMMARY = "Trp23=L-Trp invariant anchor (MDMX Met53 tighter than MDM2 Leu54). Phe19<=Phe volume (pocket conserved). Leu26=small aliphatic (MDMX Met53+Pro95 dual compression). Natural-AA cyclic on helical FxxWxxxL geometry."
VERIFIED_PEPTIDE_COMPLEXES = {
    "MDM2": ["1T4F","1YCR","2GV2","3EQS","3G03","3IUX","3IWY","3JZR","3LNZ","3TPX","3V3B","4HFZ"],
    "MDMX": ["3DAB","3EQY","3FDO","3FE7","3FEA","3JZO","3JZP","4RXZ","5VK1","7KJN","8IA5","3JZQ"],
}
DATA_QUALITY_ALERT = "4HG7/3LBK are small-molecule complexes, not peptide. Verified: 1YCR/3V3B(MDM2), 3DAB(MDMX)."

# ===== 七层指标电池阈值（文献兜底值，最终以正对照标定为准）=====
# 来源：RFpeptides (Rettie et al., Nat Chem Biol 2025)、DeeCamp kickoff 指导
DEFAULT_THRESHOLDS = {
    "L1_plddt":            {"value": 0.8,  "operator": ">",  "unit": None,
                            "source": "RFpeptides paper (Nat Chem Biol 2025)", "confidence": "high",
                            "source_pmid": "40542165",
                            "evidence_quote": "refold with pLDDT > 0.8 and within 2.0 Å backbone r.m.s.d.",
                            "quote_verified": True,
                            "evidence_grade": "paper_explicit", "calibration_status": "pending"},
    "L2_ipsae":            {"value": 0.55, "operator": ">",  "unit": None,
                            "source": "team provisional estimate; ipSAE positive-control calibration required",
                            "confidence": "low", "evidence_grade": "team_provisional",
                            "calibration_status": "pending"},
    "L3_dg":               {"value": -10,  "operator": "<",  "unit": "kcal/mol",
                            "source": "team provisional PRODIGY cutoff", "confidence": "low",
                            "evidence_grade": "team_provisional", "calibration_status": "pending",
                            "method": "PRODIGY"},
    "L3_sc":               {"value": 0.6,  "operator": ">",  "unit": None,
                            "source": "team provisional Rosetta SC cutoff", "confidence": "low",
                            "evidence_grade": "team_provisional", "calibration_status": "pending",
                            "method": "Rosetta shape complementarity"},
    "L3_dsasa":            {"value": 400,  "operator": ">",  "unit": "A^2",
                            "source": "team provisional buried-interface cutoff", "confidence": "low",
                            "evidence_grade": "team_provisional", "calibration_status": "pending"},
    "L4_nc_term_dist":     {"value": 2.0,  "operator": "<",  "unit": "A",
                            "source": "team geometric QC; validate against head-to-tail positive controls",
                            "confidence": "medium", "evidence_grade": "team_provisional",
                            "calibration_status": "pending"},
    "L5_hotspot_coverage": {"value": 0.67, "operator": ">=", "unit": None,
                            "source": "team design-intent rule: cover at least two of three pockets",
                            "confidence": "medium", "evidence_grade": "design_rule",
                            "calibration_status": "pending",
                            "applicable_targets": ["MDM2", "MDMX"]},
    "L6_pose_rmsd":        {"value": 2.0,  "operator": "<",  "unit": "A",
                            "source": "team provisional cross-seed pose convergence cutoff",
                            "confidence": "low", "evidence_grade": "team_provisional",
                            "calibration_status": "pending", "min_seed_fraction": 0.67},
    "L7_scrmsd":           {"value": 2.0,  "operator": "<",  "unit": "A",
                            "source": "RFpeptides paper bb-RMSD<2.0A", "confidence": "high",
                            "source_pmid": "40542165",
                            "evidence_quote": "refold with pLDDT > 0.8 and within 2.0 Å backbone r.m.s.d.",
                            "quote_verified": True,
                            "evidence_grade": "paper_explicit", "calibration_status": "pending"},
}

RESEARCH_CACHE_SCHEMA_VERSION = 2
THRESHOLD_CACHE_SCHEMA_VERSION = 2
CONTROL_CALIBRATION_SCHEMA_VERSION = CALIBRATION_SCHEMA_VERSION
RESEARCH_PIPELINE_VERSION = "research-v3"
PROTOCOL_VERSIONS = {
    "rcsb_search": "v2",
    "rcsb_graphql": "v2",
    "biotite_interface": "v2",
    "threshold_research": "v2",
    "positive_negative_calibration": f"v{CONTROL_CALIBRATION_SCHEMA_VERSION}",
}


def _ensure_runtime_dirs():
    _module_attr("DATA_DIR").mkdir(parents=True, exist_ok=True)
    _module_attr("EVIDENCE_DIR").mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: str | Path, payload: dict):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(temp_name, destination)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _target_identity(target: dict) -> dict:
    structure = target.get("structure") or {}
    plan = target.get("structure_plan") or {}
    selected = plan.get("selected") or {}
    return {
        "id": target.get("id"),
        "uniprot": target.get("uniprot"),
        "gene_name": target.get("gene_name"),
        "pdb_id": structure.get("pdb_id") or selected.get("pdb_id"),
        "model_id": structure.get("model_id") or selected.get("model_id"),
        "chain": structure.get("chain") or selected.get("chain"),
        "coordinate_sha256": structure.get("coordinate_sha256") or selected.get("coordinate_sha256"),
    }


def _control_data_path(config: dict) -> Path:
    """Resolve optional positive/negative controls without changing old defaults."""
    selection = config.get("selection") or {}
    configured = (
        os.environ.get("CYCPEP_CONTROL_DATA")
        or selection.get("calibration_controls_path")
    )
    return Path(configured) if configured else _module_attr("DATA_DIR") / "_calibration_controls.json"


def _control_data_digest(config: dict) -> str | None:
    path = _control_data_path(config)
    if not path.exists() or not path.is_file():
        return None
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    return digest


def _calibration_protocol(config: dict) -> tuple[dict | None, str | None]:
    """Return the approved scoring protocol used to validate control data."""
    selection = config.get("selection") or {}
    protocol = selection.get("calibration_protocol") or config.get("calibration_protocol")
    protocol_hash = (
        selection.get("calibration_protocol_hash")
        or config.get("calibration_protocol_hash")
    )
    return protocol if isinstance(protocol, dict) else None, protocol_hash


def _cache_meta(config: dict) -> dict:
    return {
        "project_id": config.get("project_id"),
        "approved_digest": (config.get("review") or {}).get("approved_digest"),
        "project_schema_version": config.get("schema_version"),
        "required_target_ids": list(required_target_ids(config)),
        "target_identities": [_target_identity(target) for target in config.get("targets", [])],
        "research_pipeline_version": RESEARCH_PIPELINE_VERSION,
        "research_cache_schema_version": RESEARCH_CACHE_SCHEMA_VERSION,
        "threshold_cache_schema_version": THRESHOLD_CACHE_SCHEMA_VERSION,
        "control_calibration_schema_version": CONTROL_CALIBRATION_SCHEMA_VERSION,
        "control_data_path": str(_control_data_path(config)),
        "control_data_sha256": _control_data_digest(config),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "protocol_versions": PROTOCOL_VERSIONS,
    }


def _cache_mismatch_reasons(cached_meta: dict, current_meta: dict) -> list[str]:
    checks = (
        "project_id", "approved_digest", "project_schema_version",
        "required_target_ids", "target_identities", "research_pipeline_version",
        "research_cache_schema_version", "threshold_cache_schema_version",
        "control_calibration_schema_version", "control_data_path", "control_data_sha256",
        "protocol_versions",
    )
    return [key for key in checks if cached_meta.get(key) != current_meta.get(key)]


def _load_valid_cache(path: Path, config: dict) -> dict | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        EvidenceLogger.log("research", "research_cache_invalidated", {
            "cache_path": str(path), "reason": "cache_unreadable",
            "error": f"{type(exc).__name__}: {str(exc)[:160]}",
        }, phase="research")
        return None
    reasons = _cache_mismatch_reasons(payload.get("_cache_meta") or {}, _cache_meta(config))
    if reasons:
        EvidenceLogger.log("research", "research_cache_invalidated", {
            "cache_path": str(path), "reason": "approved_config_mismatch",
            "mismatched_fields": reasons,
        }, phase="research")
        return None
    return payload


def _sanitize_message(message: str) -> str:
    cleaned = str(message or "").replace("\r", " ").replace("\n", " ")
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        cleaned = cleaned.replace(api_key, "[REDACTED]")
    # Progress logs are emitted before the exception/HTTP error.  Keeping only
    # the prefix made real failures indistinguishable from an ordinary partial
    # run.  Preserve both ends while retaining a bounded, secret-redacted State
    # diagnostic suitable for Evidence/GUI display.
    limit = 480
    if len(cleaned) <= limit:
        return cleaned
    marker = " ...[truncated]... "
    head_length = 160
    tail_length = limit - head_length - len(marker)
    return f"{cleaned[:head_length]}{marker}{cleaned[-tail_length:]}"


def _error_code(stderr: str, result: dict | None = None, *, timed_out=False) -> str | None:
    text = f"{stderr} {json.dumps(result or {}, ensure_ascii=False)}".casefold()
    if timed_out or "timed out" in text or "timeout" in text:
        return "subprocess_timeout"
    if any(token in text for token in ("certificate verify failed", "ssl:", "tls", "ca certificate")):
        return "tls_ca_error"
    if any(token in text for token in ("name or service not known", "getaddrinfo", "nodename nor servname", "dns")):
        return "dns_error"
    if "429" in text or "too many requests" in text:
        return "http_429"
    if (result or {}).get("parse_error"):
        return "response_parse_error"
    if any(token in text for token in ("请求失败", "网络错误", "search failed", "request failed")):
        return "network_request_failed"
    return None


def _stage_diagnostic(status: str, stderr: str = "", result: dict | None = None) -> tuple[str | None, str | None]:
    if status == "complete":
        return None, None
    if status == "empty":
        code = _error_code(stderr, result)
        if code:
            return code, _sanitize_message(stderr) or "request failed before returning usable records"
        return "api_empty_result", "request completed but returned no usable records"
    if status == "skipped":
        return "stage_skipped", "stage disabled by runtime configuration"
    if status == "degraded_no_api_key":
        return "missing_api_key", "LLM API key not configured"
    code = _error_code(stderr, result) or "stage_incomplete"
    return code, _sanitize_message(stderr) or "stage did not produce complete output"


def _overall_run_status(stage_status: dict) -> str:
    if stage_status and all(value == "complete" for value in stage_status.values()):
        return "complete"
    if any(stage_status.get(stage) == "failed" for stage in ("rcsb_search", "rcsb_enrich", "pubmed")):
        return "failed"
    return "degraded_with_fallbacks"


def _diagnostics_for_stages(stage_status: dict, stage_context: dict) -> tuple[dict, dict]:
    codes = {}
    messages = {}
    for stage, status in stage_status.items():
        result, stderr = stage_context.get(stage, ({}, ""))
        code, message = _stage_diagnostic(status, stderr, result)
        if code:
            codes[stage] = code
        if message:
            messages[stage] = message
    return codes, messages


def _generic_l5_threshold(config: dict) -> dict:
    """Use an explicit, reviewed project rule only; never borrow MDM's 0.67."""
    selection = config.get("selection") or {}
    explicit = selection.get("hotspot_coverage_threshold")
    reviewed_sites = all(
        (target.get("binding_site") or {}).get("residues")
        and (target.get("binding_site") or {}).get("status") in {"known", "user_reviewed"}
        for target in config.get("targets", []) if target.get("required", True)
    )
    if reviewed_sites and isinstance(explicit, (int, float)):
        return normalize_threshold_entry({
            "value": explicit, "operator": ">=", "unit": None,
            "source": "approved project hotspot coverage rule",
            "evidence_grade": "design_rule", "calibration_status": "pending",
            "applicable_targets": list(required_target_ids(config)),
        })
    return normalize_threshold_entry({
        "value": None, "operator": None, "unit": None, "source": None,
        "evidence_grade": "unavailable", "calibration_status": "unavailable",
        "applicable_targets": list(required_target_ids(config)),
        "reason_unavailable": "no approved project-specific hotspot coverage threshold",
    })


def _approved_known_binders(config: dict) -> list[dict]:
    """Return binders explicitly contained in the approved project contract."""
    binders = []
    for target in config.get("targets", []):
        for raw in target.get("known_binders") or []:
            if not isinstance(raw, dict):
                continue
            binder = json.loads(json.dumps(raw))
            binder.setdefault("target_id", target["id"])
            binder["provenance"] = "approved_project_config"
            binders.append(binder)
    return binders


def _merge_known_binders(*collections: list[dict]) -> list[dict]:
    """Deduplicate trusted and extracted binders without losing provenance."""
    merged = []
    seen = set()
    for collection in collections:
        for raw in collection or []:
            if not isinstance(raw, dict):
                continue
            key = (
                str(raw.get("target_id") or raw.get("target") or "").casefold(),
                str(raw.get("sequence") or "").upper(),
                str(raw.get("name") or "").casefold(),
                str(raw.get("pdb_id") or "").upper(),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(json.loads(json.dumps(raw)))
    return merged


def default_thresholds(config: dict) -> dict:
    thresholds, _ = normalize_thresholds(
        json.loads(json.dumps(DEFAULT_THRESHOLDS)),
        applicable_targets=list(required_target_ids(config)),
    )
    if set(required_target_ids(config)) != {"MDM2", "MDMX"}:
        thresholds["L5_hotspot_coverage"] = _generic_l5_threshold(config)
    return thresholds


def _write_threshold_cache(thresholds: dict, config: dict, audit: dict | None = None):
    canonical, normalization = normalize_thresholds(thresholds)
    payload = {
        "_cache_meta": _cache_meta(config),
        "thresholds": canonical,
        "_normalization_audit": audit or normalization,
    }
    _atomic_write_json(_module_attr("THRESHOLDS_CACHE"), payload)
    return payload


def _apply_control_calibration(thresholds: dict, config: dict) -> tuple[dict, dict]:
    """Optionally replace provisional cutoffs with same-protocol control cutoffs.

    The control layer is intentionally optional: an absent or undersized
    dataset leaves the existing Research result untouched and is reported as a
    pending calibration rather than turning a successful literature run into a
    failure.
    """
    path = _control_data_path(config)
    base_summary = {
        "schema_version": CONTROL_CALIBRATION_SCHEMA_VERSION,
        "status": "not_configured",
        "path": str(path),
        "calibrated_keys": [],
        "skipped_keys": [],
    }
    if not path.exists():
        EvidenceLogger.log(
            "research", "threshold_calibration", base_summary,
            targets=list(required_target_ids(config)), phase="research",
        )
        return thresholds, base_summary

    try:
        selection = config.get("selection") or {}
        expected_protocol, expected_protocol_hash = _calibration_protocol(config)
        controls, metadata = load_control_dataset(
            path,
            project_id=config.get("project_id"),
            approved_digest=(config.get("review") or {}).get("approved_digest"),
            protocol=expected_protocol,
            protocol_hash=expected_protocol_hash,
            schema_version=CONTROL_CALIBRATION_SCHEMA_VERSION,
        )
        calibrated, audit = calibrate_thresholds(
            controls=controls,
            thresholds=thresholds,
            target_ids=required_target_ids(config),
            protocol=expected_protocol or metadata.get("protocol"),
            protocol_hash=metadata.get("protocol_hash") or expected_protocol_hash,
            max_false_positive_rate=float(
                selection.get("calibration_max_false_positive_rate", 0.05)
            ),
            min_positive_recall=float(
                selection.get("calibration_min_positive_recall", 0.50)
            ),
            min_negative_controls=int(selection.get("calibration_min_negative_controls", 10)),
            min_positive_controls=int(selection.get("calibration_min_positive_controls", 3)),
        )
        calibrated, normalization = normalize_thresholds(calibrated)
        summary = {
            **audit,
            "path": str(path),
            "project_id": config.get("project_id"),
            "approved_digest": (config.get("review") or {}).get("approved_digest"),
            "normalization": normalization,
        }
        artifact = {
            "_cache_meta": _cache_meta(config),
            "source_path": str(path),
            "source_metadata": metadata,
            "audit": summary,
        }
        _atomic_write_json(_module_attr("DATA_DIR") / "_threshold_calibration.json", artifact)
        EvidenceLogger.log(
            "research", "threshold_calibration", summary,
            targets=list(required_target_ids(config)), phase="research",
        )
        return calibrated, summary
    except ControlDataError as exc:
        summary = {
            **base_summary,
            "status": "invalidated",
            "reason": str(exc),
        }
        EvidenceLogger.log(
            "research", "threshold_calibration", summary,
            targets=list(required_target_ids(config)), phase="research",
        )
        return thresholds, summary
    except (OSError, TypeError, ValueError) as exc:
        summary = {
            **base_summary,
            "status": "failed",
            "reason": f"{type(exc).__name__}: {str(exc)[:240]}",
        }
        EvidenceLogger.error(
            "research", "threshold_calibration_failed", summary["reason"],
            recovery="retain literature/provisional thresholds",
        )
        return thresholds, summary
    except Exception as exc:
        if os.environ.get("CYCPEP_STRICT_CALIBRATION") == "1" or os.environ.get("CI") == "true":
            raise
        summary = {
            **base_summary,
            "status": "failed_unexpected",
            "reason": f"{type(exc).__name__}: {str(exc)[:240]}",
        }
        EvidenceLogger.error(
            "research", "threshold_calibration_unexpected_error", summary["reason"],
            recovery="retain literature/provisional thresholds; inspect the exception",
        )
        return thresholds, summary


def _run_script(script_name, input_data=None, extra_args=None):
    script_path = SCRIPTS_DIR / script_name
    if not script_path.exists():
        raise FileNotFoundError(f"Script not found: {script_path}")
    python_exe = sys.executable
    t0 = time.time()
    cmd = [python_exe, "-m", f"scripts.{script_name.replace('.py', '')}"]
    if extra_args:
        cmd.extend(extra_args)
    try:
        proc = subprocess.run(
            cmd,
            input=json.dumps(input_data) if input_data else None,
            capture_output=True, text=True, timeout=600, cwd=str(ROOT),
            env={**os.environ},
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.time() - t0
        stderr = _sanitize_message(exc.stderr or f"{script_name} timed out after 600 seconds")
        return {"timed_out": True}, stderr, 124, duration, ""
    duration = time.time() - t0
    stdout = proc.stdout.strip()
    stderr = _sanitize_message(proc.stderr.strip())
    exit_code = proc.returncode
    output_hash = hashlib.md5(stdout.encode()).hexdigest()[:12] if stdout else ""
    try:
        result = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        result = {"stdout": stdout[:1000], "parse_error": True}
    return result, stderr, exit_code, duration, output_hash




def run(state=None, force_recompute=False, skip_pipeline=False, project_config=None):
    """Run the Research pipeline.

    ``project_config`` optionally injects an explicit approved project config
    (PR5).  When omitted, the environment-selected default project is used.
    The override lasts only for this call, so sequential multi-project runs
    stay safe in one process.
    """
    global _injected_project_config
    previous = _injected_project_config
    if project_config is not None:
        _injected_project_config = project_config
    try:
        return _run_impl(state=state, force_recompute=force_recompute, skip_pipeline=skip_pipeline)
    finally:
        _injected_project_config = previous


def _run_impl(state=None, force_recompute=False, skip_pipeline=False):
    _ensure_runtime_dirs()
    assert_project_approved(_cfg())
    # The newly approved config is authoritative even when state.json predates it.
    state = State.sync_project_config(_cfg())

    pipeline_runner = _module_attr("_run_pipeline") if _module_attr("IS_MDM_REFERENCE") else _module_attr("_run_generic_pipeline")
    if force_recompute:
        pipeline_result = pipeline_runner()
    else:
        pipeline_result = _load_valid_cache(_module_attr("CACHE_PATH"), _cfg())
        if pipeline_result is not None:
            print(f"[research] Using cache: {_module_attr('CACHE_PATH')}")
        else:
            pipeline_result = pipeline_runner()

    thresholds, threshold_normalization = normalize_thresholds(
        pipeline_result.get("thresholds") or default_thresholds(_cfg())
    )
    pipeline_result["thresholds"] = thresholds
    threshold_cache = None if force_recompute else _load_valid_cache(_module_attr("THRESHOLDS_CACHE"), _cfg())
    if force_recompute or threshold_cache is None:
        _write_threshold_cache(
            thresholds,
            _cfg(),
            {
                "normalization": threshold_normalization,
                "control_calibration": (
                    pipeline_result.get("_pipeline_meta", {}).get("control_calibration", {})
                ),
            },
        )

    configured_targets = {
        target["id"]: {key: value for key, value in target.items() if key != "id"}
        for target in _cfg().get("targets", [])
    }
    pipeline_targets = pipeline_result.get("targets", {})
    for name in list(configured_targets):
        info = pipeline_targets.get(name)
        if isinstance(info, dict):
            configured_targets[name].update({key: value for key, value in info.items() if key != "id"})

    if not _module_attr("IS_MDM_REFERENCE"):
        result = {
            "targets": configured_targets,
            "pocket_differences": pipeline_result.get("pocket_differences", {}),
            "known_binders": pipeline_result.get("known_binders", []),
            "known_dual_binders": pipeline_result.get("known_binders", []),
            "known_binder_source": pipeline_result.get("known_binder_source", "none_found"),
            "design_strategy_summary": pipeline_result.get("design_strategy_summary", ""),
            "data_quality_alert": pipeline_result.get("data_quality_alert", ""),
            "research_pipeline_meta": pipeline_result.get("_pipeline_meta", {}),
        }
        State.update(result)
        sync = State.sync_thresholds_from_cache(_module_attr("THRESHOLDS_CACHE"))
        result["thresholds"] = sync["state"].get("thresholds", {})
        meta = result["research_pipeline_meta"]
        EvidenceLogger.research_complete(
            hotspot_analysis={
                "pdb_list": meta.get("dynamic_pdb_list", []),
                "counts_by_target": meta.get("counts_by_target", {}),
                "pockets": result["pocket_differences"],
                "stage_status": meta.get("stage_status", {}),
                "run_status": meta.get("run_status", "unknown"),
            },
            known_binders=result["known_binders"],
            refs=pipeline_result.get("literature_refs", []),
        )
        return result

    result = {
        "targets": configured_targets,
        "pocket_differences": pipeline_result.get("pocket_differences", POCKET_DIFFERENCES),
        "known_dual_binders": pipeline_result.get("known_dual_binders", KNOWN_DUAL_BINDERS),
        "known_binder_source": pipeline_result.get("known_binder_source", "curated_fallback"),
        "design_strategy_summary": pipeline_result.get("design_strategy_summary", DESIGN_STRATEGY_SUMMARY),
        "data_quality_alert": pipeline_result.get("data_quality_alert", DATA_QUALITY_ALERT),
        "research_pipeline_meta": pipeline_result.get("_pipeline_meta", {}),
    }
    State.update(result)
    sync = State.sync_thresholds_from_cache(_module_attr("THRESHOLDS_CACHE"))
    result["thresholds"] = sync["state"].get("thresholds", {})

    # 用动态数据构建 hotspot_analysis
    meta = pipeline_result.get("_pipeline_meta", {})
    pocket_diff = result["pocket_differences"]
    pdb_list = meta.get("dynamic_pdb_list", [])
    if not pdb_list:
        pdb_list = VERIFIED_PEPTIDE_COMPLEXES["MDM2"] + VERIFIED_PEPTIDE_COMPLEXES["MDMX"]

    hotspot_analysis = {
        "pdb_list": pdb_list,
        "n_mdm2_peptide_complexes": meta.get("n_mdm2_structures", len(VERIFIED_PEPTIDE_COMPLEXES["MDM2"])),
        "n_mdmx_peptide_complexes": meta.get("n_mdmx_structures", len(VERIFIED_PEPTIDE_COMPLEXES["MDMX"])),
        "method": pocket_diff.get("_method", ""),
        "pocket_source": meta.get("pocket_source", "constant"),
        "pockets": pocket_diff,
        "data_quality_alert": DATA_QUALITY_ALERT,
        "stage_status": meta.get("stage_status", {}),
        "run_status": meta.get("run_status", "unknown"),
    }
    EvidenceLogger.research_complete(
        hotspot_analysis=hotspot_analysis,
        known_binders=result["known_dual_binders"],
        refs=LITERATURE_REFS,
    )
    return result


def recompute():
    return run(force_recompute=True)


if __name__ == "__main__":
    out = run()
    n = len(out.get("known_dual_binders", []))
    print(f"[research] State updated. {n} dual binders. Phase={State.load().get('phase')}")

