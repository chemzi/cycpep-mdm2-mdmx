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
            "integrity_warnings": ["请先选择一个项目运行目录"],
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
        raise RuntimeError("SSH 连接需要安装 requirements.txt 中的 paramiko") from exc
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
 except (OSError,ValueError): workflow={{"status":"invalid_status","error":{{"message":"autopilot_status.json 无法读取"}}}}
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
 for candidate in sorted((work_root/"execution").rglob("*.log"),key=lambda item:item.stat().st_mtime,reverse=True)[:6]:
  process_logs.append("### "+candidate.name+" · "+str(candidate.parent.relative_to(work_root)))
  process_logs.extend(candidate.read_text(encoding="utf-8",errors="replace").splitlines()[-30:])
except OSError: pass
if process_logs: workflow["process_log_tail"]="\\n".join(process_logs)
print(json.dumps({{"state":state,"rows":rows,"events":events,"projects":projects,"workflow":workflow}},ensure_ascii=False))'''
    payload = _ssh_json(profile, remote_code)
    state, rows = payload["state"], payload["rows"]
    warnings = [key for key, present in (
        ("state_missing_project_id", state.get("project_id")),
        ("state_missing_project_config", state.get("project_config")),
        ("state_missing_approved_digest", state.get("approved_digest")),
    ) if not present]
    return {
        "source": {"mode": "ssh", "connected": True, "host": profile["host"]},
        "project": {"project_id": state.get("project_id"), "name": state.get("project"),
                    "config": state.get("project_config"), "targets": list((state.get("targets") or {}).keys())},
        "state": {"phase": state.get("phase"), "round": state.get("round"),
                  "candidate_count": state.get("candidate_count"),
                  "iteration_history": state.get("iteration_history") or [],
                  "thresholds_ready": bool(state.get("thresholds")),
                  "workflow": payload.get("workflow") or {}},
        "stats": {"total_candidates": len(rows),
                  "all_layers_pass": sum(_truthy(r.get("all_layers_pass")) for r in rows),
                  "finalized": sum(r.get("final_status") == "finalized" for r in rows)},
        # Remote paths must never be interpreted by this process.  SSH remains
        # read-only until a real remote artifact transport is implemented.
        "candidates": [_candidate_payload(r, allow_artifacts=False) for r in rows],
        "recent_evidence": payload["events"], "integrity_warnings": warnings,
        "projects": payload.get("projects") or [],
    }


def ssh_switch_project(profile: dict, slug: str) -> dict:
    profile = _validate_ssh_profile(profile)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", str(slug)):
        raise ValueError("invalid project slug")
    context_b64 = _remote_context(profile)
    slug_b64 = base64.b64encode(str(slug).encode()).decode()
    remote_code = f'''import base64,json,os,sys
from pathlib import Path
ctx=json.loads(base64.b64decode("{context_b64}").decode()); root=Path(os.path.expanduser(ctx["workspace_root"])).resolve(); slug=base64.b64decode("{slug_b64}").decode()
config_path=(root/"projects"/(slug+".json")).resolve()
if config_path.parent != (root/"projects").resolve() or not config_path.is_file(): raise FileNotFoundError("project not found")
config=json.loads(config_path.read_text(encoding="utf-8")); approved=((config.get("review") or {{}}).get("approved_digest"))
if not config.get("project_id") or not approved: raise ValueError("project is not approved")
data_dir=root/"data"/"projects"/slug; evidence_dir=root/"evidence"/"projects"/slug; work_root=data_dir/"autopilot"
data_dir.mkdir(parents=True,exist_ok=True); evidence_dir.mkdir(parents=True,exist_ok=True)
os.environ["CYCPEP_PROJECT_CONFIG"]=str(config_path); os.environ["CYCPEP_DATA_DIR"]=str(data_dir); os.environ["CYCPEP_EVIDENCE_DIR"]=str(evidence_dir)
sys.path.insert(0,str(root)); from data_layer import State; State.sync_project_config(config)
print(json.dumps({{"project_config_path":str(config_path),"data_dir":str(data_dir),"evidence_dir":str(evidence_dir),"work_root":str(work_root),"project_id":config["project_id"]}},ensure_ascii=False))'''
    return _ssh_json(profile, remote_code, command_timeout=60)


def ssh_create_draft(profile: dict, request: dict) -> dict:
    profile = _validate_ssh_profile(profile)
    identifier = str(request.get("identifier", "")).strip()
    identifier_type = str(request.get("identifier_type", "auto")).strip() or "auto"
    if not identifier or identifier_type not in {"auto", "gene", "uniprot", "pdb"}:
        raise ValueError("invalid target identifier")
    payload_b64 = base64.b64encode(json.dumps({
        "identifier": identifier, "identifier_type": identifier_type,
        "organism_id": int(request.get("organism_id", 9606)),
        "epitope": request.get("epitope"), "objective": request.get("objective", "binder"),
    }).encode()).decode()
    root_b64 = base64.b64encode(profile["workspace_root"].encode()).decode()
    remote_code = f'''import base64,json,os,sys,uuid
