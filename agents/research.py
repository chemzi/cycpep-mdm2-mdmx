"""
Research Agent - MDM2/MDMX 靶点调研管线
8 步: RCSB Search -> GraphQL Enrich -> biotite interface -> aggregate pockets ->
      superpose analyze -> PubMed -> LLM extract -> threshold evidence
每步挂 EvidenceLogger tool_trace。biotite 失败时自动回退到预置常量。
"""

import json, os, subprocess, sys, time, hashlib, tempfile
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"

from data_layer import State, EvidenceLogger, DATA_DIR, EVIDENCE_DIR
from project_config import load_project_config, required_target_ids, target_slug
from target_bootstrap import assert_project_approved
from threshold_contract import normalize_threshold_entry, normalize_thresholds
from threshold_calibration import (
    CALIBRATION_SCHEMA_VERSION,
    ControlDataError,
    calibrate_thresholds,
    load_control_dataset,
)

PROJECT_CONFIG = load_project_config()
PROJECT_TARGET_IDS = tuple(target["id"] for target in PROJECT_CONFIG["targets"])
IS_MDM_REFERENCE = set(PROJECT_TARGET_IDS) == {"MDM2", "MDMX"}
CACHE_PATH = (
    DATA_DIR / "_research_cache.json" if IS_MDM_REFERENCE
    else DATA_DIR / f"_research_cache_{target_slug(PROJECT_CONFIG['project_id'])}.json"
)

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
THRESHOLDS_CACHE = DATA_DIR / "_thresholds_cache.json"

RESEARCH_CACHE_SCHEMA_VERSION = 2
THRESHOLD_CACHE_SCHEMA_VERSION = 2
CONTROL_CALIBRATION_SCHEMA_VERSION = CALIBRATION_SCHEMA_VERSION
RESEARCH_PIPELINE_VERSION = "research-v2"
PROTOCOL_VERSIONS = {
    "rcsb_search": "v2",
    "rcsb_graphql": "v2",
    "biotite_interface": "v2",
    "threshold_research": "v2",
    "positive_negative_calibration": f"v{CONTROL_CALIBRATION_SCHEMA_VERSION}",
}


def _ensure_runtime_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)


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
    return Path(configured) if configured else DATA_DIR / "_calibration_controls.json"


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
    return cleaned[:240]


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


def _default_thresholds(config: dict) -> dict:
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
    _atomic_write_json(THRESHOLDS_CACHE, payload)
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
        _atomic_write_json(DATA_DIR / "_threshold_calibration.json", artifact)
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


