"""One-command demo stack verification for the Frontend V2 read model.

Starts ``web_api/server.py`` on a free port (or reuses a caller-supplied port
when it already serves a CycPep workbench), fetches ``/api/v2/workbench``,
verifies the response schema, prints a human-readable summary, and writes a
snapshot JSON under ``demo/snapshot/`` (gitignored runtime artifact).

Pure standard library; no npm/GPU required.  Exits non-zero, prints the
adapter log tail, and writes no snapshot when the read model is unhealthy.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
EXPECTED_WORKBENCH_SCHEMA = "frontend.workbench.v2"
EXPECTED_RESULTS_SCHEMA = "frontend.results.v1"
POLL_ATTEMPTS = 40
POLL_INTERVAL = 0.5


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _fetch(port: int, path: str = "/api/v2/workbench", timeout: float = 2.0):
    """Fetch one read-model payload, unwrapping the adapter envelope.

    The adapter wraps every response as ``{"request_id": ..., "data": ...}``
    (or ``"error"`` on failure); raise when the payload is an error so callers
    never snapshot a partial result.
    """
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        envelope = json.loads(resp.read().decode("utf-8"))
    if not isinstance(envelope, dict):
        raise RuntimeError(f"unexpected adapter response shape: {envelope!r}")
    if "data" in envelope:
        return envelope["data"]
    raise RuntimeError(envelope.get("error") or envelope)


def _is_workbench(data) -> bool:
    return isinstance(data, dict) and data.get("schema_version") == EXPECTED_WORKBENCH_SCHEMA


def _probe(port: int, attempts: int = 3) -> bool:
    """Best-effort identity check: is the port already serving our adapter?"""
    for _ in range(attempts):
        try:
            return _is_workbench(_fetch(port, timeout=1.0))
        except Exception:
            time.sleep(0.5)
    return False


def _print_server_tail(log_path: Path, max_lines: int = 25) -> None:
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    print("--- adapter log tail ---")
    for line in lines[-max_lines:]:
        print("  " + line)


def _summarize(data: dict) -> None:
    print(f"schema_version : {data.get('schema_version')}")
    project = data.get("project") or {}
    print(f"project        : {project.get('project_id')} ({project.get('name')})")
    print(f"targets        : {project.get('targets')}")
    for section in ("candidates", "evidence", "artifacts", "protocols",
                    "transactions", "tasks", "executions", "blockers"):
        block = data.get(section)
        if isinstance(block, dict):
            print(f"{section:<14}: total={block.get('total')}")
    trace = data.get("trace") or {}
    print(f"trace          : project={trace.get('project_id')} "
          f"workflow={trace.get('workflow_id')} run={trace.get('run_id')}")


def _summarize_results(data: dict) -> None:
    print(f"schema_version : {data.get('schema_version')}")
    project = data.get("project") or {}
    print(f"project        : {project.get('project_id')} ({project.get('name')})")
    summary = data.get("summary") or {}
    print(f"candidates     : total={summary.get('candidates_total')} "
          f"evaluated={summary.get('candidates_evaluated')} "
          f"pending={summary.get('candidates_pending_prediction')}")
    print(f"hard clearance : {summary.get('hard_cleared')} "
          f"rate={summary.get('hard_clearance_rate')}")
    print(f"data basis     : {summary.get('data_basis')}")
    print(f"conclusion     : {data.get('conclusion')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=None,
                        help="adapter port; default picks a free port")
    parser.add_argument("--keep-server", action="store_true",
                        help="leave the adapter running after verification")
    parser.add_argument("--snapshot-dir", default=str(ROOT / "demo" / "snapshot"))
    args = parser.parse_args()

    port = args.port
    proc = None
    server_log = Path(tempfile.gettempdir()) / f"cycpep-webapi-{port or 'auto'}.log"

    if port is not None and _port_open(port):
        # A caller-supplied port is already serving something: verify it is
        # our adapter before fetching, otherwise fail fast instead of polling.
        if not _probe(port):
            print(f"FAILED: port {port} is already in use by a service that does "
                  f"not look like the CycPep workbench adapter; stop it or "
                  f"choose another --port")
            return 2
    else:
        if port is None:
            port = _free_port()
        log_handle = open(server_log, "wb")
        proc = subprocess.Popen(
            [sys.executable, "web_api/server.py", "--host", "127.0.0.1",
             "--port", str(port)],
            cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=log_handle,
        )
    try:
        data = None
        last_error = None
        for _ in range(POLL_ATTEMPTS):
            try:
                data = _fetch(port)
                break
            except Exception as exc:  # adapter still warming up
                last_error = exc
                time.sleep(POLL_INTERVAL)
        if not isinstance(data, dict):
            print(f"FAILED: /api/v2/workbench did not respond ({last_error})")
            if proc is not None:
                _print_server_tail(server_log)
            return 1
        if data.get("schema_version") != EXPECTED_WORKBENCH_SCHEMA:
            print(f"FAILED: unexpected schema_version "
                  f"{data.get('schema_version')!r} (expected {EXPECTED_WORKBENCH_SCHEMA!r})")
            if proc is not None:
                _print_server_tail(server_log)
            return 1

        _summarize(data)

        try:
            results = _fetch(port, path="/api/v2/results")
        except Exception as exc:
            print(f"FAILED: /api/v2/results did not respond ({exc})")
            if proc is not None:
                _print_server_tail(server_log)
            return 1
        if not isinstance(results, dict) or results.get("schema_version") != EXPECTED_RESULTS_SCHEMA:
            print(f"FAILED: unexpected results schema_version "
                  f"{results.get('schema_version')!r} (expected {EXPECTED_RESULTS_SCHEMA!r})")
            if proc is not None:
                _print_server_tail(server_log)
            return 1
        _summarize_results(results)

        snapshot_dir = Path(args.snapshot_dir)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        snapshot_path = snapshot_dir / f"workbench-v2-{stamp}.json"
        snapshot_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"snapshot       : {snapshot_path}")
        return 0
    finally:
        if proc is not None and not args.keep_server:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