from pathlib import Path
root=Path(os.path.expanduser(base64.b64decode("{root_b64}").decode())).resolve(); os.chdir(root); sys.path.insert(0,str(root)); request=json.loads(base64.b64decode("{payload_b64}").decode())
from target_bootstrap import TargetBootstrapper
draft_id="drf_"+uuid.uuid4().hex[:12]; draft=TargetBootstrapper().create_draft(**request); draft["draft_id"]=draft_id
path=root/"data"/"web_api"/"drafts"/(draft_id+".json"); path.parent.mkdir(parents=True,exist_ok=True); temp=path.with_suffix("."+uuid.uuid4().hex+".tmp"); temp.write_text(json.dumps(draft,ensure_ascii=False,indent=2),encoding="utf-8"); os.replace(temp,path)
print(json.dumps(draft,ensure_ascii=False))'''
    return _ssh_json(profile, remote_code, command_timeout=180)


def _run_ssh_draft_job(job_id: str, profile: dict, request: dict) -> None:
    try:
        update = {"status": "complete", "result": ssh_create_draft(profile, request)}
    except Exception as exc:
        update = {"status": "failed", "error": str(exc)[-1200:]}
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(update)
            JOBS[job_id]["finished_at"] = time.time()


def start_ssh_draft_job(profile: dict, request: dict) -> dict:
    if not str(request.get("identifier", "")).strip():
        raise ValueError("target identifier is required")
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    job = {"job_id": job_id, "status": "running", "message": "正在解析靶点并检索结构", "created_at": time.time()}
    with JOBS_LOCK:
        JOBS[job_id] = job
    threading.Thread(target=_run_ssh_draft_job, args=(job_id, dict(profile), dict(request)), daemon=True).start()
    return dict(job)


def get_job(job_id: str) -> dict:
    with JOBS_LOCK:
        if job_id not in JOBS:
            raise FileNotFoundError("job not found")
        return dict(JOBS[job_id])


def ssh_approve_remote_draft(profile: dict, draft_id: str) -> dict:
    profile = _validate_ssh_profile(profile)
    if not re.fullmatch(r"drf_[A-Za-z0-9]+", str(draft_id)):
        raise ValueError("invalid draft id")
    root_b64 = base64.b64encode(profile["workspace_root"].encode()).decode()
    draft_b64 = base64.b64encode(str(draft_id).encode()).decode()
    remote_code = f'''import base64,json,os,sys