def _build_dynamic_pockets(aggregate_result, superpose_result):
    """从 aggregate + superpose 输出构建 pocket_differences。
    
    如果 biotite 成功计算了界面残基，用动态数据；
    否则返回 None（调用方回退到常量）。
    """
    n_mdm2 = aggregate_result.get("n_mdm2_structures", 0)
    n_mdmx = aggregate_result.get("n_mdmx_structures", 0)
    if n_mdm2 == 0 or n_mdmx == 0:
        return None  # 双靶动态结论要求两侧都有结构支持

    # 从 aggregate 提取每靶点的口袋残基
    mdm2_agg = aggregate_result.get("MDM2", {})
    mdmx_agg = aggregate_result.get("MDMX", {})
    
    # 用 consensus 残基（如果有的话），否则用 pocket_residues 参考
    def _extract_residues(agg, pocket_name):
        consensus = agg.get("pocket_consensus", {}).get(pocket_name, [])
        if consensus:
            # 格式 "A:58GLY" -> "Gly58"
            cleaned = []
            for r in consensus:
                # 兼容新版 "58GLY" 与旧版 "A:58GLY"。
                import re
                m = re.search(r':?(-?\d+)([A-Z]{3})$', r.upper())
                if m:
                    res_id, res_name = m.group(1), m.group(2)
                    cleaned.append(f"{res_name.capitalize()}{res_id}")
            return cleaned if cleaned else agg.get("pocket_residues", {}).get(pocket_name, [])
        return agg.get("pocket_residues", {}).get(pocket_name, [])

    # 从 superpose 提取方法信息
    rmsd = superpose_result.get("ca_rmsd_A", "?") if superpose_result else "?"
    n_ca = superpose_result.get("n_ca_atoms_superposed", "?") if superpose_result else "?"
    method = f"biotite heavy-atom<4A, {n_mdm2}+{n_mdmx} structures, CA RMSD {rmsd}A/{n_ca} residues"

    return {
        "_method": method,
        "_source": "dynamic_biotite",
        "Phe19_pocket": {
            "MDM2_residues": _extract_residues(mdm2_agg, "Phe19_pocket"),
            "MDMX_residues": _extract_residues(mdmx_agg, "Phe19_pocket"),
            "design_rule": "Phe volume or smaller. Pocket conserved across MDM2/MDMX, no major steric difference.",
        },
        "Trp23_pocket": {
            "MDM2_residues": _extract_residues(mdm2_agg, "Trp23_pocket"),
            "MDMX_residues": _extract_residues(mdmx_agg, "Trp23_pocket"),
            "design_rule": "L-Trp invariant shared anchor. MDMX Met53 (vs MDM2 Leu54) is bulkier, pocket tighter.",
        },
        "Leu26_pocket": {
            "MDM2_residues": _extract_residues(mdm2_agg, "Leu26_pocket"),
            "MDMX_residues": _extract_residues(mdmx_agg, "Leu26_pocket"),
            "design_rule": "Downsize to small aliphatic (Leu/Val/Abu). MDMX Met53+Pro95 dual compression vs MDM2 Leu54+His96.",
        },
        "_superpose": {
            "rmsd_A": rmsd,
            "n_ca_atoms": n_ca,
            "sasa": superpose_result.get("sasa", {}) if superpose_result else {},
        } if superpose_result else {},
    }


