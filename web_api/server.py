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
import logging
import os
import re
import subprocess
import sys
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data_layer import CandidateIndex, EvidenceLogger, State, get_storage_backend  # noqa: E402
from target_bootstrap import (  # noqa: E402
    BootstrapError,
    ReviewRequiredError,
    TargetBootstrapper,
    approve_draft,
    edit_target_draft,
)
from web_api.project_control import ProjectControlError, ProjectControlService  # noqa: E402
from web_api.scoped_workbench import read_launcher_workbench  # noqa: E402
from web_api.workbench import WorkbenchReader  # noqa: E402
from workflow.control_models import (  # noqa: E402
    ManualApprovalRequest,
    ProjectLaunchOptions,
    ProjectLaunchRequest,
    ScopedReadIdentity,
)
from workflow.errors import DiagnosticContractError, sanitize_message  # noqa: E402

STORE = Path(os.environ.get("CYCPEP_WEB_STORE", ROOT / "data" / "web_api"))
DRAFTS = STORE / "drafts"
COORDINATES = Path(os.environ.get("CYCPEP_TARGET_ROOT", ROOT / "data" / "targets"))
CONNECTIONS: dict[str, dict] = {}
ARTIFACTS: dict[str, dict] = {}
HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:]+\])$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_LOGGER = logging.getLogger(__name__)
_CONTROL_STATUS = {
    "control_binding_invalid": 409,
    "control_binding_conflict": 409,
    "approval_plan_stale": 409,
    "approval_estimate_unavailable": 409,
    "approval_ceiling_exceeded": 409,
    "project_review_blocked": 409,
    "launcher_run_not_found": 404,
    "launcher_operation_failed": 502,
}


def _require_matching_id(body: dict, name: str, expected: str) -> None:
    supplied = body.get(name)
    if supplied is not None and supplied != expected:
        raise ValueError(f"URL and body {name} differ")


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
    root = str(profile.get("workspace_root", "")).strip()
    port = int(profile.get("port", 22))
    if not HOST_RE.fullmatch(host) or not USER_RE.fullmatch(user):
        raise ValueError("invalid SSH host or username")
    if not 1 <= port <= 65535 or not root.startswith(("/", "~")):
        raise ValueError("invalid port or remote workspace root")
    return {"host": host, "username": user, "port": port, "key_alias": alias,
            "workspace_root": root, "key_path": _ssh_key(alias)}