from pathlib import Path
root=Path(os.path.expanduser(base64.b64decode("{root_b64}").decode())).resolve(); os.chdir(root); sys.path.insert(0,str(root)); draft_id=base64.b64decode("{draft_b64}").decode(); draft_path=root/"data"/"web_api"/"drafts"/(draft_id+".json")
if not draft_path.is_file(): raise FileNotFoundError("remote draft not found")
from project_config import target_slug
from target_bootstrap import approve_draft
draft=json.loads(draft_path.read_text(encoding="utf-8")); slug=target_slug(draft["project_id"]); config_path=root/"projects"/(slug+".json"); config=approve_draft(draft_path,output_path=config_path)
data_dir=root/"data"/"projects"/slug; evidence_dir=root/"evidence"/"projects"/slug; work_root=data_dir/"autopilot"; data_dir.mkdir(parents=True,exist_ok=True); evidence_dir.mkdir(parents=True,exist_ok=True)
os.environ["CYCPEP_PROJECT_CONFIG"]=str(config_path); os.environ["CYCPEP_DATA_DIR"]=str(data_dir); os.environ["CYCPEP_EVIDENCE_DIR"]=str(evidence_dir)
from data_layer import State
State.sync_project_config(config)
print(json.dumps({{"project_config_path":str(config_path),"data_dir":str(data_dir),"evidence_dir":str(evidence_dir),"work_root":str(work_root),"project_id":config["project_id"],"slug":slug}},ensure_ascii=False))'''
    return _ssh_json(profile, remote_code, command_timeout=60)


def ssh_start_workflow(profile: dict, request: dict) -> dict:
    profile = _validate_ssh_profile(profile)
    required = ("project_config_path", "data_dir", "evidence_dir", "work_root")
    if any(not profile.get(key) for key in required):
        raise ValueError("请先批准或切换到一个项目")
    limits = {
        "max_design_proposals": int(request.get("max_design_proposals", 4)),
        "max_prediction_candidates": int(request.get("max_prediction_candidates", 4)),
        "max_gpu_minutes": float(request.get("max_gpu_minutes", 360)),
        "max_rounds": int(request.get("max_rounds", 2)),
    }
    if not 1 <= limits["max_design_proposals"] <= 100:
        raise ValueError("Design 数量必须在 1 到 100 之间")
    if not 1 <= limits["max_prediction_candidates"] <= limits["max_design_proposals"]:
        raise ValueError("Prediction 数量必须在 1 到 Design 数量之间")
    if not 1 <= limits["max_rounds"] <= 10 or not 1 <= limits["max_gpu_minutes"] <= 10080:
        raise ValueError("轮数或 GPU 时间预算超出允许范围")
    payload = {**{key: profile.get(key) for key in (
        "workspace_root", "project_config_path", "data_dir", "evidence_dir", "work_root",
        "core_python", "design_python", "prediction_python",
    )}, **limits, "approver": str(request.get("approver") or "web-user")[:128],
        "justification": str(request.get("justification") or "User approved full automatic workflow in CycPep Studio")[:500]}
    payload_b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    remote_code = f'''import base64,json,os,subprocess,sys,time
from pathlib import Path
p=json.loads(base64.b64decode("{payload_b64}").decode()); root=Path(os.path.expanduser(p["workspace_root"])).resolve(); work=Path(p["work_root"]).resolve(); work.mkdir(parents=True,exist_ok=True); process_file=work/"autopilot_process.json"
if process_file.is_file():
 old=json.loads(process_file.read_text(encoding="utf-8")); old_pid=int(old.get("pid") or 0)
 if old_pid>0 and Path("/proc").joinpath(str(old_pid)).exists(): raise RuntimeError("该项目已有工作流正在运行")
defaults={{"core_python":"/root/damodel-tmp/envs/novapeptide-core/bin/python","design_python":"/root/damodel-tmp/envs/rfdiffusion-design/bin/python","prediction_python":"/root/damodel-tmp/envs/cycpep-prediction/bin/python"}}
for key,value in defaults.items():
 if not p.get(key) and Path(value).is_file(): p[key]=value
python=p.get("core_python") if p.get("core_python") and Path(p["core_python"]).is_file() else sys.executable
argv=[python,"-m","execution.autopilot","--project-config",p["project_config_path"],"--data-dir",p["data_dir"],"--evidence-dir",p["evidence_dir"],"--work-root",p["work_root"],"--approver",p["approver"],"--justification",p["justification"],"--max-design-proposals",str(p["max_design_proposals"]),"--max-prediction-candidates",str(p["max_prediction_candidates"]),"--max-gpu-minutes",str(p["max_gpu_minutes"]),"--max-rounds",str(p["max_rounds"])]
for flag,key in (("--core-python","core_python"),("--design-python","design_python"),("--prediction-python","prediction_python")):
 if p.get(key): argv.extend([flag,p[key]])
child_env=os.environ.copy(); env_file=Path.home()/".config"/"cycpep"/"runtime.env"; allowed={{"STEP_API_KEY","STEP_BASE_URL","OPENAI_API_KEY","OPENAI_BASE_URL","CYCPEP_LLM_API_KEY","CYCPEP_LLM_BASE_URL"}}
if env_file.is_file():
 for line in env_file.read_text(encoding="utf-8").splitlines():
  if "=" not in line or line.lstrip().startswith("#"): continue
  key,value=line.split("=",1)
  if key.strip() in allowed: child_env[key.strip()]=value.strip()