def _run_pipeline():
    _ensure_runtime_dirs()
    skip_heavy = os.environ.get("SKIP_BIOTITE", "").lower() in ("1", "true", "yes")
    stage_status = {}
    stage_context = {}
    fallbacks = []

    # ===== Step 1: RCSB Search =====
    print("[research] Step 1/8: RCSB Search API...")
    sr, se, sc, sd, sh = _run_script("search_pdb.py")
    stage_status["rcsb_search"] = (
        "complete" if sc == 0 and sr.get("run_status") == "complete"
        else "empty" if sc == 0 else "failed"
    )
    stage_context["rcsb_search"] = (sr, se)
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "rcsb_search_api", "tool_version": "v2",
        "output_hash": sh, "exit_code": sc, "duration_sec": round(sd, 1),
        "stdout_snippet": (
            f"status={stage_status['rcsb_search']} "
            f"MDM2={sr.get('n_mdm2',0)} MDMX={sr.get('n_mdmx',0)}"
        ),
    }, targets=["both"], phase="research")

    # ===== Step 2: GraphQL Enrich =====
    print("[research] Step 2/8: RCSB GraphQL...")
    er, ee, ec, ed, eh = _run_script("enrich_pdb.py", sr)
    n_peptide = er.get("n_peptide_complexes", 0)
    stage_status["rcsb_enrich"] = "complete" if ec == 0 and n_peptide > 0 else "empty" if ec == 0 else "failed"
    stage_context["rcsb_enrich"] = (er, ee)
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "rcsb_graphql_api", "output_hash": eh, "exit_code": ec,
        "duration_sec": round(ed, 1),
        "stdout_snippet": f"status={stage_status['rcsb_enrich']} peptide_complexes={n_peptide}",
    }, targets=["both"], phase="research")

    # ===== Steps 3-5: biotite =====
    pocket_differences = POCKET_DIFFERENCES  # 默认常量
    dynamic_pdb_list = []  # 动态 PDB 列表（来自 enrich）
    dynamic_pdb_by_target = {"MDM2": [], "MDMX": []}
    n_mdm2_structures = 0
    n_mdmx_structures = 0

    # 从 enrich 提取动态 PDB 列表
    for entry in er.get("peptide_complexes", []):
        target_name = entry.get("target")
        if (
            target_name in dynamic_pdb_by_target
            and entry["pdb_id"] not in dynamic_pdb_by_target[target_name]
        ):
            dynamic_pdb_by_target[target_name].append(entry["pdb_id"])
        if entry["pdb_id"] not in dynamic_pdb_list:
            dynamic_pdb_list.append(entry["pdb_id"])
        if entry.get("target") == "MDM2":
            n_mdm2_structures += 1
        elif entry.get("target") == "MDMX":
            n_mdmx_structures += 1

    if skip_heavy:
        print("[research] Steps 3-5: skipped (SKIP_BIOTITE=1)")
        stage_status["interface"] = "skipped"
        stage_status["aggregate"] = "skipped"
        stage_status["superposition"] = "skipped"
        stage_context.update({name: ({}, "") for name in ("interface", "aggregate", "superposition")})
        fallbacks.append("curated_mdm_pocket_definitions")
    else:
        # Step 3: biotite interface
        print("[research] Step 3/8: biotite interface...")
        iface_result = {"with_interface": []}
        try:
            ir, ie2, ic, id_, ih = _run_script("compute_interface.py", er)
            n_iface = ir.get("n_with_interface", 0)
            stage_status["interface"] = (
                "complete" if ic == 0 and n_iface > 0 else "empty" if ic == 0 else "failed"
            )
            stage_context["interface"] = (ir, ie2)
            EvidenceLogger.log("research", "tool_call", {
                "tool_name": "biotite", "output_hash": ih, "exit_code": ic,
                "duration_sec": round(id_, 1),
                "stdout_snippet": f"status={stage_status['interface']} with_interface={n_iface}",
            }, targets=["both"], phase="research")
            iface_result = ir
        except Exception as e:
            EvidenceLogger.error("research", "tool_failure", f"biotite: {e}", recovery="fallback")
            iface_result = {"with_interface": []}
            stage_status["interface"] = "failed"
            stage_context["interface"] = ({}, str(e))

        # Step 4: aggregate pockets
        print("[research] Step 4/8: aggregate pockets...")
        pr, pe2, pc, pd_, ph = _run_script("aggregate_pockets.py", iface_result)
        n_agg_mdm2 = pr.get("n_mdm2_structures", 0)
        n_agg_mdmx = pr.get("n_mdmx_structures", 0)
        stage_status["aggregate"] = (
            "complete" if pc == 0 and n_agg_mdm2 > 0 and n_agg_mdmx > 0
            else "empty" if pc == 0 else "failed"
        )
        stage_context["aggregate"] = (pr, pe2)
        EvidenceLogger.log("research", "tool_call", {
            "tool_name": "aggregate_pockets", "output_hash": ph, "exit_code": pc,
            "duration_sec": round(pd_, 1),
            "stdout_snippet": (
                f"status={stage_status['aggregate']} "
                f"MDM2={n_agg_mdm2}struct MDMX={n_agg_mdmx}struct"
            ),
        }, targets=["both"], phase="research")

        # Step 5: superpose
        print("[research] Step 5/8: superposition...")
        spr = {}
        try:
            spr, spe2, spc, spd_, sph = _run_script("superpose_analyze.py", pr)
            stage_status["superposition"] = (
                "complete" if spc == 0 and spr.get("ca_rmsd_A") is not None
                else "empty" if spc == 0 else "failed"
            )
            stage_context["superposition"] = (spr, spe2)
            EvidenceLogger.log("research", "tool_call", {
                "tool_name": "biotite_superimpose", "output_hash": sph, "exit_code": spc,
                "duration_sec": round(spd_, 1),
                "stdout_snippet": (
                    f"status={stage_status['superposition']} "
                    f"rmsd={spr.get('ca_rmsd_A','?')}A "
                    f"n_ca={spr.get('n_ca_atoms_superposed','?')}"
                ),
            }, targets=["both"], phase="research")
        except Exception as e:
            EvidenceLogger.error("research", "tool_failure", f"superpose: {e}", recovery="fallback")
            stage_status["superposition"] = "failed"
            stage_context["superposition"] = ({}, str(e))

        # 用动态数据构建 pocket_differences
        dynamic = _build_dynamic_pockets(pr, spr)
        if dynamic:
            pocket_differences = dynamic
            n_mdm2_structures = n_agg_mdm2
            n_mdmx_structures = n_agg_mdmx
            print(f"[research] Using dynamic pockets: MDM2={n_mdm2_structures}struct MDMX={n_mdmx_structures}struct")
        else:
            print("[research] biotite produced no interface data, using constant pockets")
            fallbacks.append("curated_mdm_pocket_definitions")

    # ===== Step 6: PubMed =====
    print("[research] Step 6/8: PubMed...")
    pmr, pme, pmc, pmd_, pmh = _run_script("pubmed_search.py")
    stage_status["pubmed"] = "complete" if pmc == 0 and pmr.get("n_total", 0) > 0 else "empty" if pmc == 0 else "failed"
    stage_context["pubmed"] = (pmr, pme)
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "pubmed_eutils", "output_hash": pmh, "exit_code": pmc,
        "duration_sec": round(pmd_, 1),
        "stdout_snippet": f"status={stage_status['pubmed']} n_papers={pmr.get('n_total',0)}",
    }, targets=["both"], phase="research")

    # ===== Step 7: LLM extract =====
    print("[research] Step 7/8: LLM extract (concurrent)...")
    llm_binders = KNOWN_DUAL_BINDERS
    binder_source = "curated_fallback"
    try:
        lr, le2, lc, ld_, lh = _run_script("llm_extract.py", pmr, extra_args=["--concurrency", "3"])
        if lr and "error" not in lr:
            extracted = lr.get("known_binders", [])
            if extracted:
                llm_binders = extracted
                binder_source = "llm_extracted"
                print(f"[research] LLM found {len(extracted)} binders from {lr.get('n_papers_processed',0)} papers")
        raw_llm_status = lr.get("run_status", "failed")
        stage_status["llm_extract"] = (
            "degraded_no_api_key" if raw_llm_status == "degraded_no_api_key"
            else "complete" if lc == 0 and raw_llm_status == "complete"
            else "failed"
        )
        stage_context["llm_extract"] = (lr, le2)
        if binder_source == "curated_fallback":
            fallbacks.append("curated_mdm_binders")
        EvidenceLogger.log("research", "tool_call", {
            "tool_name": "llm_extract_concurrent",
            "tool_version": lr.get("llm_model", "unknown") if isinstance(lr, dict) else "unknown",
            "output_hash": lh, "exit_code": lc, "duration_sec": round(ld_, 1),
            "stdout_snippet": (
                f"status={stage_status['llm_extract']} binders={len(llm_binders)} "
                f"papers={lr.get('n_papers_processed',0) if isinstance(lr, dict) else '?'}"
            ),
        }, targets=["both"], phase="research")
    except Exception as e:
        EvidenceLogger.error("research", "tool_failure", f"LLM: {e}", recovery="fallback to constants")
        stage_status["llm_extract"] = "failed"
        stage_context["llm_extract"] = ({}, str(e))
        fallbacks.append("curated_mdm_binders")

    # ===== Step 8: 阈值文献检索 =====
    print("[research] Step 8/8: threshold literature research...")
    thresholds = _default_thresholds(PROJECT_CONFIG)
    try:
        tr, te2, tc, td_, th = _run_script("threshold_research.py", extra_args=["--concurrency", "4"])
        lit, threshold_normalization = normalize_thresholds(tr.get("metric_battery", {}))
        threshold_meta = tr.get("_meta", {})
        n_found = threshold_meta.get("n_auto_usable", 0)
        raw_threshold_status = threshold_meta.get("run_status", "failed")
        stage_status["threshold_research"] = (
            "degraded_no_api_key" if raw_threshold_status == "degraded_no_llm"
            else "empty" if raw_threshold_status == "degraded_empty_results"
            else "complete" if tc == 0 and raw_threshold_status == "complete"
            else "failed"
        )
        stage_context["threshold_research"] = (tr, te2)
        # 仅自动采用已核验 PMID、摘要原句和 paper_explicit 证据的阈值。
        for layer, info in lit.items():
            if info.get("auto_usable") and info.get("value") is not None:
                thresholds[layer] = {
                    **{
                        key: value
                        for key, value in thresholds.get(layer, {}).items()
                        if key in ("method", "min_seed_fraction")
                    },
                    "value": info["value"],
                    "operator": info.get("operator", ">"),
                    "unit": info.get("unit"),
                    "source": f"PubMed PMID {info.get('source_pmid')}",
                    "confidence": info.get("confidence", "medium"),
                    "source_pmid": info.get("source_pmid"),
                    "evidence_quote": info.get("evidence_quote"),
                    "evidence_grade": info.get("evidence_grade"),
                    "quote_verified": True,
                    "calibration_status": "pending",
                    "applicable_targets": list(required_target_ids(PROJECT_CONFIG)),
                }
        thresholds, final_normalization = normalize_thresholds(thresholds)
        EvidenceLogger.log("research", "tool_call", {
            "tool_name": "threshold_research", "output_hash": th, "exit_code": tc,
            "duration_sec": round(td_, 1),
            "stdout_snippet": (
                f"status={stage_status['threshold_research']} "
                f"verified_thresholds={n_found}/{len(lit)}"
            ),
        }, targets=["both"], phase="research")
        print(f"[research] Thresholds: {n_found} verified literature overrides")
    except Exception as e:
        EvidenceLogger.error("research", "tool_failure", f"threshold_research: {e}",
                             recovery="fallback to DEFAULT_THRESHOLDS")
        print(f"[research] threshold_research failed, using defaults: {e}")
        stage_status["threshold_research"] = "failed"
        stage_context["threshold_research"] = ({}, str(e))
        fallbacks.append("provisional_default_thresholds")

    if stage_status.get("threshold_research") != "complete":
        fallbacks.append("provisional_default_thresholds")
    thresholds, control_calibration = _apply_control_calibration(thresholds, PROJECT_CONFIG)
    _write_threshold_cache(thresholds, PROJECT_CONFIG, {
        "literature_input": threshold_normalization if "threshold_normalization" in locals() else {},
        "final": final_normalization if "final_normalization" in locals() else {},
        "control_calibration": control_calibration,
    })
    if not THRESHOLDS_CACHE.exists():
        _write_threshold_cache(thresholds, PROJECT_CONFIG)

    # ===== 组装结果 =====
    stage_error_code, error_message = _diagnostics_for_stages(stage_status, stage_context)
    result = {
        "targets": TARGETS.copy(),
        "pocket_differences": pocket_differences,
        "known_dual_binders": llm_binders,
        "known_binder_source": binder_source,
        "design_strategy_summary": DESIGN_STRATEGY_SUMMARY,
        "data_quality_alert": DATA_QUALITY_ALERT,
        "literature_refs": LITERATURE_REFS,
        "thresholds": thresholds,
        "_pipeline_meta": {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "pocket_source": pocket_differences.get("_source", "constant"),
            "n_pdb_complexes": len(dynamic_pdb_list),
            "n_mdm2_structures": n_mdm2_structures,
            "n_mdmx_structures": n_mdmx_structures,
            "dynamic_pdb_list": (
                dynamic_pdb_by_target["MDM2"][:10]
                + dynamic_pdb_by_target["MDMX"][:10]
            ),
            "dynamic_pdb_by_target": dynamic_pdb_by_target,
            "stage_status": stage_status,
            "stage_error_code": stage_error_code,
            "error_message": error_message,
            "fallbacks_used": list(dict.fromkeys(fallbacks)),
            "control_calibration": control_calibration,
            "run_status": _overall_run_status(stage_status),
        },
        "_cache_meta": _cache_meta(PROJECT_CONFIG),
    }
    _atomic_write_json(CACHE_PATH, result)
    print(f"[research] Pipeline done. pocket_source={result['_pipeline_meta']['pocket_source']}")
    return result


