"""LOCAL-ONLY read-model stress check for large candidate volumes.

Inserts ``--n`` synthetic candidates plus one battery-evaluated row each into
the LOCAL store (data/store.db), times the workbench and results read models,
reports the truncation semantics the frontend renders as "N / M returned -
truncated", then deletes the stress rows so the normal 4-candidate fixture is
restored. Never touches the server, git, or any shared resource.

Usage:
    python demo/scripts/stress_demo_data.py --n 1500
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["CYCPEP_DB_PATH"] = str(ROOT / "data" / "store.db")

PROJECT_ID = "mdm2_mdmx_reference"
TARGETS = ["MDM2", "MDMX"]

FULL_LAYER_KEYS = [
    "L1_plddt", "L2_ipsae_mdm2", "L2_ipsae_mdmx", "L3_dg_mdm2", "L3_dg_mdmx",
    "L3_sc_mdm2", "L3_sc_mdmx", "L3_dsasa_mdm2", "L3_dsasa_mdmx",
    "L4_nc_distance_pre", "L4_nc_distance_post", "L5_hotspot_cov_mdm2",
    "L5_hotspot_cov_mdmx", "L6_pose_rmsd_mdm2", "L6_pose_rmsd_mdmx", "L7_scrmsd",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_stress_rows(con: sqlite3.Connection, n: int) -> None:
    now = _now()
    now_iso = now
    con.execute("BEGIN")
    for index in range(n):
        candidate_id = f"CST-{index:06d}"
        passed = index % 5 == 0
        failed_layers = [] if passed else ["l3_pass", "l4_pass"]
        layer_values = {}
        for key in FULL_LAYER_KEYS:
            layer_values[key] = 0.9 if passed else (0.4 if key.startswith("L3") else 0.6)
        candidate_payload = {
            "candidate_id": candidate_id,
            "sequence": f"cyclo[Gly-{index % 20}-Ala-Phe-Glu-Pro-Arg-Lys-Thr]",
            "source_route": "route_A",
            "status": "evaluated" if passed else "predicted",
            "final_status": "evaluated" if passed else "predicted",
            "metrics": {
                "plddt": 0.85,
                "ipsae_mdm2": 0.92 if passed else 0.55,
                "ipsae_mdmx": 0.90 if passed else 0.52,
                "dg_mdm2": -12.0 if passed else -8.0,
                "dg_mdmx": -11.5 if passed else -7.5,
                "pose_rmsd_mdm2": 0.4 if passed else 1.5,
                "pose_rmsd_mdmx": 0.45 if passed else 1.6,
            },
            "demo_fixture": True,
            "stress": True,
            "project_id": PROJECT_ID,
        }
        con.execute(
            "INSERT INTO candidates(candidate_id, project_id, sequence, status, metrics_json, created_at, updated_at, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate_id, PROJECT_ID,
                candidate_payload["sequence"],
                candidate_payload["status"],
                json.dumps(candidate_payload["metrics"]),
                now_iso, now_iso, json.dumps(candidate_payload),
            ),
        )
        payload = {
            "demo_fixture": True,
            "stress": True,
            "project_id": PROJECT_ID,
            "passed": passed,
            "failed_layers": failed_layers,
            "layer_values": layer_values,
            "targets": TARGETS,
            "target_pass": {
                "MDM2": {"l1_pass": passed, "l2_pass": passed, "l3_pass": passed,
                         "l4_pass": passed, "l5_pass": passed, "l6_pass": passed, "l7_pass": passed},
                "MDMX": {"l1_pass": passed, "l2_pass": passed, "l3_pass": passed,
                         "l4_pass": passed, "l5_pass": passed, "l6_pass": passed, "l7_pass": passed},
            },
        }
        con.execute(
            "INSERT INTO evidence_events(event_id, transaction_id, workflow_id, run_id, task_id, candidate_id, agent, event_type, timestamp, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()), None, None, None, None, candidate_id, "prediction",
                "battery_evaluated", now_iso, json.dumps(payload),
            ),
        )
    con.execute("COMMIT")


def clean_stress_rows(con: sqlite3.Connection) -> None:
    con.execute("BEGIN")
    con.execute("DELETE FROM candidates WHERE json_extract(payload_json, '$.stress') = 1")
    con.execute("DELETE FROM evidence_events WHERE json_extract(payload_json, '$.stress') = 1")
    con.execute("COMMIT")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=1500, help="number of synthetic candidates")
    args = parser.parse_args()
    n = max(1, args.n)

    con = sqlite3.connect(os.environ["CYCPEP_DB_PATH"])
    try:
        insert_stress_rows(con, n)
        print(f"inserted {n} stress candidates + battery rows into LOCAL store")

        from data_layer import get_storage_backend
        from web_api.results import ResultsReader
        from web_api.workbench import WorkbenchReader, DEFAULT_COLLECTION_LIMIT

        store = get_storage_backend()
        started = time.perf_counter()
        workbench = WorkbenchReader(store).read()
        wb_ms = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        results = ResultsReader(store).read()
        res_ms = (time.perf_counter() - started) * 1000

        print(f"WorkbenchReader.read(): {wb_ms:.0f} ms")
        print(f"ResultsReader.read():   {res_ms:.0f} ms")

        for key in ("candidates", "evidence", "artifacts", "protocols"):
            collection = workbench.get(key) or {}
            total = collection.get("total")
            returned = len(collection.get("items") or [])
            truncated = "truncated" if total is not None and returned < total else "complete"
            print(f"  {key:<12} total={total} returned={returned} -> {truncated}")

        summary = results["summary"]
        print(
            "  results: total=%s evaluated=%s hard_cleared=%s "
            "(collection cap=%s, finalists cap=50)" % (
                summary["candidates_total"], summary["candidates_evaluated"],
                summary["hard_cleared"], DEFAULT_COLLECTION_LIMIT,
            )
        )
        if wb_ms > 3000 or res_ms > 3000:
            print("WARNING: read latency above 3s at this volume")
    finally:
        clean_stress_rows(con)
        con.close()

    from data_layer import get_storage_backend
    from web_api.workbench import WorkbenchReader
    restored = (WorkbenchReader(get_storage_backend()).read().get("candidates") or {}).get("total")
    print(f"stress rows removed; candidates restored to {restored}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