log=open(work/"autopilot.log","a",encoding="utf-8"); proc=subprocess.Popen(argv,cwd=root,env=child_env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,close_fds=True); process_file.write_text(json.dumps({{"pid":proc.pid,"started_at":time.time(),"alive":True}},indent=2),encoding="utf-8"); print(json.dumps({{"pid":proc.pid,"status":"running"}}))'''
    return _ssh_json(profile, remote_code, command_timeout=30)


def ssh_stop_workflow(profile: dict) -> dict:
    profile = _validate_ssh_profile(profile)
    if not profile.get("work_root"):
        raise ValueError("no active project")
    work_b64 = base64.b64encode(profile["work_root"].encode()).decode()
    remote_code = f'''import base64,json,os,signal,time
from pathlib import Path
work=Path(base64.b64decode("{work_b64}").decode()).resolve(); process_file=work/"autopilot_process.json"
if not process_file.is_file(): print(json.dumps({{"status":"not_running"}}))
else:
 p=json.loads(process_file.read_text(encoding="utf-8")); pid=int(p.get("pid") or 0); alive=pid>0 and Path("/proc").joinpath(str(pid)).exists()
 if alive: os.killpg(pid,signal.SIGTERM)
 p.update({{"alive":False,"stopped_at":time.time()}}); process_file.write_text(json.dumps(p,indent=2),encoding="utf-8")
 status_file=work/"autopilot_status.json"
 if status_file.is_file():
  try:
   status=json.loads(status_file.read_text(encoding="utf-8")); status.update({{"status":"stopped","updated_at":__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()}}); status_file.write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding="utf-8")
  except (OSError,ValueError,TypeError): pass
 print(json.dumps({{"status":"stopped" if alive else "not_running","pid":pid}}))'''
    return _ssh_json(profile, remote_code)


class Handler(BaseHTTPRequestHandler):
    server_version = "CycPepWebAdapter/0.1"

    def _json(self, status: int, data=None, error=None):
        body = {"request_id": f"req_{uuid.uuid4().hex[:12]}"}
        body["data" if error is None else "error"] = data if error is None else error
        encoded = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", os.environ.get("CYCPEP_UI_ORIGIN", "http://localhost:3000"))
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers(); self.wfile.write(encoded)

    def _body(self):
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(size) or b"{}")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", os.environ.get("CYCPEP_UI_ORIGIN", "http://localhost:3000"))
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,OPTIONS")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/v1/health":
                return self._json(200, {"status": "ok", "adapter": "local"})
            if path == "/api/v1/projects":
                state = State.load(); projects = [{"kind": "runtime", "project_id": state.get("project_id"), "name": state.get("project")}]
                if DRAFTS.exists():
                    projects += [{"kind": "draft", "draft_id": p.stem, "project_id": d.get("project_id"), "name": d.get("name")}
                                 for p in DRAFTS.glob("drf_*.json") for d in [_read_json(p)]]
                return self._json(200, projects)
            if path == "/api/v1/snapshot":
                return self._json(200, local_snapshot())
            artifact_match = re.fullmatch(r"/api/v1/artifacts/(art_[a-f0-9]{24})/coordinates", path)
            if artifact_match:
                artifact = ARTIFACTS.get(artifact_match.group(1))
                if not artifact:
                    return self._json(404, error={"code": "artifact_not_found", "message": "Verified artifact is not registered"})
                payload = artifact["path"].read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "chemical/x-pdb" if artifact["format"] == "pdb" else "chemical/x-mmcif")
                self.send_header("X-Artifact-SHA256", artifact["sha256"])
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("Access-Control-Allow-Origin", os.environ.get("CYCPEP_UI_ORIGIN", "http://localhost:3000"))
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            match = re.fullmatch(r"/api/v1/project-drafts/(drf_[A-Za-z0-9]+)", path)
            if match:
                return self._json(200, _read_json(_draft_path(match.group(1))))
            return self._json(404, error={"code": "not_found", "message": "Route not found"})
        except FileNotFoundError:
            return self._json(404, error={"code": "not_found", "message": "Resource not found"})
        except Exception as exc:
            return self._json(500, error={"code": "adapter_error", "message": str(exc)})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/v1/project-drafts":
                draft_id = f"drf_{uuid.uuid4().hex[:12]}"
                draft = TargetBootstrapper().create_draft(
                    identifier=body["identifier"], identifier_type=body.get("identifier_type", "auto"),
                    organism_id=int(body.get("organism_id", 9606)), epitope=body.get("epitope"),
                    objective=body.get("objective", "binder"),
                )
                draft["draft_id"] = draft_id
                _write_json(_draft_path(draft_id), draft)
                return self._json(201, draft)
            if path == "/api/v1/connections/ssh":
                snapshot = ssh_snapshot(body)
                connection_id = f"conn_{uuid.uuid4().hex[:12]}"
                CONNECTIONS[connection_id] = {k: body[k] for k in (
                    "host", "username", "port", "key_alias", "password", "workspace_root",
                    "core_python", "design_python", "prediction_python",
                ) if k in body}
                return self._json(200, {"connection_id": connection_id, "snapshot": snapshot})
            if path == "/api/v1/connections/ssh/snapshot":
                profile = CONNECTIONS.get(body.get("connection_id"))
                if not profile: raise FileNotFoundError("connection expired")
                return self._json(200, ssh_snapshot(profile))
            if path == "/api/v1/connections/ssh/projects/switch":
                profile = CONNECTIONS.get(body.get("connection_id"))
                if not profile: raise FileNotFoundError("connection expired")
                result = ssh_switch_project(profile, body.get("slug", ""))
                profile.update({key: result[key] for key in (
                    "project_config_path", "data_dir", "evidence_dir", "work_root"
                )})
                return self._json(200, {**result, "snapshot": ssh_snapshot(profile)})
            if path == "/api/v1/connections/ssh/project-drafts":
                profile = CONNECTIONS.get(body.get("connection_id"))
                if not profile: raise FileNotFoundError("connection expired")
                return self._json(202, start_ssh_draft_job(profile, body))
            if path == "/api/v1/connections/ssh/project-drafts/status":
                return self._json(200, get_job(body.get("job_id", "")))
            if path == "/api/v1/connections/ssh/project-drafts/approve":
                profile = CONNECTIONS.get(body.get("connection_id"))
                if not profile: raise FileNotFoundError("connection expired")
                result = ssh_approve_remote_draft(profile, body.get("draft_id", ""))
                profile.update({key: result[key] for key in (
                    "project_config_path", "data_dir", "evidence_dir", "work_root"
                )})
                return self._json(200, {**result, "snapshot": ssh_snapshot(profile)})
            if path in {"/api/v1/connections/ssh/workflow/start", "/api/v1/connections/ssh/workflow/retry"}:
                profile = CONNECTIONS.get(body.get("connection_id"))
                if not profile: raise FileNotFoundError("connection expired")
                return self._json(202, ssh_start_workflow(profile, body))
            if path == "/api/v1/connections/ssh/workflow/stop":
                profile = CONNECTIONS.get(body.get("connection_id"))
                if not profile: raise FileNotFoundError("connection expired")
                return self._json(200, ssh_stop_workflow(profile))
            match = re.fullmatch(r"/api/v1/project-drafts/(drf_[A-Za-z0-9]+)/approve", path)
            if match:
                force = bool(body.get("force"))
                if force and os.environ.get("CYCPEP_ALLOW_FORCE_APPROVE") != "1":
                    return self._json(403, error={
                        "code": "force_approval_disabled",
                        "message": "force approval requires CYCPEP_ALLOW_FORCE_APPROVE=1",
                    })
                return self._json(200, approve_draft(_draft_path(match.group(1)), force=force, justification=body.get("justification")))
            return self._json(404, error={"code": "not_found", "message": "Route not found"})
        except ReviewRequiredError as exc:
            return self._json(409, error={"code": "review_required", "message": str(exc)})
        except (BootstrapError, ValueError, KeyError, json.JSONDecodeError) as exc:
            return self._json(400, error={"code": "validation_error", "message": str(exc)})
        except FileNotFoundError:
            return self._json(404, error={"code": "not_found", "message": "Resource not found"})
        except Exception as exc:
            return self._json(502, error={"code": "connection_failed", "message": str(exc)})

    def do_PATCH(self):
        match = re.fullmatch(r"/api/v1/project-drafts/(drf_[A-Za-z0-9]+)/targets/([^/]+)", urlparse(self.path).path)
        if not match:
            return self._json(404, error={"code": "not_found", "message": "Route not found"})
        try:
            return self._json(200, edit_target_draft(_draft_path(match.group(1)), match.group(2), self._body()))
        except (BootstrapError, ValueError, KeyError) as exc:
            return self._json(400, error={"code": "validation_error", "message": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not _bind_host_is_loopback(args.host) and os.environ.get("CYCPEP_ALLOW_INSECURE_REMOTE") != "1":
        parser.error(
            "binding outside loopback requires explicit CYCPEP_ALLOW_INSECURE_REMOTE=1; "
            "the adapter has no authentication layer"
        )
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
