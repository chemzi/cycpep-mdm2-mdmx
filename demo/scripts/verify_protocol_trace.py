"""LOCAL-ONLY protocol-trace demo: artifact -> task -> protocol -> sha256.

Verifies the reproducibility chain that the Frontend V2 workbench exposes:

    artifact (id/sha256/size) --produced by--> task (action) --binds--> protocol
    (name/version/integrity_identity)

and cross-checks every artifact's recorded sha256 against the actual bytes on
disk. Reads only the LOCAL store (data/store.db) written by
``seed_demo_fixture.py``; never touches the server, git, or any shared
resource. Exits non-zero when the chain is broken.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["CYCPEP_DB_PATH"] = str(ROOT / "data" / "store.db")

import sqlite3  # noqa: E402

from prediction_pipeline.contracts import file_sha256, object_sha256  # noqa: E402
from web_api.workbench import WorkbenchReader  # noqa: E402


def _load_store() -> sqlite3.Connection:
    return sqlite3.connect(os.environ["CYCPEP_DB_PATH"])


def main() -> int:
    con = _load_store()
    from data_layer import get_storage_backend
    view = WorkbenchReader(get_storage_backend()).read()

    failures: list[str] = []

    # 1. Protocols exposed by the read model.
    protocols = (view.get("protocols") or {}).get("items") or []
    if not protocols:
        failures.append("workbench view exposes no protocol collection")
    print("== Protocols exposed by /api/v2/workbench ==")
    for protocol in protocols:
        print("  ", json.dumps(protocol, ensure_ascii=False))

    tasks = {
        task["task_id"]: task
        for task in (view.get("tasks") or {}).get("items") or []
    }
    artifacts = (view.get("artifacts") or {}).get("items") or []
    print(f"== Artifacts ({len(artifacts)}) -> producer task -> protocol ==")

    rows = con.execute(
        "SELECT artifact_id, path FROM artifacts ORDER BY artifact_id"
    ).fetchall()
    path_by_id = {artifact_id: Path(path) for artifact_id, path in rows}

    for artifact in sorted(artifacts, key=lambda item: item.get("artifact_id") or ""):
        artifact_id = artifact.get("artifact_id")
        recorded_sha = artifact.get("sha256")
        task_id = artifact.get("producer_task_id")
        task = tasks.get(task_id) or {}
        protocol = task.get("protocol") or artifact.get("protocol") or {}
        on_disk = path_by_id.get(artifact_id)

        line = f"  {artifact_id} type={artifact.get('artifact_type')}"
        if task_id:
            line += f" producer={task_id} ({task.get('action') or '?'})"
        if protocol:
            line += (
                f" protocol={protocol.get('name')}@{protocol.get('version')}"
                f" integrity={protocol.get('integrity_identity')}"
            )
        print(line)

        # 2. sha256 on disk must equal the recorded sha256.
        if on_disk is None or not on_disk.is_file():
            failures.append(f"{artifact_id}: artifact file missing on disk ({on_disk})")
            continue
        actual = file_sha256(on_disk)
        if recorded_sha and actual != recorded_sha:
            failures.append(
                f"{artifact_id}: sha256 mismatch recorded={recorded_sha} on_disk={actual}"
            )
        else:
            print(f"      sha256 ok={actual} ({on_disk.name}, {on_disk.stat().st_size} bytes)")

        # 4. The artifact file must be self-describing: embed its protocol and
        #    a hash of that protocol so the chain survives outside the DB.
        try:
            artifact_payload = json.loads(on_disk.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"{artifact_id}: artifact file unreadable ({exc})")
            continue
        embedded = artifact_payload.get("protocol") or {}
        if protocol and embedded != dict(protocol):
            failures.append(f"{artifact_id}: embedded protocol {embedded} != task protocol {protocol}")
        embedded_sha = artifact_payload.get("protocol_sha256")
        if protocol and embedded_sha != object_sha256(dict(protocol)):
            failures.append(f"{artifact_id}: embedded protocol_sha256 {embedded_sha} != expected")

        # 3. Every artifact should trace back to a known task with a protocol.
        if not task_id:
            failures.append(f"{artifact_id}: no producer_task_id in read model")
        elif task_id not in tasks:
            failures.append(f"{artifact_id}: producer task {task_id} missing from plan")
        elif not protocol:
            failures.append(f"{artifact_id}: producer task {task_id} exposes no protocol")

    con.close()
    if failures:
        print("\nFAIL:")
        for failure in failures:
            print("  -", failure)
        return 1
    print("\nProtocol trace OK: every artifact resolves task -> protocol and its sha256 matches disk bytes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
