"""
Research Agent - MDM2/MDMX é¶ç‚¹è°ƒç ”ç®¡çº¿
8 æ­¥: RCSB Search -> GraphQL Enrich -> biotite interface -> aggregate pockets ->
      superpose analyze -> PubMed -> LLM extract -> threshold evidence
æ¯æ­¥æŒ‚ EvidenceLogger tool_traceã€‚biotite å¤±è´¥æ—¶è‡ªåŠ¨å›žé€€åˆ°é¢„ç½®å¸¸é‡ã€‚
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

# ===== é¢„ç½®å¸¸é‡ï¼ˆbiotite å¤±è´¥æ—¶å…œåº•ï¼‰=====
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

# ===== ä¸ƒå±‚æŒ‡æ ‡ç”µæ± é˜ˆå€¼ï¼ˆæ–‡çŒ®å…œåº•å€¼ï¼Œæœ€ç»ˆä»¥æ­£å¯¹ç…§æ ‡å®šä¸ºå‡†ï¼‰=====
# æ¥æºï¼šRFpeptides (Rettie et al., Nat Chem Biol 2025)ã€DeeCamp kickoff æŒ‡å¯¼
DEFAULT_THRESHOLDS = {
    "L1_plddt":            {"value": 0.8,  "operator": ">",  "unit": None,
                            "source": "RFpeptides paper (Nat Chem Biol 2025)", "confidence": "high",
                            "source_pmid": "40542165",
                            "evidence_quote": "refold with pLDDT > 0.8 and within 2.0 Ã… backbone r.m.s.d.",
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
                            "evidence_quote": "refold with pLDDT > 0.8 and within 2.0 Ã… backbone r.m.s.d.",
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
    if any(token in text for token in ("è¯·æ±‚å¤±è´¥", "ç½‘ç»œé”™è¯¯", "search failed", "request failed")):
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
        return "missing_api_key", "LLM API key not conó=¶‰žËkºwµçxÁ½­•Ñ}Í½ÕÉ”õíÉ•ÍÕ±Ñl}Á¥Á•±¥¹•}µ•Ñ„ulÁ½­•Ñ}Í½ÕÉ”uôˆ¤4(€€€É•ÑÕÉ¸É•ÍÕ±Ð4(4(4)‘•˜}ÉÕ¹}•¹•É¥}Á¥Á•±¥¹” ¤è(€€€€ˆˆ‰Q…É•Ðµ½¹™¥ÕÉ•É•Í•…É Á…Ñ Ý¥Ñ¡½ÕÐ54µÍÁ•¥™¥Œ‰¥½±½¥…°™…±±‰…­Ì¸ˆˆˆ(€€€}•¹ÍÕÉ•}ÉÕ¹Ñ¥µ•}‘¥ÉÌ ¤(€€€Ñ…É•Ñ}¥‘Ì€ô±¥ÍÐ¡AI=)Q}QIQ}%L¤(€€€ÍÑ…•}ÍÑ…ÑÕÌ€ôíô(€€€ÍÑ…•}½¹Ñ•áÐ€ôíô(€€€™…±±‰…­Ì€ômt(4(€€€ÍÈ°Í”°ÍŒ°Í°Í €ô}ÉÕ¹}ÍÉ¥ÁÐ ‰Í•…É¡}Á‘ˆ¹Áäˆ¤(€€€ÍÑ…•}ÍÑ…ÑÕÍl‰ÉÍ‰}Í•…É ‰t€ô€‰½µÁ±•Ñ”ˆ¥˜ÍŒ€ôô€À…¹ÍÈ¹•Ð ‰ÉÕ¹}ÍÑ…ÑÕÌˆ¤€ôô€‰½µÁ±•Ñ”ˆ•±Í”€‰•µÁÑäˆ¥˜ÍŒ€ôô€À•±Í”€‰™…¥±•ˆ(€€€ÍÑ…•}½¹Ñ•áÑl‰ÉÍ‰}Í•…É ‰t€ô€¡ÍÈ°Í”¤(€€€Ù¥‘•¹•1½•È¹±½œ ‰É•Í•…É ˆ°€‰Ñ½½±}…±°ˆ°ì4(€€€€€€€€‰Ñ½½±}¹…µ”ˆè€‰ÉÍ‰}Í•…É¡}…Á¤ˆ°€‰½ÕÑÁÕÑ}¡…Í ˆèÍ °€‰•á¥Ñ}½‘”ˆèÍŒ°4(€€€€€€€€‰‘ÕÉ…Ñ¥½¹}Í•ŒˆèÉ½Õ¹¡Í°€Ä¤°€‰ÍÑ‘½ÕÑ}Í¹¥ÁÁ•ÐˆèÍÑÈ¡ÍÈ¹•Ð ‰½Õ¹ÑÍ}‰å}Ñ…É•Ðˆ°íô¤¤°4(€€€ô°Ñ…É•ÑÌõÑ…É•Ñ}¥‘Ì°Á¡…Í”ô‰É•Í•…É ˆ¤4(4(€€€•È°•”°•Œ°•°• €ô}ÉÕ¹}ÍÉ¥ÁÐ ‰•¹É¥¡}Á‘ˆ¹Áäˆ°ÍÈ¤(€€€ÍÑ…•}ÍÑ…ÑÕÍl‰ÉÍ‰}•¹É¥ ‰t€ô€‰½µÁ±•Ñ”ˆ¥˜•Œ€ôô€À…¹•È¹•Ð ‰¹}Á•ÁÑ¥‘•}½µÁ±•á•Ìˆ°€À¤€ø€À•±Í”€‰•µÁÑäˆ¥˜•Œ€ôô€À•±Í”€‰™…¥±•ˆ(€€€ÍÑ…•}½¹Ñ•áÑl‰ÉÍ‰}•¹É¥ ‰t€ô€¡•È°•”¤(€€€Ù¥‘•¹•1½•È¹±½œ ‰É•Í•…É ˆ°€‰Ñ½½±}…±°ˆ°ì4(€€€€€€€€‰Ñ½½±}¹…µ”ˆè€‰ÉÍ‰}É…Á¡Å±}…Á¤ˆ°€‰½ÕÑÁÕÑ}¡…Í ˆè• °€‰•á¥Ñ}½‘”ˆè•Œ°4(€€€€€€€€‰‘ÕÉ…Ñ¥½¹}Í•ŒˆèÉ½Õ¹¡•°€Ä¤°€‰ÍÑ‘½ÕÑ}Í¹¥ÁÁ•ÐˆèÍÑÈ¡•È¹•Ð ‰½Õ¹ÑÍ}‰å}Ñ…É•Ðˆ°íô¤¤°4(€€€ô°Ñ…É•ÑÌõÑ…É•Ñ}¥‘Ì°Á¡…Í”ô‰É•Í•…É ˆ¤4(4(€€€…É•…Ñ”€ôì‰É•ÍÕ±ÑÍ}‰å}Ñ…É•Ðˆèíô°€‰½Õ¹ÑÍ}‰å}Ñ…É•Ðˆèíõô4(€€€¥˜½Ì¹•¹Ù¥É½¸¹•Ð ‰M-%A}	%=Q%Qˆ°€ˆˆ¤¹±½Ý•È ¤¥¸€ ˆÄˆ°€‰ÑÉÕ”ˆ°€‰å•Ìˆ¤è4(€€€€€€€ÍÑ…•}ÍÑ…ÑÕÍl‰¥¹Ñ•É™…”‰t€ô€‰Í­¥ÁÁ•ˆ(€€€€€€€ÍÑ…•}ÍÑ…ÑÕÍl‰…É•…Ñ”‰t€ô€‰Í­¥ÁÁ•ˆ(€€€€€€€ÍÑ…•}½¹Ñ•áÐ¹ÕÁ‘…Ñ”¡í¹…µ”è€¡íô°€ˆˆ¤™½È¹…µ”¥¸€ ‰¥¹Ñ•É™…”ˆ°€‰…É•…Ñ”ˆ¥ô¤(€€€€€€€™…±±‰…­Ì¹…ÁÁ•¹ ‰¥¹Ñ•É™…•}…É•…Ñ¥½¹}½µ¥ÑÑ•ˆ¤(€€€•±Í”è4(€€€€€€€ÑÉäè4(€€€€€€€€€€€¥È°¥”°¥Œ°¥‘|°¥ €ô}ÉÕ¹}ÍÉ¥ÁÐ ‰½µÁÕÑ•}¥¹Ñ•É™…”¹Áäˆ°•È¤(€€€€€€€€€€€ÍÑ…•}ÍÑ…ÑÕÍl‰¥¹Ñ•É™…”‰t€ô€‰½µÁ±•Ñ”ˆ¥˜¥Œ€ôô€À…¹¥È¹•Ð ‰¹}Ý¥Ñ¡}¥¹Ñ•É™…”ˆ°€À¤•±Í”€‰•µÁÑäˆ¥˜¥Œ€ôô€À•±Í”€‰™…¥±•ˆ(€€€€€€€€€€€ÍÑ…•}½¹Ñ•áÑl‰¥¹Ñ•É™…”‰t€ô€¡¥È°¥”¤(€€€€€€€€€€€Ù¥‘•¹•1½•È¹±½œ ‰É•Í•…É ˆ°€‰Ñ½½±}…±°ˆ°ì4(€€€€€€€€€€€€€€€€‰Ñ½½±}¹…µ”ˆè€‰‰¥½Ñ¥Ñ”ˆ°€‰½ÕÑÁÕÑ}¡…Í ˆè¥ °€‰•á¥Ñ}½‘”ˆè¥Œ°4(€€€€€€€€€€€€€€€€‰‘ÕÉ…Ñ¥½¹}Í•ŒˆèÉ½Õ¹¡¥‘|°€Ä¤°4(€€€€€€€€€€€€€€€€‰ÍÑ‘½ÕÑ}Í¹¥ÁÁ•Ðˆè˜‰¥¹Ñ•É™…•Ìõí¥È¹•Ð ¹}Ý¥Ñ¡}¥¹Ñ•É™…”œ°€À¥ôˆ°4(€€€€€€€€€€€ô°Ñ…É•ÑÌõÑ…É•Ñ}¥‘Ì°Á¡…Í”ô‰É•Í•…É ˆ¤4(€€€€€€€€€€€…É•…Ñ”°…”°…Œ°…°… €ô}ÉÕ¹}ÍÉ¥ÁÐ ‰…É•…Ñ•}Á½­•ÑÌ¹Áäˆ°¥È¤(€€€€€€€€€€€¡…Í}…É•…Ñ”€ô‰½½°¡…É•…Ñ”¹•Ð ‰É•ÍÕ±ÑÍ}‰å}Ñ…É•Ðˆ¤½È…É•…Ñ”¹•Ð ‰½Õ¹ÑÍ}‰å}Ñ…É•Ðˆ¤¤(€€€€€€€€€€€ÍÑ…•}ÍÑ…ÑÕÍl‰…É•…Ñ”‰t€ô€‰½µÁ±•Ñ”ˆ¥˜…Œ€ôô€À…¹¡…Í}…É•…Ñ”•±Í”€‰•µÁÑäˆ¥˜…Œ€ôô€À•±Í”€‰™…¥±•ˆ(€€€€€€€€€€€ÍÑ…•}½¹Ñ•áÑl‰…É•…Ñ”‰t€ô€¡…É•…Ñ”°…”¤(€€€€€€€€€€€Ù¥‘•¹•1½•È¹±½œ ‰É•Í•…É ˆ°€‰Ñ½½±}…±°ˆ°ì4(€€€€€€€€€€€€€€€€‰Ñ½½±}¹…µ”ˆè€‰…É•…Ñ•}Á½­•ÑÌˆ°€‰½ÕÑÁÕÑ}¡…Í ˆè… °€‰•á¥Ñ}½‘”ˆè…Œ°4(€€€€€€€€€€€€€€€€‰‘ÕÉ…Ñ¥½¹}Í•ŒˆèÉ½Õ¹¡…°€Ä¤°4(€€€€€€€€€€€€€€€€‰ÍÑ‘½ÕÑ}Í¹¥ÁÁ•ÐˆèÍÑÈ¡…É•…Ñ”¹•Ð ‰½Õ¹ÑÍ}‰å}Ñ…É•Ðˆ°íô¤¤°4(€€€€€€€€€€€ô°Ñ…É•ÑÌõÑ…É•Ñ}¥‘Ì°Á¡…Í”ô‰É•Í•…É ˆ¤4(€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè4(€€€€€€€€€€€ÍÑ…•}ÍÑ…ÑÕÍl‰¥¹Ñ•É™…”‰t€ô€‰™…¥±•ˆ4(€€€€€€€€€€€ÍÑ…•}ÍÑ…ÑÕÍl‰…É•…Ñ”‰t€ô€‰™…¥±•ˆ(€€€€€€€€€€€ÍÑ…•}½¹Ñ•áÑl‰¥¹Ñ•É™…”‰t€ô€¡íô°ÍÑÈ¡•áŒ¤¤(€€€€€€€€€€€ÍÑ…•}½¹Ñ•áÑl‰…É•…Ñ”‰t€ô€¡íô°ÍÑÈ¡•áŒ¤¤(€€€€€€€€€€€™…±±‰…­Ì¹…ÁÁ•¹ ‰¥¹Ñ•É™…•}…É•…Ñ¥½¹}½µ¥ÑÑ•ˆ¤(€€€€€€€€€€€Ù¥‘•¹•1½•È¹•ÉÉ½È ‰É•Í•…É ˆ°€‰Ñ½½±}™…¥±ÕÉ”ˆ°ÍÑÈ¡•áŒ¤°É•½Ù•Éäô‰½¹Ñ¥¹Õ”Ý¥Ñ¡½ÕÐ¥¹Ñ•É™…”…É•…Ñ¥½¸ˆ¤4(4(€€€ÁµÈ°Áµ”°ÁŒ°Á°Á €ô}ÉÕ¹}ÍÉ¥ÁÐ ‰ÁÕ‰µ•‘}Í•…É ¹Áäˆ¤(€€€ÍÑ…•}ÍÑ…ÑÕÍl‰ÁÕ‰µ•‰t€ô€‰½µÁ±•Ñ”ˆ¥˜ÁŒ€ôô€À…¹ÁµÈ¹•Ð ‰¹}Ñ½Ñ…°ˆ°€À¤€ø€À•±Í”€‰•µÁÑäˆ¥˜ÁŒ€ôô€À•±Í”€‰™…¥±•ˆ(€€€ÍÑ…•}½¹Ñ•áÑl‰ÁÕ‰µ•‰t€ô€¡ÁµÈ°Áµ”¤(€€€Ù¥‘•¹•1½•È¹±½œ ‰É•Í•…É ˆ°€‰Ñ½½±}…±°ˆ°ì4(€€€€€€€€‰Ñ½½±}¹…µ”ˆè€‰ÁÕ‰µ•‘}•ÕÑ¥±Ìˆ°€‰½ÕÑÁÕÑ}¡…Í ˆèÁ °€‰•á¥Ñ}½‘”ˆèÁŒ°4(€€€€€€€€‰‘ÕÉ…Ñ¥½¹}Í•ŒˆèÉ½Õ¹¡Á°€Ä¤°€‰ÍÑ‘½ÕÑ}Í¹¥ÁÁ•Ðˆè˜‰Á…Á•ÉÌõíÁµÈ¹•Ð ¹}Ñ½Ñ…°œ°€À¥ôˆ°4(€€€ô°Ñ…É•ÑÌõÑ…É•Ñ}¥‘Ì°Á¡…Í”ô‰É•Í•…É ˆ¤4(4(€€€­¹½Ý¹}‰¥¹‘•ÉÌ€ômt4(€€€ÑÉäè4(€€€€€€€±È°±”°±Œ°±°± €ô}ÉÕ¹}ÍÉ¥ÁÐ ‰±±µ}•áÑÉ…Ð¹Áäˆ°ÁµÈ°•áÑÉ…}…ÉÌõlˆ´µ½¹ÕÉÉ•¹äˆ°€ˆÌ‰t¤(€€€€€€€­¹½Ý¹}‰¥¹‘•ÉÌ€ô±È¹•Ð ‰­¹½Ý¹}‰¥¹‘•ÉÌˆ°mt¤4(€€€€€€€É…Ý}±±µ}ÍÑ…ÑÕÌ€ô±È¹•Ð ‰ÉÕ¹}ÍÑ…ÑÕÌˆ°€‰™…¥±•ˆ¤(€€€€€€€ÍÑ…•}ÍÑ…ÑÕÍl‰±±µ}•áÑÉ…Ð‰t€ô€ (€€€€€€€€€€€€‰‘•É…‘•‘}¹½}…Á¥}­•äˆ¥˜É…Ý}±±µ}ÍÑ…ÑÕÌ€ôô€‰‘•É…‘•‘}¹½}…Á¥}­•äˆ(€€€€€€€€€€€•±Í”€‰½µÁ±•Ñ”ˆ¥˜±Œ€ôô€À…¹É…Ý}±±µ}ÍÑ…ÑÕÌ€ôô€‰½µÁ±•Ñ”ˆ(€€€€€€€€€€€•±Í”€‰™…¥±•ˆ(€€€€€€€€¤(€€€€€€€ÍÑ…•}½¹Ñ•áÑl‰±±µ}•áÑÉ…Ð‰t€ô€¡±È°±”¤(€€€€€€€¥˜¹½Ð­¹½Ý¹}‰¥¹‘•ÉÌè(€€€€€€€€€€€™…±±‰…­Ì¹…ÁÁ•¹ ‰¹½}‰¥¹‘•É}™…±±‰…¬ˆ¤(€€€€€€€Ù¥‘•¹•1½•È¹±½œ ‰É•Í•…É ˆ°€‰Ñ½½±}…±°ˆ°ì4(€€€€€€€€€€€€‰Ñ½½±}¹…µ”ˆè€‰±±µ}•áÑÉ…Ðˆ°€‰½ÕÑÁÕÑ}¡…Í ˆè± °€‰•á¥Ñ}½‘”ˆè±Œ°4(€€€€€€€€€€€€‰‘ÕÉ…Ñ¥½¹}Í•ŒˆèÉ½Õ¹¡±°€Ä¤°€‰ÍÑ‘½ÕÑ}Í¹¥ÁÁ•Ðˆè˜‰‰¥¹‘•ÉÌõí±•¸¡­¹½Ý¹}‰¥¹‘•ÉÌ¥ôˆ°4(€€€€€€€ô°Ñ…É•ÑÌõÑ…É•Ñ}¥‘Ì°Á¡…Í”ô‰É•Í•…É ˆ¤4(€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè4(€€€€€€€ÍÑ…•}ÍÑ…ÑÕÍl‰±±µ}•áÑÉ…Ð‰t€ô€‰™…¥±•ˆ(€€€€€€€ÍÑ…•}½¹Ñ•áÑl‰±±µ}•áÑÉ…Ð‰t€ô€¡íô°ÍÑÈ¡•áŒ¤¤(€€€€€€€™…±±‰…­Ì¹…ÁÁ•¹ ‰¹½}‰¥¹‘•É}™…±±‰…¬ˆ¤(€€€€€€€Ù¥‘•¹•1½•È¹•ÉÉ½È ‰É•Í•…É ˆ°€‰Ñ½½±}™…¥±ÕÉ”ˆ°ÍÑÈ¡•áŒ¤°É•½Ù•Éäô‰¹¼™…‰É¥…Ñ•‰¥¹‘•È™…±±‰…¬ˆ¤4(4(€€€Ñ¡É•Í¡½±‘Ì€ô}‘•™…Õ±Ñ}Ñ¡É•Í¡½±‘Ì¡AI=)Q}=9%¤(€€€ÑÉäè4(€€€€€€€ÑÈ°Ñ”°ÑŒ°Ñ°Ñ¡…Í €ô}ÉÕ¹}ÍÉ¥ÁÐ ‰Ñ¡É•Í¡½±‘}É•Í•…É ¹Áäˆ°•áÑÉ…}…ÉÌõlˆ´µ½¹ÕÉÉ•¹äˆ°€ˆÐ‰t¤(€€€€€€€±¥Ñ•É…ÑÕÉ•}Ñ¡É•Í¡½±‘Ì°Ñ¡É•Í¡½±‘}¹½Éµ…±¥é…Ñ¥½¸€ô¹½Éµ…±¥é•}Ñ¡É•Í¡½±‘Ì¡ÑÈ¹•Ð ‰µ•ÑÉ¥}‰…ÑÑ•Éäˆ°íô¤¤(€€€€€€€™½È­•ä°¥¹™¼¥¸±¥Ñ•É…ÑÕÉ•}Ñ¡É•Í¡½±‘Ì¹¥Ñ•µÌ ¤è(€€€€€€€€€€€¥˜¥¹™¼¹•Ð ‰…ÕÑ½}ÕÍ…‰±”ˆ¤…¹¥¹™¼¹•Ð ‰Ù…±Õ”ˆ¤¥Ì¹½Ð9½¹”è(€€€€€€€€€€€€€€€Ñ¡É•Í¡½±‘Ím­•åt€ôì(€€€€€€€€€€€€€€€€€€€€¨©í¹…µ”èÙ…±Õ”™½È¹…µ”°Ù…±Õ”¥¸Ñ¡É•Í¡½±‘Ì¹•Ð¡­•ä°íô¤¹¥Ñ•µÌ ¤4(€€€€€€€€€€€€€€€€€€€€€€¥˜¹…µ”¥¸€ ‰µ•Ñ¡½ˆ°€‰µ¥¹}Í••‘}™É…Ñ¥½¸ˆ¥ô°4(€€€€€€€€€€€€€€€€€€€€‰Ù…±Õ”ˆè¥¹™½l‰Ù…±Õ”‰t°€‰½Á•É…Ñ½Èˆè¥¹™¼¹•Ð ‰½Á•É…Ñ½Èˆ°€ˆøˆ¤°4(€€€€€€€€€€€€€€€€€€€€‰Õ¹¥Ðˆè¥¹™¼¹•Ð ‰Õ¹¥Ðˆ¤°€‰Í½ÕÉ”ˆè˜‰AÕ‰5•A5%í¥¹™¼¹•Ð Í½ÕÉ•}Áµ¥œ¥ôˆ°4(€€€€€€€€€€€€€€€€€€€€‰Í½ÕÉ•}Áµ¥ˆè¥¹™¼¹•Ð ‰Í½ÕÉ•}Áµ¥ˆ¤°€‰•Ù¥‘•¹•}ÅÕ½Ñ”ˆè¥¹™¼¹•Ð ‰•Ù¥‘•¹•}ÅÕ½Ñ”ˆ¤°4(€€€€€€€€€€€€€€€€€€€€‰•Ù¥‘•¹•}É…‘”ˆè€‰Á…Á•É}•áÁ±¥¥Ðˆ°€‰ÅÕ½Ñ•}Ù•É¥™¥•ˆèQÉÕ”°(€€€€€€€€€€€€€€€€€€€€‰…±¥‰É…Ñ¥½¹}ÍÑ…ÑÕÌˆè€‰Á•¹‘¥¹œˆ°(€€€€€€€€€€€€€€€€€€€€‰…ÁÁ±¥…‰±•}Ñ…É•ÑÌˆèÑ…É•Ñ}¥‘Ì°(€€€€€€€€€€€€€€€ô(€€€€€€€É…Ý}Ñ¡É•Í¡½±‘}ÍÑ…ÑÕÌ€ôÑÈ¹•Ð ‰}µ•Ñ„ˆ°íô¤¹•Ð ‰ÉÕ¹}ÍÑ…ÑÕÌˆ°€‰™…¥±•ˆ¤(€€€€€€€ÍÑ…•}ÍÑ…ÑÕÍl‰Ñ¡É•Í¡½±‘}É•Í•…É ‰t€ô€ (€€€€€€€€€€€€‰‘•É…‘•‘}¹½}…Á¥}­•äˆ¥˜É…Ý}Ñ¡É•Í¡½±‘}ÍÑ…ÑÕÌ€ôô€‰‘•É…‘•‘}¹½}±±´ˆ(€€€€€€€€€€€•±Í”€‰•µÁÑäˆ¥˜É…Ý}Ñ¡É•Í¡½±‘}ÍÑ…ÑÕÌ€ôô€‰‘•É…‘•‘}•µÁÑå}É•ÍÕ±ÑÌˆ(€€€€€€€€€€€•±Í”€‰½µÁ±•Ñ”ˆ¥˜ÑŒ€ôô€À…¹É…Ý}Ñ¡É•Í¡½±‘}ÍÑ…ÑÕÌ€ôô€‰½µÁ±•Ñ”ˆ(€€€€€€€€€€€•±Í”€‰™…¥±•ˆ(€€€€€€€€¤(€€€€€€€ÍÑ…•}½¹Ñ•áÑl‰Ñ¡É•Í¡½±‘}É•Í•…É ‰t€ô€¡ÑÈ°Ñ”¤(€€€€€€€Ñ¡É•Í¡½±‘Ì°™¥¹…±}¹½Éµ…±¥é…Ñ¥½¸€ô¹½Éµ…±¥é•}Ñ¡É•Í¡½±‘Ì¡Ñ¡É•Í¡½±‘Ì¤(€€€€€€€Ù¥‘•¹•1½•È¹±½œ ‰É•Í•…É ˆ°€‰Ñ½½±}…±°ˆ°ì4(€€€€€€€€€€€€‰Ñ½½±}¹…µ”ˆè€‰Ñ¡É•Í¡½±‘}É•Í•…É ˆ°€‰½ÕÑÁÕÑ}¡…Í ˆèÑ¡…Í °€‰•á¥Ñ}½‘”ˆèÑŒ°4(€€€€€€€€€€€€‰‘ÕÉ…Ñ¥½¹}Í•ŒˆèÉ½Õ¹¡Ñ°€Ä¤°4(€€€€€€€€€€€€‰ÍÑ‘½ÕÑ}Í¹¥ÁÁ•Ðˆè˜‰ÕÍ…‰±”õíÑÈ¹•Ð }µ•Ñ„œ°íô¤¹•Ð ¹}…ÕÑ½}ÕÍ…‰±”œ°€À¥ôˆ°4(€€€€€€€ô°Ñ…É•ÑÌõÑ…É•Ñ}¥‘Ì°Á¡…Í”ô‰É•Í•…É ˆ¤4(€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè4(€€€€€€€ÍÑ…•}ÍÑ…ÑÕÍl‰Ñ¡É•Í¡½±‘}É•Í•…É ‰t€ô€‰™…¥±•ˆ(€€€€€€€ÍÑ…•}½¹Ñ•áÑl‰Ñ¡É•Í¡½±‘}É•Í•…É ‰t€ô€¡íô°ÍÑÈ¡•áŒ¤¤(€€€€€€€™…±±‰…­Ì¹…ÁÁ•¹ ‰ÁÉ½Ù¥Í¥½¹…±}‘•™…Õ±Ñ}Ñ¡É•Í¡½±‘Ìˆ¤((€€€¥˜ÍÑ…•}ÍÑ…ÑÕÌ¹•Ð ‰Ñ¡É•Í¡½±‘}É•Í•…É ˆ¤€„ô€‰½µÁ±•Ñ”ˆè(€€€€€€€™…±±‰…­Ì¹…ÁÁ•¹ ‰ÁÉ½Ù¥Í¥½¹…±}‘•™…Õ±Ñ}Ñ¡É•Í¡½±‘Ìˆ¤(€€€Ñ¡É•Í¡½±‘Ì°½¹ÑÉ½±}…±¥‰É…Ñ¥½¸€ô}…ÁÁ±å}½¹ÑÉ½±}…±¥‰É…Ñ¥½¸¡Ñ¡É•Í¡½±‘Ì°AI=)Q}=9%¤(€€€}ÝÉ¥Ñ•}Ñ¡É•Í¡½±‘}…¡”¡Ñ¡É•Í¡½±‘Ì°AI=)Q}=9%°ì(€€€€€€€€‰±¥Ñ•É…ÑÕÉ•}¥¹ÁÕÐˆèÑ¡É•Í¡½±‘}¹½Éµ…±¥é…Ñ¥½¸¥˜€‰Ñ¡É•Í¡½±‘}¹½Éµ…±¥é…Ñ¥½¸ˆ¥¸±½…±Ì ¤•±Í”íô°(€€€€€€€€‰™¥¹…°ˆè™¥¹…±}¹½Éµ…±¥é…Ñ¥½¸¥˜€‰™¥¹…±}¹½Éµ…±¥é…Ñ¥½¸ˆ¥¸±½…±Ì ¤•±Í”íô°(€€€€€€€€‰½¹ÑÉ½±}…±¥‰É…Ñ¥½¸ˆè½¹ÑÉ½±}…±¥‰É…Ñ¥½¸°(€€€ô¤(€€€¥˜¹½ÐQ!IM!=1M}!¹•á¥ÍÑÌ ¤è(€€€€€€€}ÝÉ¥Ñ•}Ñ¡É•Í¡½±‘}…¡”¡Ñ¡É•Í¡½±‘Ì°AI=)Q}=9%¤(€€€€€€€Ù¥‘•¹•1½•È¹•ÉÉ½È ‰É•Í•…É ˆ°€‰Ñ½½±}™…¥±ÕÉ”ˆ°ÍÑÈ¡•áŒ¤°É•½Ù•Éäô‰ÁÉ½Ù¥Í¥½¹…°Ñ¡É•Í¡½±‘ÌÉ•µ…¥¸¹½¸µ±•…É…‰±”ˆ¤4(4(€€€‘å¹…µ¥}Á‘‰}±¥ÍÐ€ômÉ½Ü¹•Ð ‰Á‘‰}¥ˆ¤™½ÈÉ½Ü¥¸•È¹•Ð ‰Á•ÁÑ¥‘•}½µÁ±•á•Ìˆ°mt¤¥˜É½Ü¹•Ð ‰Á‘‰}¥ˆ¥t4(€€€ÍÑ…•}•ÉÉ½É}½‘”°•ÉÉ½É}µ•ÍÍ…”€ô}‘¥…¹½ÍÑ¥Í}™½É}ÍÑ…•Ì¡ÍÑ…•}ÍÑ…ÑÕÌ°ÍÑ…•}½¹Ñ•áÐ¤(€€€É•ÍÕ±Ð€ôì(€€€€€€€€‰ÁÉ½©•Ñ}¥ˆèAI=)Q}=9%l‰ÁÉ½©•Ñ}¥‰t°4(€€€€€€€€‰Ñ…É•ÑÌˆèíÑ…É•Ñl‰¥‰tèÑ…É•Ð™½ÈÑ…É•Ð¥¸AI=)Q}=9%l‰Ñ…É•ÑÌ‰uô°4(€€€€€€€€‰Á½­•Ñ}‘¥™™•É•¹•Ìˆèì4(€€€€€€€€€€€€‰}Í½ÕÉ”ˆè€‰‘å¹…µ¥}¥¹Ñ•É™…•}…É•…Ñ¥½¸ˆ°4(€€€€€€€€€€€€‰Ñ…É•ÑÌˆè…É•…Ñ”¹•Ð ‰É•ÍÕ±ÑÍ}‰å}Ñ…É•Ðˆ°íô¤°4(€€€€€€€ô°4(€€€€€€€€‰­¹½Ý¹}‰¥¹‘•ÉÌˆè­¹½Ý¹}‰¥¹‘•ÉÌ°4(€€€€€€€€‰­¹½Ý¹}‘Õ…±}‰¥¹‘•ÉÌˆè­¹½Ý¹}‰¥¹‘•ÉÌ°4(€€€€€€€€‰­¹½Ý¹}‰¥¹‘•É}Í½ÕÉ”ˆè€‰±±µ}•áÑÉ…Ñ•ˆ¥˜­¹½Ý¹}‰¥¹‘•ÉÌ•±Í”€‰¹½¹•}™½Õ¹ˆ°4(€€€€€€€€‰‘•Í¥¹}ÍÑÉ…Ñ•å}ÍÕµµ…Éäˆè€‰Q…É•Ðµ½¹™¥ÕÉ•ì‘•É¥Ù”‘•Í¥¸½¹ÍÑÉ…¥¹ÑÌ™É½´É•ÑÉ¥•Ù••Á¥Ñ½Á”•Ù¥‘•¹”¸ˆ°4(€€€€€€€€‰‘…Ñ…}ÅÕ…±¥Ñå}…±•ÉÐˆè€‰9¼54µÍÁ•¥™¥Œ½¹ÍÑ…¹ÑÌÝ•É”ÕÍ•…Ì™…±±‰…¬¸ˆ°4(€€€€€€€€‰±¥Ñ•É…ÑÕÉ•}É•™ÌˆèÁµÈ¹•Ð ‰Áµ¥‘Ìˆ°mt¤°4(€€€€€€€€‰Ñ¡É•Í¡½±‘ÌˆèÑ¡É•Í¡½±‘Ì°4(€€€€€€€€‰}Á¥Á•±¥¹•}µ•Ñ„ˆèì4(€€€€€€€€€€€€‰±…ÍÑ}ÉÕ¸ˆè‘…Ñ•Ñ¥µ”¹¹½Ü¡Ñ¥µ•é½¹”¹ÕÑŒ¤¹¥Í½™½Éµ…Ð ¤°4(€€€€€€€€€€€€‰‘å¹…µ¥}Á‘‰}±¥ÍÐˆè‘å¹…µ¥}Á‘‰}±¥ÍÐ°4(€€€€€€€€€€€€‰½Õ¹ÑÍ}‰å}Ñ…É•Ðˆè•È¹•Ð ‰½Õ¹ÑÍ}‰å}Ñ…É•Ðˆ°íô¤°4(€€€€€€€€€€€€‰ÍÑ…•}ÍÑ…ÑÕÌˆèÍÑ…•}ÍÑ…ÑÕÌ°(€€€€€€€€€€€€‰ÍÑ…•}•ÉÉ½É}½‘”ˆèÍÑ…•}•ÉÉ½É}½‘”°(€€€€€€€€€€€€‰•ÉÉ½É}µ•ÍÍ…”ˆè•ÉÉ½É}µ•ÍÍ…”°(€€€€€€€€€€€€‰™…±±‰…­Í}ÕÍ•ˆè±¥ÍÐ¡‘¥Ð¹™É½µ­•åÌ¡™…±±‰…­Ì¤¤°(€€€€€€€€€€€€‰½¹ÑÉ½±}…±¥‰É…Ñ¥½¸ˆè½¹ÑÉ½±}…±¥‰É…Ñ¥½¸°(€€€€€€€€€€€€‰ÉÕ¹}ÍÑ…ÑÕÌˆè}½Ù•É…±±}ÉÕ¹}ÍÑ…ÑÕÌ¡ÍÑ…•}ÍÑ…ÑÕÌ¤°(€€€€€€€ô°(€€€€€€€€‰}…¡•}µ•Ñ„ˆè}…¡•}µ•Ñ„¡AI=)Q}=9%¤°(€€€ô(€€€}…Ñ½µ¥}ÝÉ¥Ñ•}©Í½¸¡!}AQ °É•ÍÕ±Ð¤(€€€É•ÑÕÉ¸É•ÍÕ±Ð4(4(4)‘•˜ÉÕ¸¡ÍÑ…Ñ”õ9½¹”°™½É•}É•½µÁÕÑ”õ…±Í”°Í­¥Á}Á¥Á•±¥¹”õ…±Í”¤è(€€€}•¹ÍÕÉ•}ÉÕ¹Ñ¥µ•}‘¥ÉÌ ¤(€€€…ÍÍ•ÉÑ}ÁÉ½©•Ñ}…ÁÁÉ½Ù•¡AI=)Q}=9%¤(€€€€ŒQ¡”¹•Ý±ä…ÁÁÉ½Ù•½¹™¥œ¥Ì…ÕÑ¡½É¥Ñ…Ñ¥Ù”•Ù•¸Ý¡•¸ÍÑ…Ñ”¹©Í½¸ÁÉ•‘…Ñ•Ì¥Ð¸(€€€ÍÑ…Ñ”€ôMÑ…Ñ”¹Íå¹}ÁÉ½©•Ñ}½¹™¥œ¡AI=)Q}=9%¤((€€€Á¥Á•±¥¹•}ÉÕ¹¹•È€ô}ÉÕ¹}Á¥Á•±¥¹”¥˜%M}55}II9•±Í”}ÉÕ¹}•¹•É¥}Á¥Á•±¥¹”(€€€¥˜™½É•}É•½µÁÕÑ”è(€€€€€€€Á¥Á•±¥¹•}É•ÍÕ±Ð€ôÁ¥Á•±¥¹•}ÉÕ¹¹•È ¤(€€€•±Í”è(€€€€€€€Á¥Á•±¥¹•}É•ÍÕ±Ð€ô}±½…‘}Ù…±¥‘}…¡”¡!}AQ °AI=)Q}=9%¤(€€€€€€€¥˜Á¥Á•±¥¹•}É•ÍÕ±Ð¥Ì¹½Ð9½¹”è(€€€€€€€€€€€ÁÉ¥¹Ð¡˜‰mÉ•Í•…É¡tUÍ¥¹œ…¡”èí!}AQ!ôˆ¤(€€€€€€€•±Í”è(€€€€€€€€€€€Á¥Á•±¥¹•}É•ÍÕ±Ð€ôÁ¥Á•±¥¹•}ÉÕ¹¹•È ¤((€€€Ñ¡É•Í¡½±‘Ì°Ñ¡É•Í¡½±‘}¹½Éµ…±¥é…Ñ¥½¸€ô¹½Éµ…±¥é•}Ñ¡É•Í¡½±‘Ì (€€€€€€€Á¥Á•±¥¹•}É•ÍÕ±Ð¹•Ð ‰Ñ¡É•Í¡½±‘Ìˆ¤½È}‘•™…Õ±Ñ}Ñ¡É•Í¡½±‘Ì¡AI=)Q}=9%¤(€€€€¤(€€€Á¥Á•±¥¹•}É•ÍÕ±Ñl‰Ñ¡É•Í¡½±‘Ì‰t€ôÑ¡É•Í¡½±‘Ì(€€€Ñ¡É•Í¡½±‘}…¡”€ô9½¹”¥˜™½É•}É•½µÁÕÑ”•±Í”}±½…‘}Ù…±¥‘}…¡”¡Q!IM!=1M}!°AI=)Q}=9%¤(€€€¥˜™½É•}É•½µÁÕÑ”½ÈÑ¡É•Í¡½±‘}…¡”¥Ì9½¹”è(€€€€€€€}ÝÉ¥Ñ•}Ñ¡É•Í¡½±‘}…¡” (€€€€€€€€€€€Ñ¡É•Í¡½±‘Ì°(€€€€€€€€€€€AI=)Q}=9%°(€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€‰¹½Éµ…±¥é…Ñ¥½¸ˆèÑ¡É•Í¡½±‘}¹½Éµ…±¥é…Ñ¥½¸°(€€€€€€€€€€€€€€€€‰½¹ÑÉ½±}…±¥‰É…Ñ¥½¸ˆè€ (€€€€€€€€€€€€€€€€€€€Á¥Á•±¥¹•}É•ÍÕ±Ð¹•Ð ‰}Á¥Á•±¥¹•}µ•Ñ„ˆ°íô¤¹•Ð ‰½¹ÑÉ½±}…±¥‰É…Ñ¥½¸ˆ°íô¤(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€ô°(€€€€€€€€¤((€€€½¹™¥ÕÉ•‘}Ñ…É•ÑÌ€ôì(€€€€€€€Ñ…É•Ñl‰¥‰tèí­•äèÙ…±Õ”™½È­•ä°Ù…±Õ”¥¸Ñ…É•Ð¹¥Ñ•µÌ ¤¥˜­•ä€„ô€‰¥‰ô(€€€€€€€™½ÈÑ…É•Ð¥¸AI=)Q}=9%¹•Ð ‰Ñ…É•ÑÌˆ°mt¤(€€€ô(€€€Á¥Á•±¥¹•}Ñ…É•ÑÌ€ôÁ¥Á•±¥¹•}É•ÍÕ±Ð¹•Ð ‰Ñ…É•ÑÌˆ°íô¤(€€€™½È¹…µ”¥¸±¥ÍÐ¡½¹™¥ÕÉ•‘}Ñ…É•ÑÌ¤è(€€€€€€€¥¹™¼€ôÁ¥Á•±¥¹•}Ñ…É•ÑÌ¹•Ð¡¹…µ”¤(€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡¥¹™¼°‘¥Ð¤è(€€€€€€€€€€€½¹™¥ÕÉ•‘}Ñ…É•ÑÍm¹…µ•t¹ÕÁ‘…Ñ”¡í­•äèÙ…±Õ”™½È­•ä°Ù…±Õ”¥¸¥¹™¼¹¥Ñ•µÌ ¤¥˜­•ä€„ô€‰¥‰ô¤((€€€¥˜¹½Ð%M}55}II9è(€€€€€€€É•ÍÕ±Ð€ôì(€€€€€€€€€€€€‰Ñ…É•ÑÌˆè½¹™¥ÕÉ•‘}Ñ…É•ÑÌ°(€€€€€€€€€€€€‰Á½­•Ñ}‘¥™™•É•¹•ÌˆèÁ¥Á•±¥¹•}É•ÍÕ±Ð¹•Ð ‰Á½­•Ñ}‘¥™™•É•¹•Ìˆ°íô¤°4(€€€€€€€€€€€€‰­¹½Ý¹}‰¥¹‘•ÉÌˆèÁ¥Á•±¥¹•}É•ÍÕ±Ð¹•Ð ‰­¹½Ý¹}‰¥¹‘•ÉÌˆ°mt¤°4(€€€€€€€€€€€€‰­¹½Ý¹}‘Õ…±}‰¥¹‘•ÉÌˆèÁ¥Á•±¥¹•}É•ÍÕ±Ð¹•Ð ‰­¹½Ý¹}‰¥¹‘•ÉÌˆ°mt¤°4(€€€€€€€€€€€€‰­¹½Ý¹}‰¥¹‘•É}Í½ÕÉ”ˆèÁ¥Á•±¥¹•}É•ÍÕ±Ð¹•Ð ‰­¹½Ý¹}‰¥¹‘•É}Í½ÕÉ”ˆ°€‰¹½¹•}™½Õ¹ˆ¤°4(€€€€€€€€€€€€‰‘•Í¥¹}ÍÑÉ…Ñ•å}ÍÕµµ…ÉäˆèÁ¥Á•±¥¹•}É•ÍÕ±Ð¹•Ð ‰‘•Í¥¹}ÍÑÉ…Ñ•å}ÍÕµµ…Éäˆ°€ˆˆ¤°4(€€€€€€€€€€€€‰‘…Ñ…}ÅÕ…±¥Ñå}…±•ÉÐˆèÁ¥Á•±¥¹•}É•ÍÕ±Ð¹•Ð ‰‘…Ñ…}ÅÕ…±¥Ñå}…±•ÉÐˆ°€ˆˆ¤°4(€€€€€€€€€€€€‰É•Í•…É¡}Á¥Á•±¥¹•}µ•Ñ„ˆèÁ¥Á•±¥¹•}É•ÍÕ±Ð¹•Ð ‰}Á¥Á•±¥¹•}µ•Ñ„ˆ°íô¤°(€€€€€€€ô(€€€€€€€MÑ…Ñ”¹ÕÁ‘…Ñ”¡É•ÍÕ±Ð¤(€€€€€€€Íå¹Œ€ôMÑ…Ñ”¹Íå¹}Ñ¡É•Í¡½±‘Í}™É½µ}…¡”¡Q!IM!=1M}!¤(€€€€€€€É•ÍÕ±Ñl‰Ñ¡É•Í¡½±‘Ì‰t€ôÍå¹l‰ÍÑ…Ñ”‰t¹•Ð ‰Ñ¡É•Í¡½±‘Ìˆ°íô¤(€€€€€€€µ•Ñ„€ôÉ•ÍÕ±Ñl‰É•Í•…É¡}Á¥Á•±¥¹•}µ•Ñ„‰t4(€€€€€€€Ù¥‘•¹•1½•È¹É•Í•…É¡}½µÁ±•Ñ” 4(€€€€€€€€€€€¡½ÑÍÁ½Ñ}…¹…±åÍ¥Ìõì4(€€€€€€€€€€€€€€€€‰Á‘‰}±¥ÍÐˆèµ•Ñ„¹•Ð ‰‘å¹…µ¥}Á‘‰}±¥ÍÐˆ°mt¤°4(€€€€€€€€€€€€€€€€‰½Õ¹ÑÍ}‰å}Ñ…É•Ðˆèµ•Ñ„¹•Ð ‰½Õ¹ÑÍ}‰å}Ñ…É•Ðˆ°íô¤°4(€€€€€€€€€€€€€€€€‰Á½­•ÑÌˆèÉ•ÍÕ±Ñl‰Á½­•Ñ}‘¥™™•É•¹•Ì‰t°4(€€€€€€€€€€€€€€€€‰ÍÑ…•}ÍÑ…ÑÕÌˆèµ•Ñ„¹•Ð ‰ÍÑ…•}ÍÑ…ÑÕÌˆ°íô¤°4(€€€€€€€€€€€€€€€€‰ÉÕ¹}ÍÑ…ÑÕÌˆèµ•Ñ„¹•Ð ‰ÉÕ¹}ÍÑ…ÑÕÌˆ°€‰Õ¹­¹½Ý¸ˆ¤°4(€€€€€€€€€€€ô°4(€€€€€€€€€€€­¹½Ý¹}‰¥¹‘•ÉÌõÉ•ÍÕ±Ñl‰­¹½Ý¹}‰¥¹‘•ÉÌ‰t°4(€€€€€€€€€€€É•™ÌõÁ¥Á•±¥¹•}É•ÍÕ±Ð¹•Ð ‰±¥Ñ•É…ÑÕÉ•}É•™Ìˆ°mt¤°4(€€€€€€€€¤4(€€€€€€€É•ÑÕÉ¸É•ÍÕ±Ð4(4(€€€É•ÍÕ±Ð€ôì(€€€€€€€€‰Ñ…É•ÑÌˆè½¹™¥ÕÉ•‘}Ñ…É•ÑÌ°(€€€€€€€€‰Á½­•Ñ}‘¥™™•É•¹•ÌˆèÁ¥Á•±¥¹•}É•ÍÕ±Ð¹•Ð ‰Á½­•Ñ}‘¥™™•É•¹•Ìˆ°A=-Q}%I9L¤°4(€€€€€€€€‰­¹½Ý¹}‘Õ…±}‰¥¹‘•ÉÌˆèÁ¥Á•±¥¹•}É•ÍÕ±Ð¹•Ð ‰­¹½Ý¹}‘Õ…±}‰¥¹‘•ÉÌˆ°-9=]9}U1}	%9IL¤°4(€€€€€€€€‰­¹½Ý¹}‰¥¹‘•É}Í½ÕÉ”ˆèÁ¥Á•±¥¹•}É•ÍÕ±Ð¹•Ð ‰­¹½Ý¹}‰¥¹‘•É}Í½ÕÉ”ˆ°€‰ÕÉ…Ñ•‘}™…±±‰…¬ˆ¤°4(€€€€€€€€‰‘•Í¥¹}ÍÑÉ…Ñ•å}ÍÕµµ…ÉäˆèÁ¥Á•±¥¹•}É•ÍÕ±Ð¹•Ð ‰‘•Í¥¹}ÍÑÉ…Ñ•å}ÍÕµµ…Éäˆ°M%9}MQIQe}MU55Id¤°4(€€€€€€€€‰‘…Ñ…}ÅÕ…±¥Ñå}…±•ÉÐˆèÁ¥Á•±¥¹•}É•ÍÕ±Ð¹•Ð ‰‘…Ñ…}ÅÕ…±¥Ñå}…±•ÉÐˆ°Q}EU1%Qe}1IP¤°4(€€€€€€€€‰É•Í•…É¡}Á¥Á•±¥¹•}µ•Ñ„ˆèÁ¥Á•±¥¹•}É•ÍÕ±Ð¹•Ð ‰}Á¥Á•±¥¹•}µ•Ñ„ˆ°íô¤°(€€€ô(€€€MÑ…Ñ”¹ÕÁ‘…Ñ”¡É•ÍÕ±Ð¤(€€€Íå¹Œ€ôMÑ…Ñ”¹Íå¹}Ñ¡É•Í¡½±‘Í}™É½µ}…¡”¡Q!IM!=1M}!¤(€€€É•ÍÕ±Ñl‰Ñ¡É•Í¡½±‘Ì‰t€ôÍå¹l‰ÍÑ…Ñ”‰t¹•Ð ‰Ñ¡É•Í¡½±‘Ìˆ°íô¤(4(€€€€ŒƒžR£–*£ššVÃš6»šz–îè¡½ÑÍÁ½Ñ}…¹…±åÍ¥Ì4(€€€µ•Ñ„€ôÁ¥Á•±¥¹•}É•ÍÕ±Ð¹•Ð ‰}Á¥Á•±¥¹•}µ•Ñ„ˆ°íô¤4(€€€Á½­•Ñ}‘¥™˜€ôÉ•ÍÕ±Ñl‰Á½­•Ñ}‘¥™™•É•¹•Ì‰t4(€€€Á‘‰}±¥ÍÐ€ôµ•Ñ„¹•Ð ‰‘å¹…µ¥}Á‘‰}±¥ÍÐˆ°mt¤4(€€€¥˜¹½ÐÁ‘‰}±¥ÍÐè4(€€€€€€€Á‘‰}±¥ÍÐ€ôYI%%}AAQ%}=5A1aMl‰54È‰t€¬YI%%}AAQ%}=5A1aMl‰55`‰t4(4(€€€¡½ÑÍÁ½Ñ}…¹…±åÍ¥Ì€ôì4(€€€€€€€€‰Á‘‰}±¥ÍÐˆèÁ‘‰}±¥ÍÐ°4(€€€€€€€€‰¹}µ‘´É}Á•ÁÑ¥‘•}½µÁ±•á•Ìˆèµ•Ñ„¹•Ð ‰¹}µ‘´É}ÍÑÉÕÑÕÉ•Ìˆ°±•¸¡YI%%}AAQ%}=5A1aMl‰54È‰t¤¤°4(€€€€€€€€‰¹}µ‘µá}Á•ÁÑ¥‘•}½µÁ±•á•Ìˆèµ•Ñ„¹•Ð ‰¹}µ‘µá}ÍÑÉÕÑÕÉ•Ìˆ°±•¸¡YI%%}AAQ%}=5A1aMl‰55`‰t¤¤°4(€€€€€€€€‰µ•Ñ¡½ˆèÁ½­•Ñ}‘¥™˜¹•Ð ‰}µ•Ñ¡½ˆ°€ˆˆ¤°4(€€€€€€€€‰Á½­•Ñ}Í½ÕÉ”ˆèµ•Ñ„¹•Ð ‰Á½­•Ñ}Í½ÕÉ”ˆ°€‰½¹ÍÑ…¹Ðˆ¤°4(€€€€€€€€‰Á½­•ÑÌˆèÁ½­•Ñ}‘¥™˜°4(€€€€€€€€‰‘…Ñ…}ÅÕ…±¥Ñå}…±•ÉÐˆèQ}EU1%Qe}1IP°4(€€€€€€€€‰ÍÑ…•}ÍÑ…ÑÕÌˆèµ•Ñ„¹•Ð ‰ÍÑ…•}ÍÑ…ÑÕÌˆ°íô¤°4(€€€€€€€€‰ÉÕ¹}ÍÑ…ÑÕÌˆèµ•Ñ„¹•Ð ‰ÉÕ¹}ÍÑ…ÑÕÌˆ°€‰Õ¹­¹½Ý¸ˆ¤°4(€€€ô4(€€€Ù¥‘•¹•1½•È¹É•Í•…É¡}½µÁ±•Ñ” 4(€€€€€€€¡½ÑÍÁ½Ñ}…¹…±åÍ¥Ìõ¡½ÑÍÁ½Ñ}…¹…±åÍ¥Ì°4(€€€€€€€­¹½Ý¹}‰¥¹‘•ÉÌõÉ•ÍÕ±Ñl‰­¹½Ý¹}‘Õ…±}‰¥¹‘•ÉÌ‰t°4(€€€€€€€É•™Ìõ1%QIQUI}IL°4(€€€€¤4(€€€É•ÑÕÉ¸É•ÍÕ±Ð4(4(4)‘•˜É•½µÁÕÑ” ¤è4(€€€É•ÑÕÉ¸ÉÕ¸¡™½É•}É•½µÁÕÑ”õQÉÕ”¤4(4(4)¥˜}}¹…µ•}|€ôô€‰}}µ…¥¹}|ˆè4(€€€½ÕÐ€ôÉÕ¸ ¤4(€€€¸€ô±•¸¡½ÕÐ¹•Ð ‰­¹½Ý¹}‘Õ…±}‰¥¹‘•ÉÌˆ°mt¤¤4(€€€ÁÉ¥¹Ð¡˜‰mÉ•Í•…É¡tMÑ…Ñ”ÕÁ‘…Ñ•¸í¹ô‘Õ…°‰¥¹‘•ÉÌ¸A¡…Í”õíMÑ…Ñ”¹±½… ¤¹•Ð Á¡…Í”œ¥ôˆ¤4