"""Truthful HTTP adapter for the CycPep Studio web UI.

This process is the only browser-facing component allowed to read the shared
runtime files.  It deliberately delegates state, candidate, and evidence reads
to data_layer.py instead of maintaining a second dashboard database.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data_layer import CandidateIndex, EvidenceLogger, State  # noqa: E402
from target_bootstrap import (  # noqa: E402
    BootstrapError,
    ReviewRequiredError,
    TargetBootstrapper,
    approve_draft,
    edit_target_draft,
    select_resolved_candidate,
)

STORE = Path(os.environ.get("CYCPEP_WEB_STORE", ROOT / "data" / "web_api"))
DRAFTS = STORE / "drafts"
COORDINATES = Path(os.environ.get("CYCPEP_TARGET_ROOT", ROOT / "data" / "targets"))
CONNECTIONS: dict[str, dict] = {}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
ARTIFACTS: dict[str, dict] = {}
HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:]+\])$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")


def _draft_path(draft_id: str) -> Path:
    if not re.fullmatch(r"drf_[A-Za-z0-9]+", draft_id):
        raise FileNotFoundError(draft_id)
    return DRAFTS / f"{draft_id}.json"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _truthy(value) -> bool:
    return value is True or str(value).casefold() == "true"


def _normalise_sha256(value) -> str:
    return str(value or "").removeprefix("sha256:").lower()


def _bind_host_is_loopback(host: str) -> bool:
    if str(host).casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _artifact_roots() -> list[Path]:
    configured = os.environ.get("CYCPEP_ARTIFACT_ROOTS")
    roots = configured.split(os.pathsep) if configured else [str(ROOT)]
    return [Path(root).expanduser().resolve() for root in roots if root]


def _register_coordinate_artifact(row: dict) -> str | None:
    """Register a hash-verified, manifest-bound coordinate without exposing its path."""
    if not _truthy(row.get("all_layers_pass")):
        return None
    candidate_id = row.get("candidate_id")
    sequence = row.get("sequence")
    coordinate = row.get("design_pdb_path")
    expected = _normalise_sha256(row.get("design_pdb_hash"))
    manifest = row.get("manifest_path")
    if not candidate_id or not sequence or not coordinate or not expected or not manifest:
        return None
    path = Path(coordinate).expanduser()
    manifest_path = Path(manifest).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    if not manifest_path.is_absolute():
        manifest_path = ROOT / manifest_path
    try:
        path = path.resolve(strict=True)
        manifest_path = manifest_path.resolve(strict=True)
        if not any(path.is_relative_to(root) and manifest_path.resolve().is_relative_to(root)
                   for root in _artifact_roots()):
            return None
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, RuntimeError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(manifest_payload, dict):
        return None
    if str(manifest_payload.get("candidate_id")) != str(candidate_id):
        return None
    if manifest_payload.get("sequence") != sequence:
        return None
    if manifest_payload.get("length") not in (None, len(sequence)):
        return None
    manifest_coordinate = manifest_payload.get("refold_pdb")
    if not manifest_coordinate:
        return None
    manifest_coordinate_path = Path(manifest_coordinate).expanduser()
    if not manifest_coordinate_path.is_absolute():
        manifest_coordinate_path = manifest_path.parent / manifest_coordinate_path
    try:
        if manifest_coordinate_path.resolve(strict=True) != path:
            return None
    except (OSError, RuntimeError):
        return None
    if _normalise_sha256(manifest_payload.get("refold_pdb_hash")) != expected:
        return None
    declared_manifest = manifest_payload.get("manifest_path")
    if declared_manifest:
        declared_manifest_path = Path(declared_manifest).expanduser()
        if not declared_manifest_path.is_absolute():
            declared_manifest_path = manifest_path.parent / declared_manifest_path
        try:
            if declared_manifest_path.resolve(strict=True) != manifest_path:
                return None
        except (OSError, RuntimeError):
            return None
    if path.suffix.casefold() not in {".pdb", ".cif", ".mmcif"}:
        return None
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        return None
    artifact_id = "art_" + hashlib.sha256(
        f"{candidate_id}:{actual}".encode()
    ).hexdigest()[:24]
    ARTIFACTS[artifact_id] = {
        "path": path, "sha256": actual,
        "format": "pdb" if path.suffix.casefold() == ".pdb" else "cif",
        "candidate_id": candidate_id,
    }
    return artifact_id


def _candidate_payload(row: dict, *, allow_artifacts: bool = True) -> dict:
    layers = [_truthy(row.get(f"l{i}_pass")) for i in range(1, 8)]
    artifact_id = _register_coordinate_artifact(row) if allow_artifacts else None
    return {
        "candidate_id": row.get("candidate_id"),
        "sequence": row.get("sequence"),
        "source_route": row.get("source_route"),
        "final_status": row.get("final_status"),
        "layers": layers,
        "all_layers_pass": _truthy(row.get("all_layers_pass")),
        "artifact_id": artifact_id,
        "last_updated": row.get("last_updated"),
    }


def local_snapshot() -> dict:
    if not os.environ.get("CYCPEP_PROJECT_CONFIG") or not os.environ.get("CYCPEP_DATA_DIR"):
        return {
            "source": {"mode": "local", "connected": True},
            "project": {"project_id": None, "name": None, "config": None, "targets": []},
            "state": {"phase": None, "round": None, "candidate_count": 0,
                      "iteration_history": [], "thresholds_ready": False, "workflow": {}},
            "stats": {"total_candidates": 0, "all_layers_pass": 0, "finalized": 0},
            "candidates": [], "recent_evidence": [],
            "integrity_warnings": ["è¯·å…ˆé€‰æ‹©ä¸€ä¸ªé¡¹ç›®è¿è¡Œç›®å½•"],
        }
    state = State.load()
    rows = CandidateIndex.load()
    evidence = EvidenceLogger.get_all()
    config = state.get("project_config")
    warnings = []
    if not state.get("project_id"):
        warnings.append("state_missing_project_id")
    if not isinstance(config, dict):
        warnings.append("state_missing_project_config")
    if not state.get("approved_digest"):
        warnings.append("state_missing_approved_digest")
    return {
        "source": {"mode": "local", "state_path": "opaque", "connected": True},
        "project": {
            "project_id": state.get("project_id"),
            "name": state.get("project"),
            "config": config,
            "targets": list((state.get("targets") or {}).keys()),
        },
        "state": {
            "phase": state.get("phase"),
            "round": state.get("round"),
            "candidate_count": state.get("candidate_count"),
            "iteration_history": state.get("iteration_history") or [],
            "thresholds_ready": bool(state.get("thresholds")),
        },
        "stats": CandidateIndex.stats(),
        "candidates": [_candidate_payload(row) for row in rows],
        "recent_evidence": evidence[-100:],
        "integrity_warnings": warnings,
    }


def _ssh_key(alias: str) -> str:
    if not ALIAS_RE.fullmatch(alias):
        raise ValueError("invalid key alias")
    value = os.environ.get(f"CYCPEP_SSH_KEY_{alias.upper()}")
    if not value:
        raise ValueError(f"server has no key registered for alias {alias}")
    path = Path(value)
    if not path.is_file():
        raise ValueError("registered SSH key is unavailable")
    return str(path)


def _validate_ssh_profile(profile: dict) -> dict:
    host = str(profile.get("host", "")).strip()
    user = str(profile.get("username", "")).strip()
    alias = str(profile.get("key_alias", "")).strip()
    password = str(profile.get("password", ""))
    root = str(profile.get("workspace_root", "")).strip()
    port = int(profile.get("port", 22))
    if not HOST_RE.fullmatch(host) or not USER_RE.fullmatch(user):
        raise ValueError("invalid SSH host or username")
    if not 1 <= port <= 65535 or not root.startswith(("/", "~")):
        raise ValueError("invalid port or remote workspace root")
    if len(password) > 1024:
        raise ValueError("SSH password is too long")
    key_path = _ssh_key(alias) if alias else None
    validated = {"host": host, "username": user, "port": port, "key_alias": alias,
                 "password": password, "workspace_root": root, "key_path": key_path}
    for field in (
        "project_config_path", "data_dir", "evidence_dir", "work_root",
        "core_python", "design_python", "prediction_python",
    ):
        value = str(profile.get(field, "")).strip()
        if value:
            validated[field] = value
    return validated


def _ssh_json(profile: dict, remote_code: str, *, command_timeout: int = 30) -> dict:
    """Execute fixed backend-generated Python over SSH and decode one JSON object."""
    try:
        import paramiko
    except ImportError as exc:
        raise RuntimeError("SSH è¿žæŽ¥éœ€è¦å®‰è£… requirements.txt ä¸­çš„ paramiko") from exc
    encoded = base64.b64encode(remote_code.encode()).decode()
    launcher = "import base64;exec(base64.b64decode('" + encoded + "'))"
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=profile["host"], port=profile["port"], username=profile["username"],
            password=profile.get("password") or None, key_filename=profile.get("key_path"),
            look_for_keys=not bool(profile.get("password")),
            allow_agent=not bool(profile.get("password")),
            timeout=10, banner_timeout=10, auth_timeout=10,
        )
        _, stdout, stderr = client.exec_command(
            f'python3 -c "{launcher}"', timeout=command_timeout
        )
        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace").strip()
        status = stdout.channel.recv_exit_status()
        if status:
            raise RuntimeError((error or "SSH command failed")[-1200:])
        value = json.loads(output)
        if not isinstance(value, dict):
            raise RuntimeError("SSH endpoint returned a non-object payload")
        return value
    finally:
        client.close()


def _remote_context(profile: dict) -> str:
    payload = {
        key: profile.get(key) for key in (
            "workspace_root", "project_config_path", "data_dir", "evidence_dir",
            "work_root", "core_python", "design_python", "prediction_python",
        )
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def ssh_snapshot(profile: dict) -> dict:
    profile = _validate_ssh_profile(profile)
    context_b64 = _remote_context(profile)
    remote_code = f'''import base64,json,os,sys
from pathlib import Path
ctx=json.loads(base64.b64decode("{context_b64}").decode())
root=Path(os.path.expanduser(ctx["workspace_root"])).resolve()
for env_key,ctx_key in (("CYCPEP_PROJECT_CONFIG","project_config_path"),("CYCPEP_DATA_DIR","data_dir"),("CYCPEP_EVIDENCE_DIR","evidence_dir")):
 if ctx.get(ctx_key): os.environ[env_key]=ctx[ctx_key]
sys.path.insert(0,str(root))
if ctx.get("project_config_path") and ctx.get("data_dir") and ctx.get("evidence_dir"):
 from data_layer import State,CandidateIndex,EvidenceLogger
 state=State.load(); rows=CandidateIndex.load(); events=EvidenceLogger.get_all()[-100:]
else:
 state={{}}; rows=[]; events=[]
projects=[]
for config_path in sorted((root/"projects").glob("*.json")):
 try:
  config=json.loads(config_path.read_text(encoding="utf-8"))
  if not config.get("project_id"): continue
  slug=config_path.stem; state_path=root/"data"/"projects"/slug/"state.json"
  project_state=json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {{}}
  meta=project_state.get("research_pipeline_meta") or {{}}
  projects.append({{"slug":slug,"project_id":config.get("project_id"),"name":config.get("name") or config.get("project_id"),"targets":[t.get("id") for t in config.get("targets",[]) if isinstance(t,dict) and t.get("id")],"has_runtime":bool(state_path.is_file()),"phase":project_state.get("phase"),"run_status":meta.get("run_status"),"last_updated":__import__("datetime").datetime.fromtimestamp(state_path.stat().st_mtime,__import__("datetime").timezone.utc).isoformat() if state_path.is_file() else None}})
 except (OSError,ValueError,TypeError): pass
workflow={{}}
work_root=Path(ctx.get("work_root") or ((Path(ctx["data_dir"])/"autopilot") if ctx.get("data_dir") else root/"data"/"autopilot"))
status_path=work_root/"autopilot_status.json"; process_path=work_root/"autopilot_process.json"; log_path=work_root/"autopilot.log"
if status_path.is_file():
 try: workflow=json.loads(status_path.read_text(encoding="utf-8"))
 except (OSError,ValueError): workflow={{"status":"invalid_status","error":{{"message":"autopilot_status.json æ— æ³•è¯»å–"}}}}
if process_path.is_file():
 try:
  process=json.loads(process_path.read_text(encoding="utf-8")); pid=int(process.get("pid") or 0)
  process["alive"]=pid>0 and Path("/proc") .joinpath(str(pid)).exists(); workflow["process"]=process
 except (OSError,ValueError,TypeError): pass
if log_path.is_file():
 try: workflow["log_tail"]="\\n".join(log_path.read_text(encoding="utf-8",errors="replace").splitlines()[-120:])
 except OSError: pass
process_logs=[]
try:
 for candidß½y¶‰žËkºwµçH°€‰‘…Ñ…}‘¥Èˆ°€‰•Ù¥‘•¹•}‘¥Èˆ°€‰Ý½É­}É½½Ðˆ°4(€€€€€€€€‰½É•}ÁåÑ¡½¸ˆ°€‰‘•Í¥¹}ÁåÑ¡½¸ˆ°€‰ÁÉ•‘¥Ñ¥½¹}ÁåÑ¡½¸ˆ°4(€€€€¥ô°€¨©±¥µ¥ÑÌ°€‰…ÁÁÉ½Ù•ÈˆèÍÑÈ¡É•ÅÕ•ÍÐ¹•Ð ‰…ÁÁÉ½Ù•Èˆ¤½È€‰Ý•ˆµÕÍ•Èˆ¥lèÄÈát°4(€€€€€€€€‰©ÕÍÑ¥™¥…Ñ¥½¸ˆèÍÑÈ¡É•ÅÕ•ÍÐ¹•Ð ‰©ÕÍÑ¥™¥…Ñ¥½¸ˆ¤½È€‰UÍ•È…ÁÁÉ½Ù•™Õ±°…ÕÑ½µ…Ñ¥ŒÝ½É­™±½Ü¥¸åA•ÀMÑÕ‘¥¼ˆ¥lèÔÀÁuô4(€€€Á…å±½…‘}ˆØÐ€ô‰…Í”ØÐ¹ˆØÑ•¹½‘”¡©Í½¸¹‘ÕµÁÌ¡Á…å±½…¤¹•¹½‘” ¤¤¹‘•½‘” ¤4(€€€É•µ½Ñ•}½‘”€ô˜œœ¥µÁ½ÉÐ‰…Í”ØÐ±©Í½¸±½Ì±ÍÕ‰ÁÉ½•ÍÌ±ÍåÌ±Ñ¥µ”4)™É½´Á…Ñ¡±¥ˆ¥µÁ½ÉÐA…Ñ 4)Àõ©Í½¸¹±½…‘Ì¡‰…Í”ØÐ¹ˆØÑ‘•½‘” ‰íÁ…å±½…‘}ˆØÑôˆ¤¹‘•½‘” ¤¤ìÉ½½ÐõA…Ñ ¡½Ì¹Á…Ñ ¹•áÁ…¹‘ÕÍ•È¡Ál‰Ý½É­ÍÁ…•}É½½Ð‰t¤¤¹É•Í½±Ù” ¤ìÝ½É¬õA…Ñ ¡Ál‰Ý½É­}É½½Ð‰t¤¹É•Í½±Ù” ¤ìÝ½É¬¹µ­‘¥È¡Á…É•¹ÑÌõQÉÕ”±•á¥ÍÑ}½¬õQÉÕ”¤ìÁÉ½•ÍÍ}™¥±”õÝ½É¬¼‰…ÕÑ½Á¥±½Ñ}ÁÉ½•ÍÌ¹©Í½¸ˆ4)¥˜ÁÉ½•ÍÍ}™¥±”¹¥Í}™¥±” ¤è4(½±õ©Í½¸¹±½…‘Ì¡ÁÉ½•ÍÍ}™¥±”¹É•…‘}Ñ•áÐ¡•¹½‘¥¹œô‰ÕÑ˜´àˆ¤¤ì½±‘}Á¥õ¥¹Ð¡½±¹•Ð ‰Á¥ˆ¤½È€À¤4(¥˜½±‘}Á¥øÀ…¹A…Ñ  ˆ½ÁÉ½Œˆ¤¹©½¥¹Á…Ñ ¡ÍÑÈ¡½±‘}Á¥¤¤¹•á¥ÍÑÌ ¤èÉ…¥Í”IÕ¹Ñ¥µ•ÉÉ½È ‹¢¾—¦†çžn»–ÞËšr'–Þ—’ösšÖš¶–r£¢þC¢†0ˆ¤4)‘•™…Õ±ÑÌõíì‰½É•}ÁåÑ¡½¸ˆèˆ½É½½Ð½‘…µ½‘•°µÑµÀ½•¹ÙÌ½¹½Ù…Á•ÁÑ¥‘”µ½É”½‰¥¸½ÁåÑ¡½¸ˆ°‰‘•Í¥¹}ÁåÑ¡½¸ˆèˆ½É½½Ð½‘…µ½‘•°µÑµÀ½•¹ÙÌ½É™‘¥™™ÕÍ¥½¸µ‘•Í¥¸½‰¥¸½ÁåÑ¡½¸ˆ°‰ÁÉ•‘¥Ñ¥½¹}ÁåÑ¡½¸ˆèˆ½É½½Ð½‘…µ½‘•°µÑµÀ½•¹ÙÌ½åÁ•ÀµÁÉ•‘¥Ñ¥½¸½‰¥¸½ÁåÑ¡½¸‰õô4)™½È­•ä±Ù…±Õ”¥¸‘•™…Õ±ÑÌ¹¥Ñ•µÌ ¤è4(¥˜¹½ÐÀ¹•Ð¡­•ä¤…¹A…Ñ ¡Ù…±Õ”¤¹¥Í}™¥±” ¤èÁm­•åtõÙ…±Õ”4)ÁåÑ¡½¸õÀ¹•Ð ‰½É•}ÁåÑ¡½¸ˆ¤¥˜À¹•Ð ‰½É•}ÁåÑ¡½¸ˆ¤…¹A…Ñ ¡Ál‰½É•}ÁåÑ¡½¸‰t¤¹¥Í}™¥±” ¤•±Í”ÍåÌ¹•á•ÕÑ…‰±”4)…ÉØõmÁåÑ¡½¸°ˆµ´ˆ°‰•á•ÕÑ¥½¸¹…ÕÑ½Á¥±½Ðˆ°ˆ´µÁÉ½©•Ðµ½¹™¥œˆ±Ál‰ÁÉ½©•Ñ}½¹™¥}Á…Ñ ‰t°ˆ´µ‘…Ñ„µ‘¥Èˆ±Ál‰‘…Ñ…}‘¥È‰t°ˆ´µ•Ù¥‘•¹”µ‘¥Èˆ±Ál‰•Ù¥‘•¹•}‘¥È‰t°ˆ´µÝ½É¬µÉ½½Ðˆ±Ál‰Ý½É­}É½½Ð‰t°ˆ´µ…ÁÁÉ½Ù•Èˆ±Ál‰…ÁÁÉ½Ù•È‰t°ˆ´µ©ÕÍÑ¥™¥…Ñ¥½¸ˆ±Ál‰©ÕÍÑ¥™¥…Ñ¥½¸‰t°ˆ´µµ…àµ‘•Í¥¸µÁÉ½Á½Í…±Ìˆ±ÍÑÈ¡Ál‰µ…á}‘•Í¥¹}ÁÉ½Á½Í…±Ì‰t¤°ˆ´µµ…àµÁÉ•‘¥Ñ¥½¸µ…¹‘¥‘…Ñ•Ìˆ±ÍÑÈ¡Ál‰µ…á}ÁÉ•‘¥Ñ¥½¹}…¹‘¥‘…Ñ•Ì‰t¤°ˆ´µµ…àµÁÔµµ¥¹ÕÑ•Ìˆ±ÍÑÈ¡Ál‰µ…á}ÁÕ}µ¥¹ÕÑ•Ì‰t¤°ˆ´µµ…àµÉ½Õ¹‘Ìˆ±ÍÑÈ¡Ál‰µ…á}É½Õ¹‘Ì‰t¥t4)™½È™±…œ±­•ä¥¸€  ˆ´µ½É”µÁåÑ¡½¸ˆ°‰½É•}ÁåÑ¡½¸ˆ¤° ˆ´µ‘•Í¥¸µÁåÑ¡½¸ˆ°‰‘•Í¥¹}ÁåÑ¡½¸ˆ¤° ˆ´µÁÉ•‘¥Ñ¥½¸µÁåÑ¡½¸ˆ°‰ÁÉ•‘¥Ñ¥½¹}ÁåÑ¡½¸ˆ¤¤è4(¥˜À¹•Ð¡­•ä¤è…ÉØ¹•áÑ•¹¡m™±…œ±Ám­•åut¤4)¡¥±‘}•¹Øõ½Ì¹•¹Ù¥É½¸¹½Áä ¤ì•¹Ù}™¥±”õA…Ñ ¹¡½µ” ¤¼ˆ¹½¹™¥œˆ¼‰åÁ•Àˆ¼‰ÉÕ¹Ñ¥µ”¹•¹Øˆì…±±½Ý•õíì‰MQA}A%}-dˆ°‰MQA}	M}UI0ˆ°‰=A9%}A%}-dˆ°‰=A9%}	M}UI0ˆ°‰eAA}115}A%}-dˆ°‰eAA}115}	M}UI0‰õô4)¥˜•¹Ù}™¥±”¹¥Í}™¥±” ¤è4(™½È±¥¹”¥¸•¹Ù}™¥±”¹É•…‘}Ñ•áÐ¡•¹½‘¥¹œô‰ÕÑ˜´àˆ¤¹ÍÁ±¥Ñ±¥¹•Ì ¤è4(€¥˜€ˆôˆ¹½Ð¥¸±¥¹”½È±¥¹”¹±ÍÑÉ¥À ¤¹ÍÑ…ÉÑÍÝ¥Ñ  ˆŒˆ¤è½¹Ñ¥¹Õ”4(€­•ä±Ù…±Õ”õ±¥¹”¹ÍÁ±¥Ð ˆôˆ°Ä¤4(€¥˜­•ä¹ÍÑÉ¥À ¤¥¸…±±½Ý•è¡¥±‘}•¹Ùm­•ä¹ÍÑÉ¥À ¥tõÙ…±Õ”¹ÍÑÉ¥À ¤4)±½œõ½Á•¸¡Ý½É¬¼‰…ÕÑ½Á¥±½Ð¹±½œˆ°‰„ˆ±•¹½‘¥¹œô‰ÕÑ˜´àˆ¤ìÁÉ½ŒõÍÕ‰ÁÉ½•ÍÌ¹A½Á•¸¡…ÉØ±ÝõÉ½½Ð±•¹Øõ¡¥±‘}•¹Ø±ÍÑ‘¥¸õÍÕ‰ÁÉ½•ÍÌ¹Y9U10±ÍÑ‘½ÕÐõ±½œ±ÍÑ‘•ÉÈõÍÕ‰ÁÉ½•ÍÌ¹MQ=UP±ÍÑ…ÉÑ}¹•Ý}Í•ÍÍ¥½¸õQÉÕ”±±½Í•}™‘ÌõQÉÕ”¤ìÁÉ½•ÍÍ}™¥±”¹ÝÉ¥Ñ•}Ñ•áÐ¡©Í½¸¹‘ÕµÁÌ¡íì‰Á¥ˆéÁÉ½Œ¹Á¥°‰ÍÑ…ÉÑ•‘}…ÐˆéÑ¥µ”¹Ñ¥µ” ¤°‰…±¥Ù”ˆéQÉÕ•õô±¥¹‘•¹ÐôÈ¤±•¹½‘¥¹œô‰ÕÑ˜´àˆ¤ìÁÉ¥¹Ð¡©Í½¸¹‘ÕµÁÌ¡íì‰Á¥ˆéÁÉ½Œ¹Á¥°‰ÍÑ…ÑÕÌˆè‰ÉÕ¹¹¥¹œ‰õô¤¤œœœ4(€€€É•ÑÕÉ¸}ÍÍ¡}©Í½¸¡ÁÉ½™¥±”°É•µ½Ñ•}½‘”°½µµ…¹‘}Ñ¥µ•½ÕÐôÌÀ¤4(4(4)‘•˜ÍÍ¡}ÍÑ½Á}Ý½É­™±½Ü¡ÁÉ½™¥±”è‘¥Ð¤€´ø‘¥Ðè4(€€€ÁÉ½™¥±”€ô}Ù…±¥‘…Ñ•}ÍÍ¡}ÁÉ½™¥±”¡ÁÉ½™¥±”¤4(€€€¥˜¹½ÐÁÉ½™¥±”¹•Ð ‰Ý½É­}É½½Ðˆ¤è4(€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È ‰¹¼…Ñ¥Ù”ÁÉ½©•Ðˆ¤4(€€€Ý½É­}ˆØÐ€ô‰…Í”ØÐ¹ˆØÑ•¹½‘”¡ÁÉ½™¥±•l‰Ý½É­}É½½Ð‰t¹•¹½‘” ¤¤¹‘•½‘” ¤4(€€€É•µ½Ñ•}½‘”€ô˜œœ¥µÁ½ÉÐ‰…Í”ØÐ±©Í½¸±½Ì±Í¥¹…°±Ñ¥µ”4)™É½´Á…Ñ¡±¥ˆ¥µÁ½ÉÐA…Ñ 4)Ý½É¬õA…Ñ ¡‰…Í”ØÐ¹ˆØÑ‘•½‘” ‰íÝ½É­}ˆØÑôˆ¤¹‘•½‘” ¤¤¹É•Í½±Ù” ¤ìÁÉ½•ÍÍ}™¥±”õÝ½É¬¼‰…ÕÑ½Á¥±½Ñ}ÁÉ½•ÍÌ¹©Í½¸ˆ4)¥˜¹½ÐÁÉ½•ÍÍ}™¥±”¹¥Í}™¥±” ¤èÁÉ¥¹Ð¡©Í½¸¹‘ÕµÁÌ¡íì‰ÍÑ…ÑÕÌˆè‰¹½Ñ}ÉÕ¹¹¥¹œ‰õô¤¤4)•±Í”è4(Àõ©Í½¸¹±½…‘Ì¡ÁÉ½•ÍÍ}™¥±”¹É•…‘}Ñ•áÐ¡•¹½‘¥¹œô‰ÕÑ˜´àˆ¤¤ìÁ¥õ¥¹Ð¡À¹•Ð ‰Á¥ˆ¤½È€À¤ì…±¥Ù”õÁ¥øÀ…¹A…Ñ  ˆ½ÁÉ½Œˆ¤¹©½¥¹Á…Ñ ¡ÍÑÈ¡Á¥¤¤¹•á¥ÍÑÌ ¤4(¥˜…±¥Ù”è½Ì¹­¥±±Áœ¡Á¥±Í¥¹…°¹M%QI4¤4(À¹ÕÁ‘…Ñ”¡íì‰…±¥Ù”ˆé…±Í”°‰ÍÑ½ÁÁ•‘}…ÐˆéÑ¥µ”¹Ñ¥µ” ¥õô¤ìÁÉ½•ÍÍ}™¥±”¹ÝÉ¥Ñ•}Ñ•áÐ¡©Í½¸¹‘ÕµÁÌ¡À±¥¹‘•¹ÐôÈ¤±•¹½‘¥¹œô‰ÕÑ˜´àˆ¤4(ÍÑ…ÑÕÍ}™¥±”õÝ½É¬¼‰…ÕÑ½Á¥±½Ñ}ÍÑ…ÑÕÌ¹©Í½¸ˆ4(¥˜ÍÑ…ÑÕÍ}™¥±”¹¥Í}™¥±” ¤è4(€ÑÉäè4(€€ÍÑ…ÑÕÌõ©Í½¸¹±½…‘Ì¡ÍÑ…ÑÕÍ}™¥±”¹É•…‘}Ñ•áÐ¡•¹½‘¥¹œô‰ÕÑ˜´àˆ¤¤ìÍÑ…ÑÕÌ¹ÕÁ‘…Ñ”¡íì‰ÍÑ…ÑÕÌˆè‰ÍÑ½ÁÁ•ˆ°‰ÕÁ‘…Ñ•‘}…Ðˆé}}¥µÁ½ÉÑ}| ‰‘…Ñ•Ñ¥µ”ˆ¤¹‘…Ñ•Ñ¥µ”¹¹½Ü¡}}¥µÁ½ÉÑ}| ‰‘…Ñ•Ñ¥µ”ˆ¤¹Ñ¥µ•é½¹”¹ÕÑŒ¤¹¥Í½™½Éµ…Ð ¥õô¤ìÍÑ…ÑÕÍ}™¥±”¹ÝÉ¥Ñ•}Ñ•áÐ¡©Í½¸¹‘ÕµÁÌ¡ÍÑ…ÑÕÌ±•¹ÍÕÉ•}…Í¥¤õ…±Í”±¥¹‘•¹ÐôÈ¤±•¹½‘¥¹œô‰ÕÑ˜´àˆ¤4(€•á•ÁÐ€¡=MÉÉ½È±Y…±Õ•ÉÉ½È±QåÁ•ÉÉ½È¤èÁ…ÍÌ4(ÁÉ¥¹Ð¡©Í½¸¹‘ÕµÁÌ¡íì‰ÍÑ…ÑÕÌˆè‰ÍÑ½ÁÁ•ˆ¥˜…±¥Ù”•±Í”€‰¹½Ñ}ÉÕ¹¹¥¹œˆ°‰Á¥ˆéÁ¥‘õô¤¤œœœ4(€€€É•ÑÕÉ¸}ÍÍ¡}©Í½¸¡ÁÉ½™¥±”°É•µ½Ñ•}½‘”¤4(4(4)±…ÍÌ!…¹‘±•È¡	…Í•!QQAI•ÅÕ•ÍÑ!…¹‘±•È¤è(€€€Í•ÉÙ•É}Ù•ÉÍ¥½¸€ô€‰åA•Á]•‰‘…ÁÑ•È¼À¸Äˆ((€€€‘•˜}½ÉÍ}½É¥¥¸¡Í•±˜¤€´øÍÑÈè(€€€€€€€É•ÅÕ•ÍÑ•€ôÍ•±˜¹¡•…‘•ÉÌ¹•Ð ‰=É¥¥¸ˆ°€ˆˆ¤(€€€€€€€½¹™¥ÕÉ•€ô½Ì¹•¹Ù¥É½¸¹•Ð ‰eAA}U%}=I%%8ˆ°€‰¡ÑÑÀè¼¼ÄÈÜ¸À¸À¸ÄèÐÄÜÌˆ¤(€€€€€€€…±±½Ý•€ôí½¹™¥ÕÉ•°€‰¡ÑÑÀè¼¼ÄÈÜ¸À¸À¸ÄèÐÄÜÌˆ°€‰¡ÑÑÀè¼½±½…±¡½ÍÐèÐÄÜÌˆ°€‰¡ÑÑÀè¼½±½…±¡½ÍÐèÌÀÀÀ‰ô(€€€€€€€É•ÑÕÉ¸É•ÅÕ•ÍÑ•¥˜É•ÅÕ•ÍÑ•¥¸…±±½Ý••±Í”½¹™¥ÕÉ•(4(€€€‘•˜}©Í½¸¡Í•±˜°ÍÑ…ÑÕÌè¥¹Ð°‘…Ñ„õ9½¹”°•ÉÉ½Èõ9½¹”¤è4(€€€€€€€‰½‘ä€ôì‰É•ÅÕ•ÍÑ}¥ˆè˜‰É•Å}íÕÕ¥¹ÕÕ¥Ð ¤¹¡•álèÄÉuô‰ô4(€€€€€€€‰½‘ål‰‘…Ñ„ˆ¥˜•ÉÉ½È¥Ì9½¹”•±Í”€‰•ÉÉ½È‰t€ô‘…Ñ„¥˜•ÉÉ½È¥Ì9½¹”•±Í”•ÉÉ½È4(€€€€€€€•¹½‘•€ô©Í½¸¹‘ÕµÁÌ¡‰½‘ä°•¹ÍÕÉ•}…Í¥¤õ…±Í”¤¹•¹½‘” ¤4(€€€€€€€Í•±˜¹Í•¹‘}É•ÍÁ½¹Í”¡ÍÑ…ÑÕÌ¤4(€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰½¹Ñ•¹ÐµQåÁ”ˆ°€‰…ÁÁ±¥…Ñ¥½¸½©Í½¸ì¡…ÉÍ•ÐõÕÑ˜´àˆ¤4(€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰…¡”µ½¹ÑÉ½°ˆ°€‰¹¼µÍÑ½É”ˆ¤4(€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰•ÍÌµ½¹ÑÉ½°µ±±½Üµ=É¥¥¸ˆ°Í•±˜¹}½ÉÍ}½É¥¥¸ ¤¤(€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰½¹Ñ•¹Ðµ1•¹Ñ ˆ°ÍÑÈ¡±•¸¡•¹½‘•¤¤¤4(€€€€€€€Í•±˜¹•¹‘}¡•…‘•ÉÌ ¤ìÍ•±˜¹Ý™¥±”¹ÝÉ¥Ñ”¡•¹½‘•¤4(4(€€€‘•˜}‰½‘ä¡Í•±˜¤è4(€€€€€€€Í¥é”€ô¥¹Ð¡Í•±˜¹¡•…‘•ÉÌ¹•Ð ‰½¹Ñ•¹Ðµ1•¹Ñ ˆ°€ˆÀˆ¤¤4(€€€€€€€É•ÑÕÉ¸©Í½¸¹±½…‘Ì¡Í•±˜¹É™¥±”¹É•…¡Í¥é”¤½Èˆ‰íôˆ¤4(4(€€€‘•˜‘½}=AQ%=9L¡Í•±˜¤è4(€€€€€€€Í•±˜¹Í•¹‘}É•ÍÁ½¹Í” ÈÀÐ¤4(€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰•ÍÌµ½¹ÑÉ½°µ±±½Üµ=É¥¥¸ˆ°Í•±˜¹}½ÉÍ}½É¥¥¸ ¤¤(€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰•ÍÌµ½¹ÑÉ½°µ±±½Üµ!•…‘•ÉÌˆ°€‰½¹Ñ•¹ÐµQåÁ”ˆ¤4(€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰•ÍÌµ½¹ÑÉ½°µ±±½Üµ5•Ñ¡½‘Ìˆ°€‰P±A=MP±AQ ±=AQ%=9Lˆ¤4(€€€€€€€Í•±˜¹•¹‘}¡•…‘•ÉÌ ¤4(4(€€€‘•˜‘½}P¡Í•±˜¤è4(€€€€€€€Á…Ñ €ôÕÉ±Á…ÉÍ”¡Í•±˜¹Á…Ñ ¤¹Á…Ñ 4(€€€€€€€ÑÉäè4(€€€€€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½ØÄ½¡•…±Ñ ˆè4(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÈÀÀ°ì‰ÍÑ…ÑÕÌˆè€‰½¬ˆ°€‰…‘…ÁÑ•Èˆè€‰±½…°‰ô¤4(€€€€€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½ØÄ½ÁÉ½©•ÑÌˆè4(€€€€€€€€€€€€€€€ÍÑ…Ñ”€ôMÑ…Ñ”¹±½… ¤ìÁÉ½©•ÑÌ€ômì‰­¥¹ˆè€‰ÉÕ¹Ñ¥µ”ˆ°€‰ÁÉ½©•Ñ}¥ˆèÍÑ…Ñ”¹•Ð ‰ÁÉ½©•Ñ}¥ˆ¤°€‰¹…µ”ˆèÍÑ…Ñ”¹•Ð ‰ÁÉ½©•Ðˆ¥õt4(€€€€€€€€€€€€€€€¥˜IQL¹•á¥ÍÑÌ ¤è4(€€€€€€€€€€€€€€€€€€€ÁÉ½©•ÑÌ€¬ômì‰­¥¹ˆè€‰‘É…™Ðˆ°€‰‘É…™Ñ}¥ˆèÀ¹ÍÑ•´°€‰ÁÉ½©•Ñ}¥ˆè¹•Ð ‰ÁÉ½©•Ñ}¥ˆ¤°€‰¹…µ”ˆè¹•Ð ‰¹…µ”ˆ¥ô4(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€™½ÈÀ¥¸IQL¹±½ˆ ‰‘É™|¨¹©Í½¸ˆ¤™½È¥¸m}É•…‘}©Í½¸¡À¥ut4(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÈÀÀ°ÁÉ½©•ÑÌ¤4(€€€€€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½ØÄ½Í¹…ÁÍ¡½Ðˆè4(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÈÀÀ°±½…±}Í¹…ÁÍ¡½Ð ¤¤4(€€€€€€€€€€€…ÉÑ¥™…Ñ}µ…Ñ €ôÉ”¹™Õ±±µ…Ñ ¡Èˆ½…Á¤½ØÄ½…ÉÑ¥™…ÑÌ¼¡…ÉÑ}m„µ˜À´åuìÈÑô¤½½½É‘¥¹…Ñ•Ìˆ°Á…Ñ ¤4(€€€€€€€€€€€¥˜…ÉÑ¥™…Ñ}µ…Ñ è4(€€€€€€€€€€€€€€€…ÉÑ¥™…Ð€ôIQ%QL¹•Ð¡…ÉÑ¥™…Ñ}µ…Ñ ¹É½ÕÀ Ä¤¤4(€€€€€€€€€€€€€€€¥˜¹½Ð…ÉÑ¥™…Ðè4(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÐÀÐ°•ÉÉ½Èõì‰½‘”ˆè€‰…ÉÑ¥™…Ñ}¹½Ñ}™½Õ¹ˆ°€‰µ•ÍÍ…”ˆè€‰Y•É¥™¥•…ÉÑ¥™…Ð¥Ì¹½ÐÉ•¥ÍÑ•É•‰ô¤4(€€€€€€€€€€€€€€€Á…å±½…€ô…ÉÑ¥™…Ñl‰Á…Ñ ‰t¹É•…‘}‰åÑ•Ì ¤4(€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}É•ÍÁ½¹Í” ÈÀÀ¤4(€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰½¹Ñ•¹ÐµQåÁ”ˆ°€‰¡•µ¥…°½àµÁ‘ˆˆ¥˜…ÉÑ¥™…Ñl‰™½Éµ…Ð‰t€ôô€‰Á‘ˆˆ•±Í”€‰¡•µ¥…°½àµµµ¥˜ˆ¤4(€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰`µÉÑ¥™…ÐµM!ÈÔØˆ°…ÉÑ¥™…Ñl‰Í¡„ÈÔØ‰t¤4(€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰…¡”µ½¹ÑÉ½°ˆ°€‰ÁÉ¥Ù…Ñ”°¹¼µÍÑ½É”ˆ¤4(€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰•ÍÌµ½¹ÑÉ½°µ±±½Üµ=É¥¥¸ˆ°Í•±˜¹}½ÉÍ}½É¥¥¸ ¤¤(€€€€€€€€€€€€€€€Í•±˜¹Í•¹‘}¡•…‘•È ‰½¹Ñ•¹Ðµ1•¹Ñ ˆ°ÍÑÈ¡±•¸¡Á…å±½…¤¤¤4(€€€€€€€€€€€€€€€Í•±˜¹•¹‘}¡•…‘•ÉÌ ¤4(€€€€€€€€€€€€€€€Í•±˜¹Ý™¥±”¹ÝÉ¥Ñ”¡Á…å±½…¤4(€€€€€€€€€€€€€€€É•ÑÕÉ¸4(€€€€€€€€€€€µ…Ñ €ôÉ”¹™Õ±±µ…Ñ ¡Èˆ½…Á¤½ØÄ½ÁÉ½©•Ðµ‘É…™ÑÌ¼¡‘É™}mµi„µèÀ´åt¬¤ˆ°Á…Ñ ¤4(€€€€€€€€€€€¥˜µ…Ñ è4(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÈÀÀ°}É•…‘}©Í½¸¡}‘É…™Ñ}Á…Ñ ¡µ…Ñ ¹É½ÕÀ Ä¤¤¤¤4(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÐÀÐ°•ÉÉ½Èõì‰½‘”ˆè€‰¹½Ñ}™½Õ¹ˆ°€‰µ•ÍÍ…”ˆè€‰I½ÕÑ”¹½Ð™½Õ¹‰ô¤4(€€€€€€€•á•ÁÐ¥±•9½Ñ½Õ¹‘ÉÉ½Èè4(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÐÀÐ°•ÉÉ½Èõì‰½‘”ˆè€‰¹½Ñ}™½Õ¹ˆ°€‰µ•ÍÍ…”ˆè€‰I•Í½ÕÉ”¹½Ð™½Õ¹‰ô¤4(€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè4(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÔÀÀ°•ÉÉ½Èõì‰½‘”ˆè€‰…‘…ÁÑ•É}•ÉÉ½Èˆ°€‰µ•ÍÍ…”ˆèÍÑÈ¡•áŒ¥ô¤4(4(€€€‘•˜‘½}A=MP¡Í•±˜¤è4(€€€€€€€Á…Ñ €ôÕÉ±Á…ÉÍ”¡Í•±˜¹Á…Ñ ¤¹Á…Ñ 4(€€€€€€€ÑÉäè4(€€€€€€€€€€€‰½‘ä€ôÍ•±˜¹}‰½‘ä ¤4(€€€€€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½ØÄ½ÁÉ½©•Ðµ‘É…™ÑÌˆè4(€€€€€€€€€€€€€€€‘É…™Ñ}¥€ô˜‰‘É™}íÕÕ¥¹ÕÕ¥Ð ¤¹¡•álèÄÉuôˆ4(€€€€€€€€€€€€€€€‘É…™Ð€ôQ…É•Ñ	½½ÑÍÑÉ…ÁÁ•È ¤¹É•…Ñ•}‘É…™Ð 4(€€€€€€€€€€€€€€€€€€€¥‘•¹Ñ¥™¥•Èõ‰½‘ål‰¥‘•¹Ñ¥™¥•È‰t°¥‘•¹Ñ¥™¥•É}ÑåÁ”õ‰½‘ä¹•Ð ‰¥‘•¹Ñ¥™¥•É}ÑåÁ”ˆ°€‰…ÕÑ¼ˆ¤°4(€€€€€€€€€€€€€€€€€€€½É…¹¥Íµ}¥õ¥¹Ð¡‰½‘ä¹•Ð ‰½É…¹¥Íµ}¥ˆ°€äØÀØ¤¤°•Á¥Ñ½Á”õ‰½‘ä¹•Ð ‰•Á¥Ñ½Á”ˆ¤°4(€€€€€€€€€€€€€€€€€€€½‰©•Ñ¥Ù”õ‰½‘ä¹•Ð ‰½‰©•Ñ¥Ù”ˆ°€‰‰¥¹‘•Èˆ¤°4(€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€‘É…™Ñl‰‘É…™Ñ}¥‰t€ô‘É…™Ñ}¥4(€€€€€€€€€€€€€€€}ÝÉ¥Ñ•}©Í½¸¡}‘É…™Ñ}Á…Ñ ¡‘É…™Ñ}¥¤°‘É…™Ð¤4(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÈÀÄ°‘É…™Ð¤4(€€€€€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½ØÄ½½¹¹•Ñ¥½¹Ì½ÍÍ ˆè4(€€€€€€€€€€€€€€€Í¹…ÁÍ¡½Ð€ôÍÍ¡}Í¹…ÁÍ¡½Ð¡‰½‘ä¤4(€€€€€€€€€€€€€€€½¹¹•Ñ¥½¹}¥€ô˜‰½¹¹}íÕÕ¥¹ÕÕ¥Ð ¤¹¡•álèÄÉuôˆ4(€€€€€€€€€€€€€€€=99Q%=9Mm½¹¹•Ñ¥½¹}¥‘t€ôí¬è‰½‘åm­t™½È¬¥¸€ 4(€€€€€€€€€€€€€€€€€€€€‰¡½ÍÐˆ°€‰ÕÍ•É¹…µ”ˆ°€‰Á½ÉÐˆ°€‰­•å}…±¥…Ìˆ°€‰Á…ÍÍÝ½Éˆ°€‰Ý½É­ÍÁ…•}É½½Ðˆ°4(€€€€€€€€€€€€€€€€€€€€‰½É•}ÁåÑ¡½¸ˆ°€‰‘•Í¥¹}ÁåÑ¡½¸ˆ°€‰ÁÉ•‘¥Ñ¥½¹}ÁåÑ¡½¸ˆ°4(€€€€€€€€€€€€€€€€¤¥˜¬¥¸‰½‘åô4(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÈÀÀ°ì‰½¹¹•Ñ¥½¹}¥ˆè½¹¹•Ñ¥½¹}¥°€‰Í¹…ÁÍ¡½ÐˆèÍ¹…ÁÍ¡½Ñô¤4(€€€€€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½ØÄ½½¹¹•Ñ¥½¹Ì½ÍÍ ½Í¹…ÁÍ¡½Ðˆè4(€€€€€€€€€€€€€€€ÁÉ½™¥±”€ô=99Q%=9L¹•Ð¡‰½‘ä¹•Ð ‰½¹¹•Ñ¥½¹}¥ˆ¤¤4(€€€€€€€€€€€€€€€¥˜¹½ÐÁÉ½™¥±”èÉ…¥Í”¥±•9½Ñ½Õ¹‘ÉÉ½È ‰½¹¹•Ñ¥½¸•áÁ¥É•ˆ¤4(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÈÀÀ°ÍÍ¡}Í¹…ÁÍ¡½Ð¡ÁÉ½™¥±”¤¤4(€€€€€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½ØÄ½½¹¹•Ñ¥½¹Ì½ÍÍ ½ÁÉ½©•ÑÌ½ÍÝ¥Ñ ˆè4(€€€€€€€€€€€€€€€ÁÉ½™¥±”€ô=99Q%=9L¹•Ð¡‰½‘ä¹•Ð ‰½¹¹•Ñ¥½¹}¥ˆ¤¤4(€€€€€€€€€€€€€€€¥˜¹½ÐÁÉ½™¥±”èÉ…¥Í”¥±•9½Ñ½Õ¹‘ÉÉ½È ‰½¹¹•Ñ¥½¸•áÁ¥É•ˆ¤4(€€€€€€€€€€€€€€€É•ÍÕ±Ð€ôÍÍ¡}ÍÝ¥Ñ¡}ÁÉ½©•Ð¡ÁÉ½™¥±”°‰½‘ä¹•Ð ‰Í±Õœˆ°€ˆˆ¤¤4(€€€€€€€€€€€€€€€ÁÉ½™¥±”¹ÕÁ‘…Ñ”¡í­•äèÉ•ÍÕ±Ñm­•åt™½È­•ä¥¸€ 4(€€€€€€€€€€€€€€€€€€€€‰ÁÉ½©•Ñ}½¹™¥}Á…Ñ ˆ°€‰‘…Ñ…}‘¥Èˆ°€‰•Ù¥‘•¹•}‘¥Èˆ°€‰Ý½É­}É½½Ðˆ4(€€€€€€€€€€€€€€€€¥ô¤4(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÈÀÀ°ì¨©É•ÍÕ±Ð°€‰Í¹…ÁÍ¡½ÐˆèÍÍ¡}Í¹…ÁÍ¡½Ð¡ÁÉ½™¥±”¥ô¤4(€€€€€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½ØÄ½½¹¹•Ñ¥½¹Ì½ÍÍ ½ÁÉ½©•Ðµ‘É…™ÑÌˆè4(€€€€€€€€€€€€€€€ÁÉ½™¥±”€ô=99Q%=9L¹•Ð¡‰½‘ä¹•Ð ‰½¹¹•Ñ¥½¹}¥ˆ¤¤4(€€€€€€€€€€€€€€€¥˜¹½ÐÁÉ½™¥±”èÉ…¥Í”¥±•9½Ñ½Õ¹‘ÉÉ½È ‰½¹¹•Ñ¥½¸•áÁ¥É•ˆ¤4(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÈÀÈ°ÍÑ…ÉÑ}ÍÍ¡}‘É…™Ñ}©½ˆ¡ÁÉ½™¥±”°‰½‘ä¤¤4(€€€€€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½ØÄ½½¹¹•Ñ¥½¹Ì½ÍÍ ½ÁÉ½©•Ðµ‘É…™ÑÌ½ÍÑ…ÑÕÌˆè(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÈÀÀ°•Ñ}©½ˆ¡‰½‘ä¹•Ð ‰©½‰}¥ˆ°€ˆˆ¤¤¤(€€€€€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½ØÄ½½¹¹•Ñ¥½¹Ì½ÍÍ ½ÁÉ½©•Ðµ‘É…™ÑÌ½É•Í½±Ù•µ…¹‘¥‘…Ñ”ˆè(€€€€€€€€€€€€€€€ÁÉ½™¥±”€ô=99Q%=9L¹•Ð¡‰½‘ä¹•Ð ‰½¹¹•Ñ¥½¹}¥ˆ¤¤(€€€€€€€€€€€€€€€¥˜¹½ÐÁÉ½™¥±”èÉ…¥Í”¥±•9½Ñ½Õ¹‘ÉÉ½È ‰½¹¹•Ñ¥½¸•áÁ¥É•ˆ¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÈÀÀ°ÍÍ¡}Í•±•Ñ}É•Í½±Ù•‘}…¹‘¥‘…Ñ” (€€€€€€€€€€€€€€€€€€€ÁÉ½™¥±”°‰½‘ä¹•Ð ‰‘É…™Ñ}¥ˆ°€ˆˆ¤°‰½‘ä¹•Ð ‰…¹‘¥‘…Ñ•}É•˜ˆ°€ˆˆ¤(€€€€€€€€€€€€€€€€¤¤(€€€€€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½ØÄ½½¹¹•Ñ¥½¹Ì½ÍÍ ½ÁÉ½©•Ðµ‘É…™ÑÌ½…ÁÁÉ½Ù”ˆè(€€€€€€€€€€€€€€€ÁÉ½™¥±”€ô=99Q%=9L¹•Ð¡‰½‘ä¹•Ð ‰½¹¹•Ñ¥½¹}¥ˆ¤¤4(€€€€€€€€€€€€€€€¥˜¹½ÐÁÉ½™¥±”èÉ…¥Í”¥±•9½Ñ½Õ¹‘ÉÉ½È ‰½¹¹•Ñ¥½¸•áÁ¥É•ˆ¤4(€€€€€€€€€€€€€€€É•ÍÕ±Ð€ôÍÍ¡}…ÁÁÉ½Ù•}É•µ½Ñ•}‘É…™Ð¡ÁÉ½™¥±”°‰½‘ä¹•Ð ‰‘É…™Ñ}¥ˆ°€ˆˆ¤¤4(€€€€€€€€€€€€€€€ÁÉ½™¥±”¹ÕÁ‘…Ñ”¡í­•äèÉ•ÍÕ±Ñm­•åt™½È­•ä¥¸€ 4(€€€€€€€€€€€€€€€€€€€€‰ÁÉ½©•Ñ}½¹™¥}Á…Ñ ˆ°€‰‘…Ñ…}‘¥Èˆ°€‰•Ù¥‘•¹•}‘¥Èˆ°€‰Ý½É­}É½½Ðˆ4(€€€€€€€€€€€€€€€€¥ô¤4(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÈÀÀ°ì¨©É•ÍÕ±Ð°€‰Í¹…ÁÍ¡½ÐˆèÍÍ¡}Í¹…ÁÍ¡½Ð¡ÁÉ½™¥±”¥ô¤4(€€€€€€€€€€€¥˜Á…Ñ ¥¸ìˆ½…Á¤½ØÄ½½¹¹•Ñ¥½¹Ì½ÍÍ ½Ý½É­™±½Ü½ÍÑ…ÉÐˆ°€ˆ½…Á¤½ØÄ½½¹¹•Ñ¥½¹Ì½ÍÍ ½Ý½É­™±½Ü½É•ÑÉä‰ôè4(€€€€€€€€€€€€€€€ÁÉ½™¥±”€ô=99Q%=9L¹•Ð¡‰½‘ä¹•Ð ‰½¹¹•Ñ¥½¹}¥ˆ¤¤4(€€€€€€€€€€€€€€€¥˜¹½ÐÁÉ½™¥±”èÉ…¥Í”¥±•9½Ñ½Õ¹‘ÉÉ½È ‰½¹¹•Ñ¥½¸•áÁ¥É•ˆ¤4(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÈÀÈ°ÍÍ¡}ÍÑ…ÉÑ}Ý½É­™±½Ü¡ÁÉ½™¥±”°‰½‘ä¤¤4(€€€€€€€€€€€¥˜Á…Ñ €ôô€ˆ½…Á¤½ØÄ½½¹¹•Ñ¥½¹Ì½ÍÍ ½Ý½É­™±½Ü½ÍÑ½Àˆè4(€€€€€€€€€€€€€€€ÁÉ½™¥±”€ô=99Q%=9L¹•Ð¡‰½‘ä¹•Ð ‰½¹¹•Ñ¥½¹}¥ˆ¤¤4(€€€€€€€€€€€€€€€¥˜¹½ÐÁÉ½™¥±”èÉ…¥Í”¥±•9½Ñ½Õ¹‘ÉÉ½È ‰½¹¹•Ñ¥½¸•áÁ¥É•ˆ¤4(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÈÀÀ°ÍÍ¡}ÍÑ½Á}Ý½É­™±½Ü¡ÁÉ½™¥±”¤¤4(€€€€€€€€€€€µ…Ñ €ôÉ”¹™Õ±±µ…Ñ ¡Èˆ½…Á¤½ØÄ½ÁÉ½©•Ðµ‘É…™ÑÌ¼¡‘É™}mµi„µèÀ´åt¬¤½…ÁÁÉ½Ù”ˆ°Á…Ñ ¤(€€€€€€€€€€€¥˜µ…Ñ è(€€€€€€€€€€€€€€€™½É”€ô‰½½°¡‰½‘ä¹•Ð ‰™½É”ˆ¤¤4(€€€€€€€€€€€€€€€¥˜™½É”…¹½Ì¹•¹Ù¥É½¸¹•Ð ‰eAA}11=]}=I}AAI=Yˆ¤€„ô€ˆÄˆè4(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÐÀÌ°•ÉÉ½Èõì4(€€€€€€€€€€€€€€€€€€€€€€€€‰½‘”ˆè€‰™½É•}…ÁÁÉ½Ù…±}‘¥Í…‰±•ˆ°4(€€€€€€€€€€€€€€€€€€€€€€€€‰µ•ÍÍ…”ˆè€‰™½É”…ÁÁÉ½Ù…°É•ÅÕ¥É•ÌeAA}11=]}=I}AAI=YôÄˆ°4(€€€€€€€€€€€€€€€€€€€ô¤4(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÈÀÀ°…ÁÁÉ½Ù•}‘É…™Ð¡}‘É…™Ñ}Á…Ñ ¡µ…Ñ ¹É½ÕÀ Ä¤¤°™½É”õ™½É”°©ÕÍÑ¥™¥…Ñ¥½¸õ‰½‘ä¹•Ð ‰©ÕÍÑ¥™¥…Ñ¥½¸ˆ¤¤¤(€€€€€€€€€€€µ…Ñ €ôÉ”¹™Õ±±µ…Ñ ¡Èˆ½…Á¤½ØÄ½ÁÉ½©•Ðµ‘É…™ÑÌ¼¡‘É™}mµi„µèÀ´åt¬¤½É•Í½±Ù•µ…¹‘¥‘…Ñ”ˆ°Á…Ñ ¤(€€€€€€€€€€€¥˜µ…Ñ è(€€€€€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÈÀÀ°Í•±•Ñ}É•Í½±Ù•‘}…¹‘¥‘…Ñ” (€€€€€€€€€€€€€€€€€€€}‘É…™Ñ}Á…Ñ ¡µ…Ñ ¹É½ÕÀ Ä¤¤°‰½‘ä¹•Ð ‰…¹‘¥‘…Ñ•}É•˜ˆ°€ˆˆ¤(€€€€€€€€€€€€€€€€¤¤(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÐÀÐ°•ÉÉ½Èõì‰½‘”ˆè€‰¹½Ñ}™½Õ¹ˆ°€‰µ•ÍÍ…”ˆè€‰I½ÕÑ”¹½Ð™½Õ¹‰ô¤4(€€€€€€€•á•ÁÐI•Ù¥•ÝI•ÅÕ¥É•‘ÉÉ½È…Ì•áŒè4(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÐÀä°•ÉÉ½Èõì‰½‘”ˆè€‰É•Ù¥•Ý}É•ÅÕ¥É•ˆ°€‰µ•ÍÍ…”ˆèÍÑÈ¡•áŒ¥ô¤4(€€€€€€€•á•ÁÐ€¡	½½ÑÍÑÉ…ÁÉÉ½È°Y…±Õ•ÉÉ½È°-•åÉÉ½È°©Í½¸¹)M=9•½‘•ÉÉ½È¤…Ì•áŒè4(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÐÀÀ°•ÉÉ½Èõì‰½‘”ˆè€‰Ù…±¥‘…Ñ¥½¹}•ÉÉ½Èˆ°€‰µ•ÍÍ…”ˆèÍÑÈ¡•áŒ¥ô¤4(€€€€€€€•á•ÁÐ¥±•9½Ñ½Õ¹‘ÉÉ½Èè4(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÐÀÐ°•ÉÉ½Èõì‰½‘”ˆè€‰¹½Ñ}™½Õ¹ˆ°€‰µ•ÍÍ…”ˆè€‰I•Í½ÕÉ”¹½Ð™½Õ¹‰ô¤4(€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè4(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÔÀÈ°•ÉÉ½Èõì‰½‘”ˆè€‰½¹¹•Ñ¥½¹}™…¥±•ˆ°€‰µ•ÍÍ…”ˆèÍÑÈ¡•áŒ¥ô¤4(4(€€€‘•˜‘½}AQ ¡Í•±˜¤è4(€€€€€€€µ…Ñ €ôÉ”¹™Õ±±µ…Ñ ¡Èˆ½…Á¤½ØÄ½ÁÉ½©•Ðµ‘É…™ÑÌ¼¡‘É™}mµi„µèÀ´åt¬¤½Ñ…É•ÑÌ¼¡mx½t¬¤ˆ°ÕÉ±Á…ÉÍ”¡Í•±˜¹Á…Ñ ¤¹Á…Ñ ¤4(€€€€€€€¥˜¹½Ðµ…Ñ è4(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÐÀÐ°•ÉÉ½Èõì‰½‘”ˆè€‰¹½Ñ}™½Õ¹ˆ°€‰µ•ÍÍ…”ˆè€‰I½ÕÑ”¹½Ð™½Õ¹‰ô¤4(€€€€€€€ÑÉäè4(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÈÀÀ°•‘¥Ñ}Ñ…É•Ñ}‘É…™Ð¡}‘É…™Ñ}Á…Ñ ¡µ…Ñ ¹É½ÕÀ Ä¤¤°µ…Ñ ¹É½ÕÀ È¤°Í•±˜¹}‰½‘ä ¤¤¤4(€€€€€€€•á•ÁÐ€¡	½½ÑÍÑÉ…ÁÉÉ½È°Y…±Õ•ÉÉ½È°-•åÉÉ½È¤…Ì•áŒè4(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}©Í½¸ ÐÀÀ°•ÉÉ½Èõì‰½‘”ˆè€‰Ù…±¥‘…Ñ¥½¹}•ÉÉ½Èˆ°€‰µ•ÍÍ…”ˆèÍÑÈ¡•áŒ¥ô¤4(4(4)‘•˜µ…¥¸ ¤€´ø9½¹”è4(€€€Á…ÉÍ•È€ô…ÉÁ…ÉÍ”¹ÉÕµ•¹ÑA…ÉÍ•È ¤4(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µ¡½ÍÐˆ°‘•™…Õ±ÐôˆÄÈÜ¸À¸À¸Äˆ¤4(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µÁ½ÉÐˆ°ÑåÁ”õ¥¹Ð°‘•™…Õ±ÐôàÜØÔ¤4(€€€…ÉÌ€ôÁ…ÉÍ•È¹Á…ÉÍ•}…ÉÌ ¤4(€€€¥˜¹½Ð}‰¥¹‘}¡½ÍÑ}¥Í}±½½Á‰…¬¡…ÉÌ¹¡½ÍÐ¤…¹½Ì¹•¹Ù¥É½¸¹•Ð ‰eAA}11=]}%9MUI}I5=Qˆ¤€„ô€ˆÄˆè4(€€€€€€€Á…ÉÍ•È¹•ÉÉ½È 4(€€€€€€€€€€€€‰‰¥¹‘¥¹œ½ÕÑÍ¥‘”±½½Á‰…¬É•ÅÕ¥É•Ì•áÁ±¥¥ÐeAA}11=]}%9MUI}I5=QôÄì€ˆ4(€€€€€€€€€€€€‰Ñ¡”…‘…ÁÑ•È¡…Ì¹¼…ÕÑ¡•¹Ñ¥…Ñ¥½¸±…å•Èˆ4(€€€€€€€€¤4(€€€Q¡É•…‘¥¹!QQAM•ÉÙ•È ¡…ÉÌ¹¡½ÍÐ°…ÉÌ¹Á½ÉÐ¤°!…¹‘±•È¤¹Í•ÉÙ•}™½É•Ù•È ¤4(4(4)¥˜}}¹…µ•}|€ôô€‰}}µ…¥¹}|ˆè4(€€€µ…¥¸ ¤4(