def _run_generic_pipeline():
    """Target-configured research path without MDM-specific biological fallbacks."""
    _ensure_runtime_dirs()
    target_ids = list(PROJECT_TARGET_IDS)
    stage_status = {}
    stage_context = {}
    fallbacks = []

    sr, se, sc, sd, sh = _run_script("search_pdb.py")
    stage_status["rcsb_search"] = "complete" if sc == 0 and sr.get("run_status") == "complete" else "empty" if sc == 0 else "failed"
    stage_context["rcsb_search"] = (sr, se)
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "rcsb_search_api", "output_hash": sh, "exit_code": sc,
        "duration_sec": round(sd, 1), "stdout_snippet": str(sr.get("counts_by_target", {})),
    }, targets=target_ids, phase="research")

    er, ee, ec, ed, eh = _run_script("enrich_pdb.py", sr)
    stage_status["rcsb_enrich"] = "complete" if ec == 0 and er.get("n_peptide_complexes", 0) > 0 else "empty" if ec == 0 else "failed"
    stage_context["rcsb_enrich"] = (er, ee)
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "rcsb_graphql_api", "output_hash": eh, "exit_code": ec,
        "duration_sec": round(ed, 1), "stdout_snippet": str(er.get("counts_by_target", {})),
    }, targets=target_ids, phase="research")

    aggregate = {"results_by_target": {}, "counts_by_target": {}}
    if os.environ.get("SKIP_BIOTITE", "").lower() in ("1", "true", "yes"):
        stage_status["interface"] = "skipped"
        stage_status["aggregate"] = "skipped"
        stage_context.update({name: ({}, "") for name in ("interface", "aggregate")})
        fallbacks.append("interface_aggregation_omitted")
    else:
        try:
            ir, ie, ic, id_, ih = _run_script("compute_interface.py", er)
            stage_status["interface"] = "complete" if ic == 0 and ir.get("n_with_interface", 0) else "empty" if ic == 0 else "failed"
            stage_context["interface"] = (ir, ie)
            EvidenceLogger.log("research", "tool_call", {
                "tool_name": "biotite", "output_hash": ih, "exit_code": ic,
                "duration_sec": round(id_, 1),
                "stdout_snippet": f"interfaces={ir.get('n_with_interface', 0)}",
            }, targets=target_ids, phase="research")
            aggregate, ae, ac, ad, ah = _run_script("aggregate_pockets.py", ir)
            has_aggregate = bool(aggregate.get("results_by_target") or aggregate.get("counts_by_target"))
            stage_status["aggregate"] = "complete" if ac == 0 and has_aggregate else "empty" if ac == 0 else "failed"
            stage_context["aggregate"] = (aggregate, ae)
            EvidenceLogger.log("research", "tool_call", {
                "tool_name": "aggregate_pockets", "output_hash": ah, "exit_code": ac,
                "duration_sec": round(ad, 1),
                "stdout_snippet": str(aggregate.get("counts_by_target", {})),
            }, targets=target_ids, phase="research")
        except Exception as exc:
            stage_status["interface"] = "failed"
            stage_status["aggregate"] = "failed"
            stage_context["interface"] = ({}, str(exc))
            stage_context["aggregate"] = ({}, str(exc))
            fallbacks.append("interface_aggregation_omitted")
            EvidenceLogger.error("research", "tool_failure", str(exc), recovery="continue without interface aggregation")

    pmr, pme, pc, pd, ph = _run_script("pubmed_search.py")
    stage_status["pubmed"] = "complete" if pc == 0 and pmr.get("n_total", 0) > 0 else "empty" if pc == 0 else "failed"
    stage_context["pubmed"] = (pmr, pme)
    EvidenceLogger.log("research", "tool_call", {
        "tool_name": "pubmed_eutils", "output_hash": ph, "exit_code": pc,
        "duration_sec": round(pd, 1), "stdout_snippet": f"papers={pmr.get('n_total', 0)}",
    }, targets=target_ids, phase="research")

    known_binders = []
    try:
        lr, le, lc, ld, lh = _run_script("llm_extract.py", pmr, extra_args=["--concurrency", "3"])
        known_binders = lr.get("known_binders", [])
        raw_llm_status = lr.get("run_status", "failed")
        stage_status["llm_extract"] = (
            "degraded_no_api_key" if raw_llm_status == "degraded_no_api_key"
            else "complete" if lc == 0 and raw_llm_status == "complete"
            else "failed"
        )
        stage_context["llm_extract"] = (lr, le)
        if not known_binders:
            fallbacks.append("no_binder_fallback")
        EvidenceLogger.log("research", "tool_call", {
            "tool_name": "llm_extract", "output_hash": lh, "exit_code": lc,
            "duration_sec": round(ld, 1), "stdout_snippet": f"binders={len(known_binders)}",
        }, targets=target_ids, phase="research")
    except Exception as exc:
        stage_status["llm_extract"] = "failed"
        stage_context["llm_extract"] = ({}, str(exc))
        fallbacks.append("no_binder_fallback")
        EvidenceLogger.error("research", "tool_failure", str(exc), recovery="no fabricated binder fallback")

    thresholds = _default_thresholds(PROJECT_CONFIG)
    try:
        tr, te, tc, td, thash = _run_script("threshold_research.py", extra_args=["--concurrency", "4"])
        literature_thresholds, threshold_normalization = normalize_thresholds(tr.get("metric_battery", {}))
        for key, info in literature_thresholds.items():
            if info.get("auto_usable") and info.get("value") is not None:
                thresholds[key] = {
                    **{name: value for name, value in thresholds.get(key, {}).items()
                       if name in ("method", "min_seed_fraction")},
                    "value": info["value"], "operator": info.get("operator", ">"),
                    "unit": info.get("unit"), "source": f"PubMed PMID {info.get('source_pmid')}",
                    "source_pmid": info.get("source_pmid"), "evidence_quote": info.get("evidence_quote"),
                    "evidence_grade": "paper_explicit", "quote_verified": True,
                    "calibration_status": "pending",
                    "applicable_targets": target_ids,
                }
        raw_threshold_status = tr.get("_meta", {}).get("run_status", "failed")
        stage_status["threshold_research"] = (
            "degraded_no_api_key" if raw_threshold_status == "degraded_no_llm"
            else "empty" if raw_threshold_status == "degraded_empty_results"
            else "complete" if tc == 0 and raw_threshold_status == "complete"
            else "failed"
        )
        stage_context["threshold_research"] = (tr, te)
        thresholds, final_normalization = normalize_thresholds(thresholds)
        EvidenceLogger.log("research", "tool_call", {
            "tool_name": "threshold_research", "output_hash": thash, "exit_code": tc,
            "duration_sec": round(td, 1),
            "stdout_snippet": f"usable={tr.get('_meta', {}).get('n_auto_usable', 0)}",
        }, targets=target_ids, phase="research")
    except Exception as exc:
        stage_status["threshold_research"] = "failed"
        stage_context["threshold_research"] = ({}, str(exc))
        fallbacks.append("provisional_default_thresholds")

    if stage_status.get("threshold_research") != "complete":
        fallbacks.append("provisional_default_thresholds")
    thresholds, control_calibration = _apply_control_calibration(thresholds, PROJECT_CONFIG)
    _write_threshold_cache(thresholds, PROJECT_CONFIG, {
        "literature_input": threshold_normalization if "threshold_normalization" in locals() else {},
        "final": final_normalization if "final_normalization" in locals() else {},
        "control_calibration": control_calibration,
    })
    if not THRESHOLDS_CACHE.exists():
        _write_threshold_cache(thresholds, PROJECT_CONFIG)
        EvidenceLogger.error("research", "tool_failure", str(exc), recovery="provisional thresholds remain non-clearable")

    dynamic_pdb_list = [row.get("pdb_id") for row in er.get("peptide_complexes", []) if row.get("pdb_id")]
    stage_error_code, error_message = _diagnostics_for_stages(stage_status, stage_context)
    result = {
        "project_id": PROJECT_CONFIG["project_id"],
        "targets": {target["id"]: target for target in PROJECT_CONFIG["targets"]},
        "pocket_differences": {
            "_source": "dynamic_interface_aggregation",
            "targets": aggregate.get("results_by_target", {}),
        },
        "known_binders": known_binders,
        "known_dual_binders": known_binders,
        "known_binder_source": "llm_extracted" if known_binders else "none_found",
        "design_strategy_summary": "Target-configured; derive design constraints from retrieved epitope evidence.",
        "data_quality_alert": "No MDM-specific constants were used as fallback.",
        "literature_refs": pmr.get("pmids", []),
        "thresholds": thresholds,
        "_pipeline_meta": {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "dynamic_pdb_list": dynamic_pdb_list,
            "counts_by_target": er.get("counts_by_target", {}),
            "stage_status": stage_status,
            "stage_error_code": stage_error_code,
            "error_message": error_message,
            "fallbacks_used": list(dict.fromkeys(fallbacks)),
            "control_calibration": control_calibration,
            "run_status": _overall_run_status(stage_status),
        },
        "_cache_meta": _cache_meta(PROJECT_CONFIG),
    }
    _atomic_write_json(CACHE_PATH, result)
    return result