def ssh_snapshot(profile: dict) -> dict:
    profile = _validate_ssh_profile(profile)
    root_b64 = base64.b64encode(profile["workspace_root"].encode()).decode()
    remote_code = f'''import base64,json,os,sys
root=os.path.expanduser(base64.b64decode("{root_b64}").decode())
sys.path.insert(0,root)
from data_layer import State,CandidateIndex,EvidenceLogger
print(json.dumps({{"state":State.load(),"rows":CandidateIndex.load(),"events":EvidenceLogger.get_all()[-100:]}},ensure_ascii=False))'''
    encoded = base64.b64encode(remote_code.encode()).decode()
    launcher = "import base64;exec(base64.b64decode('" + encoded + "'))"
    command = [
        "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=8", "-p", str(profile["port"]), "-i", profile["key_path"],
        f'{profile["username"]}@{profile["host"]}',
        f'python3 -c "{launcher}"',
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or "SSH connection failed").strip()[-500:])
    payload = json.loads(result.stdout)
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
                  "thresholds_ready": bool(state.get("thresholds"))},
        "stats": {"total_candidates": len(rows),
                  "all_layers_pass": sum(_truthy(r.get("all_layers_pass")) for r in rows),
                  "finalized": sum(r.get("final_status") == "finalized" for r in rows)},
        # Remote paths must never be interpreted by this process.  SSH remains
        # read-only until a real remote artifact transport is implemented.
        "candidates": [_candidate_payload(r, allow_artifacts=False) for r in rows],
        "recent_evidence": payload["events"], "integrity_warnings": warnings,
    }


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

    def _object_body(self):
        value = self._body()
        if not isinstance(value, dict):
            raise ValueError("request body must be an object")
        return value

    def _project_control(self):
        service = getattr(self.server, "project_control_service", None)
        return service if service is not None else ProjectControlService(DRAFTS)

    def _control_json(self, value, *, status=200):
        failure = value.get("control_failure") if isinstance(value, dict) else None
        if not failure:
            return self._json(status, value)
        error_status = _CONTROL_STATUS.get(failure.get("code"), 409)
        return self._json(error_status, error={**failure, "control": value})

    def _control_error(self, status, code, message):
        return self._json(status, error={
            "code": code,
            "message": sanitize_message(
                str(message), fallback="Control operation could not be completed."
            ),
        })

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", os.environ.get("CYCPEP_UI_ORIGIN", "http://localhost:3000"))
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,OPTIONS")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/v2/control/"):
            return self._do_control_get(path)
        if path == "/api/v2/workbench" and "launcher_run_id" in parse_qs(
            parsed.query, keep_blank_values=True
        ):
            return self._do_scoped_workbench(parsed.query)
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
            if path == "/api/v2/workbench":
                store = getattr(self.server, "workbench_store", None)
                if store is None:
                    return self._json(503, error={
                        "code": "workbench_unavailable",
                        "message": "Workbench read model is unavailable",
                    })
                return self._json(200, WorkbenchReader(store).read())
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
        if path == "/api/v2/control/project-drafts" or path.startswith(
            "/api/v2/control/"
        ):
            return self._do_control_post(path)
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
                CONNECTIONS[connection_id] = {k: body[k] for k in ("host", "username", "port", "key_alias", "workspace_root") if k in body}
                return self._json(200, {"connection_id": connection_id, "snapshot": snapshot})
            if path == "/api/v1/connections/ssh/snapshot":
                profile = CONNECTIONS.get(body.get("connection_id"))
                if not profile: raise FileNotFoundError("connection expired")
                return self._json(200, ssh_snapshot(profile))
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

    def _do_scoped_workbench(self, query):
        try:
            values = parse_qs(query, keep_blank_values=True).get("launcher_run_id", [])
            if len(values) != 1:
                raise ValueError("launcher_run_id must appear exactly once")
            identity = ScopedReadIdentity.from_dict({"launcher_run_id": values[0]})
            reader = getattr(
                self.server, "launcher_workbench_reader", read_launcher_workbench
            )
            return self._json(200, reader(launcher_run_id=identity.launcher_run_id))
        except (AttributeError, TypeError, ValueError) as exc:
            return self._control_error(400, "validation_error", exc)
        except DiagnosticContractError as exc:
            status = 404 if exc.code == "launcher_diagnostic_not_found" else 409
            return self._control_error(status, exc.code, exc)
        except Exception:
            _LOGGER.exception("scoped workbench read failed")
            return self._control_error(
                502, "workbench_unavailable", "Scoped workbench read failed."
            )

    def _do_control_get(self, path):
        try:
            service = self._project_control()
            draft = re.fullmatch(
                r"/api/v2/control/project-drafts/([^/]+)", path
            )
            if draft:
                return self._json(200, service.retrieve_draft(draft.group(1)))
            run = re.fullmatch(
                r"/api/v2/control/launcher-runs/([^/]+)", path
            )
            if run:
                identity = ScopedReadIdentity.from_dict({
                    "launcher_run_id": run.group(1)
                })
                return self._control_json(service.status(identity.launcher_run_id))
            return self._json(
                404, error={"code": "not_found", "message": "Route not found"}
            )
        except FileNotFoundError:
            return self._control_error(404, "not_found", "Resource not found.")
        except (AttributeError, TypeError, ValueError) as exc:
            return self._control_error(400, "validation_error", exc)
        except ProjectControlError as exc:
            failure = exc.to_dict()
            return self._json(
                _CONTROL_STATUS.get(failure["code"], 409), error=failure
            )
        except Exception:
            _LOGGER.exception("control GET failed")
            return self._control_error(
                502, "launcher_operation_failed", "Control operation failed."
            )

    def _do_control_post(self, path):
        try:
            body = self._object_body()
            service = self._project_control()
            if path == "/api/v2/control/project-drafts":
                request = ProjectLaunchRequest.from_dict(body)
                return self._json(201, service.create_draft(request))
            approve = re.fullmatch(
                r"/api/v2/control/project-drafts/([^/]+)/approve", path
            )
            if approve:
                _require_matching_id(body, "draft_id", approve.group(1))
                return self._json(200, service.approve_project(
                    approve.group(1), justification=body.get("justification")
                ))
            launch = re.fullmatch(
                r"/api/v2/control/project-drafts/([^/]+)/launch", path
            )
            if launch:
                _require_matching_id(body, "draft_id", launch.group(1))
                options = ProjectLaunchOptions.from_dict(body.get("options", body))
                return self._control_json(
                    service.launch_project(launch.group(1), options)
                )
            approval = re.fullmatch(
                r"/api/v2/control/launcher-runs/([^/]+)/approval", path
            )
            if approval:
                request = ManualApprovalRequest.from_dict(body)
                if request.launcher_run_id != approval.group(1):
                    raise ValueError("URL and body launcher_run_id differ")
                return self._control_json(service.approve_and_continue(request))
            continuation = re.fullmatch(
                r"/api/v2/control/launcher-runs/([^/]+)/continue", path
            )
            if continuation:
                identity = ScopedReadIdentity.from_dict({
                    "launcher_run_id": continuation.group(1)
                })
                _require_matching_id(body, "launcher_run_id", identity.launcher_run_id)
                return self._control_json(service.continue_run(identity.launcher_run_id))
            return self._json(
                404, error={"code": "not_found", "message": "Route not found"}
            )
        except (
            AttributeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
        ) as exc:
            return self._control_error(400, "validation_error", exc)
        except FileNotFoundError:
            return self._control_error(404, "not_found", "Resource not found.")
        except ProjectControlError as exc:
            failure = exc.to_dict()
            return self._json(
                _CONTROL_STATUS.get(failure["code"], 409), error=failure
            )
        except Exception:
            _LOGGER.exception("control POST failed")
            return self._control_error(
                502, "launcher_operation_failed", "Control operation failed."
            )

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
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.workbench_store = get_storage_backend()
    server.project_control_service = ProjectControlService(DRAFTS)
    server.serve_forever()


if __name__ == "__main__":
    main()