def run(state=None, force_recompute=False, skip_pipeline=False):
    _ensure_runtime_dirs()
    assert_project_approved(PROJECT_CONFIG)
    # The newly approved config is authoritative even when state.json predates it.
    state = State.sync_project_config(PROJECT_CONFIG)

    pipeline_runner = _run_pipeline if IS_MDM_REFERENCE else _run_generic_pipeline
    if force_recompute:
        pipeline_result = pipeline_runner()
    else:
        pipeline_result = _load_valid_cache(CACHE_PATH, PROJECT_CONFIG)
        if pipeline_result is not None:
            print(f"[research] Using cache: {CACHE_PATH}")
        else:
            pipeline_result = pipeline_runner()

    thresholds, threshold_normalization = normalize_thresholds(
        pipeline_result.get("thresholds") or _default_thresholds(PROJECT_CONFIG)
    )
    pipeline_result["thresholds"] = thresholds
    threshold_cache = None if force_recompute else _load_valid_cache(THRESHOLDS_CACHE, PROJECT_CONFIG)
    if force_recompute or threshold_cache is None:
        _write_threshold_cache(
            thresholds,
            PROJECT_CONFIG,
            {
                "normalization": threshold_normalization,
                "control_calibration": (
                    pipeline_result.get("_pipeline_meta", {}).get("control_calibration", {})
                ),
            },
        )

    configured_targets = {
        target["id"]: {key: value for key, value in target.items() if key != "id"}
        for target in PROJECT_CONFIG.get("targets", [])
    }
    pipeline_targets = pipeline_result.get("targets", {})
    for name in list(configured_targets):
        info = pipeline_targets.get(name)
        if isinstance(info, dict):
            configured_targets[name].update({key: value for key, value in info.items() if key != "id"})

    if not IS_MDM_REFERENCE:
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
        sync = State.sync_thresholds_from_cache(THRESHOLDS_CACHE)
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
    sync = State.sync_thresholds_from_cache(THRESHOLDS_CACHE)
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
