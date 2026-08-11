"""LOCAL-ONLY demo fixture seeder for the Frontend V2 workbench.

Writes synthetic but well-formed candidates, evidence, artifacts, and one
bound Orchestrator run into the LOCAL store (data/store.db) plus local run
files under demo/snapshot/demo_run/. It never touches the server, git, or any
shared/remote resource. Re-running this script first removes the rows/files it
created in a previous run (marked with demo_fixture=true or DEMO id prefix).

The seven-layer verdicts are computed by the real ``evaluate_battery`` against
the current ``state.json`` thresholds -- nothing is hardcoded. C0101/C0102 are
synthetic candidates whose metrics genuinely clear all seven layers under the
bundled default thresholds; C0103 fails L4/L6; C0104 is designed but awaits
prediction (shown as pending, not passed).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["CYCPEP_DB_PATH"] = str(ROOT / "data" / "store.db")

from battery_evaluation import evaluate_battery  # noqa: E402
from agents.orchestrator.service import initialize as orchestrator_initialize  # noqa: E402
from agents.planner.plan_builder import build_plan  # noqa: E402
from contracts.plan import MANDATORY_POLICY_CONSTRAINTS as POLICY_CONSTRAINTS  # noqa: E402
from contracts.trace import TraceContext  # noqa: E402
from data_layer import EvidenceLogger, State  # noqa: E402
from exploration import exploration_shortlist, record_exploration_shortlist  # noqa: E402
from prediction_pipeline.contracts import file_sha256, object_sha256  # noqa: E402
from web_api.workbench import WorkbenchReader  # noqa: E402
from web_api.results import ResultsReader  # noqa: E402

DB_PATH = ROOT / "data" / "store.db"
FIXTURE_DIR = ROOT / "demo" / "snapshot" / "demo_run"
ARTIFACT_DIR = FIXTURE_DIR / "artifacts"
PROJECT_ID = "mdm2_mdmx_reference"
TARGETS = ["MDM2", "MDMX"]

NOW = datetime.now(timezone.utc).isoformat()

# Metric keys follow the v6 canonical flat names consumed by
# ``evaluate_battery`` (``target_value``/``global_value`` fallbacks).
CANDIDATES = [
    {
        "candidate_id": "C0101",
        "sequence": "cyclo[D-Phe-Glu-Pro-Arg-Lys-Thr]",
        "source_route": "route_A",
        "status": "accepted",
        "final_status": "accepted",
        "metrics": {
            "plddt": 0.87,
            "ipsae_mdm2": 0.94, "ipsae_mdmx": 0.92,
            "dg_mdm2": -12.5, "dg_mdmx": -11.8, "dg_method": "PRODIGY",
            "sc_mdm2": 0.71, "sc_mdmx": 0.69,
            "dsasa_mdm2": 520.0, "dsasa_mdmx": 480.0,
            "nc_distance_pre": 0.9, "nc_distance_post": 1.1,
            "hotspot_cov_mdm2": 0.95, "hotspot_cov_mdmx": 0.93,
            "site_consistency_mdm2": "yes", "site_consistency_mdmx": "yes",
            "pose_rmsd_mdm2": 0.32, "pose_rmsd_mdmx": 0.38,
            "seed_convergence_mdm2": 0.9, "seed_convergence_mdmx": 0.85,
            "scrmsd": 1.3,
        },
    },
    {
        "candidate_id": "C0102",
        "sequence": "cyclo[D-Ala-Glu-Pro-Arg-Lys-Tyr]",
        "source_route": "route_A",
        "status": "evaluated",
        "metrics": {
            "plddt": 0.82,
            "ipsae_mdm2": 0.93, "ipsae_mdmx": 0.91,
            "dg_mdm2": -11.2, "dg_mdmx": -10.8, "dg_method": "PRODIGY",
            "sc_mdm2": 0.68, "sc_mdmx": 0.66,
            "dsasa_mdm2": 460.0, "dsasa_mdmx": 440.0,
            "nc_distance_pre": 1.1, "nc_distance_post": 1.4,
            "hotspot_cov_mdm2": 0.92, "hotspot_cov_mdmx": 0.91,
            "site_consistency_mdm2": "yes", "site_consistency_mdmx": "yes",
            "pose_rmsd_mdm2": 0.40, "pose_rmsd_mdmx": 0.45,
            "seed_convergence_mdm2": 0.86, "seed_convergence_mdmx": 0.80,
            "scrmsd": 1.6,
        },
    },
    {
        "candidate_id": "C0103",
        "sequence": "cyclo[D-Phe-Glu-Pro-Arg-Lys-Trp]",
        "source_route": "route_B",
        "status": "evaluated",
        "metrics": {
            "plddt": 0.81,
            "ipsae_mdm2": 0.68, "ipsae_mdmx": 0.91,
            "dg_mdm2": -9.2, "dg_mdmx": -8.7, "dg_method": "PRODIGY",
            "sc_mdm2": 0.55, "sc_mdmx": 0.52,
            "dsasa_mdm2": 380.0, "dsasa_mdmx": 350.0,
            "nc_distance_pre": 1.8, "nc_distance_post": 2.1,
            "hotspot_cov_mdm2": 0.80, "hotspot_cov_mdmx": 0.74,
            "site_consistency_mdm2": "yes", "site_consistency_mdmx": "yes",
            "pose_rmsd_mdm2": 1.8, "pose_rmsd_mdmx": 1.9,
            "seed_convergence_mdm2": 0.50, "seed_convergence_mdmx": 0.45,
            "scrmsd": 2.0,
        },
    },
    {
        "candidate_id": "C0104",
        "sequence": "cyclo[D-Val-Glu-Pro-Arg-Lys-Ser]",
        "source_route": "route_C",
        "status": "designed",
        # Designed but awaiting prediction: only L1 (refold pLDDT) exists.
        "metrics": {"plddt": 0.85},
    },
]


def build_battery_report(candidate: dict, thresholds: dict) -> dict:
    """Compute one seven-layer verdict with the real battery (no hardcoding).

    ``evaluate_battery`` reads metrics from the candidate's top level (v5 flat
    keys) or ``metrics.targets/global`` (v6 nested); the fixture keeps metrics
    nested for storage, so flatten them for the battery call.
    """
    flat = dict(candidate)
    flat.update(candidate.get("metrics") or {})
    return evaluate_battery(flat, thresholds or {}, required_targets=tuple(TARGETS))


def build_critic_report() -> dict:
    issues = [
        {
            "code": "l2_interface_confidence_low",
            "severity": "high",
            "category": "scientific_metric",
            "message": "Interface confidence for the dual-target cohort is below the calibrated L2 target on MDMX.",
            "candidate_ids": ["C0101", "C0102"],
            "evidence": [],
            "recommended_action": "iterate_interface_design",
            "owner_hint": "design",
            "blocks_finalization": True,
            "approval_required": False,
            "priority": "P1",
        },
        {
            "code": "threshold_calibration_pending",
            "severity": "medium",
            "category": "process",
            "message": "The L5 MDMX threshold is still team-provisional; calibration should be re-run.",
            "candidate_ids": [],
            "evidence": [],
            "recommended_action": "calibrate_thresholds",
            "owner_hint": "research",
            "blocks_finalization": False,
            "approval_required": False,
            "priority": "P2",
        },
    ]
    recommendations = [
        {
            "action": "iterate_interface_design",
            "owner_hint": "design",
            "priority": "P1",
            "reason_codes": ["l2_interface_confidence_low"],
            "approval_required": False,
        },
        {
            "action": "calibrate_thresholds",
            "owner_hint": "research",
            "priority": "P2",
            "reason_codes": ["threshold_calibration_pending"],
            "approval_required": False,
        },
    ]
    digest = object_sha256({"fixture": "demo-v1", "issues": issues, "verdict": "iterate"})
    report_id = f"critic_{digest[:12]}"
    return {
        "schema_version": 1,
        "critic_version": "1.0.0",
        "report_id": report_id,
        "input_digest": digest,
        "source": {
            "prediction_handoff": str(FIXTURE_DIR / "prediction_handoff.json"),
            "prediction_handoff_sha256": "b" * 64,
            "prediction_run_id": "prediction_demo_fixture",
            "prediction_pipeline_version": "1.5.0",
            "project_id": PROJECT_ID,
            "required_targets": TARGETS,
            "record_count": len(CANDIDATES),
        },
        "verdict": "iterate",
        "passed": False,
        "summary": "Demo fixture critic report: one design iteration plus threshold calibration recommendation.",
        "issue_counts": {"high": 1, "medium": 1},
        "issues": issues,
        "metrics_snapshot": {},
        "recommendations": recommendations,
        "planner_handoff": {
            "critic_report_id": report_id,
            "issue_codes": [issue["code"] for issue in issues],
            "recommended_actions": ["iterate_interface_design", "calibrate_thresholds"],
            "policy_constraints": sorted(POLICY_CONSTRAINTS),
        },
    }


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH), timeout=30)
    con.execute("PRAGMA busy_timeout = 30000")
    con.row_factory = sqlite3.Row
    return con


def reset_demo_rows(con: sqlite3.Connection) -> None:
    con.execute("DELETE FROM candidates WHERE json_extract(payload_json, '$.demo_fixture') = 1")
    con.execute("DELETE FROM evidence_events WHERE json_extract(payload_json, '$.demo_fixture') = 1")
    con.execute("DELETE FROM artifacts WHERE artifact_id LIKE 'ART-DEMO-%'")
    con.execute("DELETE FROM execution_transactions WHERE transaction_id LIKE 'TX-DEMO-%'")
    con.commit()


def insert_candidates(con: sqlite3.Connection, workflow_id: str, run_id: str, plan_id: str) -> None:
    for candidate in CANDIDATES:
        payload = dict(candidate)
        payload.update({
            "demo_fixture": True,
            "project_id": PROJECT_ID,
            "workflow_id": workflow_id,
            "run_id": run_id,
            "plan_id": plan_id,
            "protocol": {"name": "design", "version": "2.1", "integrity_identity": "protocol-design-v2.1"},
        })
        metrics = {key: value for key, value in (candidate.get("metrics") or {}).items()}
        con.execute(
            "INSERT INTO candidates(candidate_id, project_id, sequence, status, metrics_json, created_at, updated_at, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate["candidate_id"],
                PROJECT_ID,
                candidate["sequence"],
                candidate.get("status"),
                json.dumps(metrics),
                NOW,
                NOW,
                json.dumps(payload),
            ),
        )
    con.commit()


def log_evidence(workflow_id: str, run_id: str, plan_id: str, report_id: str, thresholds: dict) -> list[str]:
    base = TraceContext(project_id=PROJECT_ID, workflow_id=workflow_id, run_id=run_id, plan_id=plan_id)
    event_ids: list[str] = []

    event_ids.append(EvidenceLogger.log(
        "design", "design_batch",
        {
            "demo_fixture": True,
            "route": "route_A",
            "n_generated": 12,
            "n_valid": 10,
            "tool_trace": {"tool_name": "RFdiffusion", "tool_version": "1.2.3", "exit_code": 0, "duration_sec": 312.0},
        },
        targets=TARGETS, phase="design", trace_context=base,
    ))

    for candidate in CANDIDATES:
        if candidate["candidate_id"] == "C0104":
            # Designed but not yet predicted: keep out of the evaluated set so
            # the results digest reports it as pending rather than passed.
            continue
        trace = base.with_updates(candidate_id=candidate["candidate_id"])
        battery = build_battery_report(candidate, thresholds)
        event_ids.append(EvidenceLogger.log(
            "prediction", "candidate_scored",
            {
                "demo_fixture": True,
                "candidate_id": candidate["candidate_id"],
                "layer": 7,
                "scores": {"metrics": candidate.get("metrics") or {}},
                "tool_trace": {"tool_name": "battery", "tool_version": "1.0.0", "exit_code": 0},
                "passed": bool(battery["all_layers_pass"]),
                "targets": TARGETS,
            },
            targets=TARGETS, phase="evaluate", trace_context=trace,
        ))
        # Same payload shape as EvidenceLogger.battery_evaluated, plus the
        # demo_fixture marker so re-runs clean up their own rows.
        event_ids.append(EvidenceLogger.log(
            "prediction", "battery_evaluated",
            {
                "demo_fixture": True,
                "candidate_id": candidate["candidate_id"],
                "sequence": candidate["sequence"],
                "route": candidate.get("source_route"),
                "passed": bool(battery["all_layers_pass"]),
                "competition_clearance": bool(battery["competition_clearance"]),
                "failed_layers": battery["failed_layers"],
                "hard_failures": battery["hard_failures"],
                "missing_thresholds": battery["missing_thresholds"],
                "triage_status": battery["triage_status"],
                "layer_values": battery["layer_values"],
                "target_pass": battery["target_pass"],
            },
            targets=battery["required_targets"], phase="evaluate", trace_context=trace,
        ))

    shortlist = exploration_shortlist(
        events=[event for event in EvidenceLogger.get_all() if event.get("demo_fixture") and event.get("event_type") == "battery_evaluated"],
        targets=TARGETS, k=4,
    )
    event_ids.append(record_exploration_shortlist(
        shortlist, targets=TARGETS, round_num=1, trace_context=base,
    ))

    event_ids.append(EvidenceLogger.log(
        "critic", "critic_review",
        {
            "demo_fixture": True,
            "report_id": report_id,
            "verdict": "iterate",
            "passed": False,
            "issue_codes": ["l2_interface_confidence_low", "threshold_calibration_pending"],
        },
        targets=TARGETS, phase="critic", round_num=1, trace_context=base,
    ))
    return event_ids


def insert_artifacts_and_transactions(
    con: sqlite3.Connection, workflow_id: str, run_id: str, plan_id: str, succeeded_tasks: list[dict]
) -> None:
    for index, task in enumerate(succeeded_tasks, start=1):
        task_id = task["task_id"]
        action = task["action"]
        attempt_id = TraceContext.attempt_id_for(task_id, 1)

        artifact_id = f"ART-DEMO-{index:02d}"
        artifact_path = ARTIFACT_DIR / f"{task_id}-{action}.json"
        protocol = dict(task.get("protocol") or {})
        artifact_payload = {
            "demo_fixture": True,
            "task_id": task_id,
            "action": action,
            "workflow_id": workflow_id,
            "run_id": run_id,
            "plan_id": plan_id,
            "produced_at": NOW,
        }
        if protocol:
            artifact_payload["protocol"] = protocol
            artifact_payload["protocol_sha256"] = object_sha256(protocol)
        artifact_path.write_text(
            json.dumps(artifact_payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        con.execute(
            "INSERT INTO artifacts(artifact_id, artifact_type, path, size_bytes, sha256, producer_task_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                artifact_id,
                action,
                str(artifact_path),
                artifact_path.stat().st_size,
                file_sha256(artifact_path),
                task_id,
                NOW,
            ),
        )

        transaction_id = f"TX-DEMO-{index:02d}"
        payload = {
            "demo_fixture": True,
            "workflow_id": workflow_id,
            "run_id": run_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "artifact_ids": [artifact_id],
            "metadata": {"project_id": PROJECT_ID},
            "event_ids": [],
        }
        con.execute(
            "INSERT INTO execution_transactions(transaction_id, task_id, attempt_id, status, created_at, updated_at, payload_json) "
            "VALUES (?, ?, ?, 'COMMITTED', ?, ?, ?)",
            (transaction_id, task_id, attempt_id, NOW, NOW, json.dumps(payload)),
        )
    con.commit()


def main() -> None:
    shutil.rmtree(FIXTURE_DIR, ignore_errors=True)
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    state = dict(State.load())
    state["round"] = 1
    state["orchestrator"] = {}
    State.update({"orchestrator": {}})
    thresholds = state.get("thresholds") or {}

    report = build_critic_report()
    report_path = FIXTURE_DIR / "critic_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    plan = build_plan(critic_report_path=report_path, state=state)
    _KIND_BY_AGENT = {
        "design": "scientific",
        "prediction": "scientific",
        "critic": "review",
        "research": "review",
        "reporter": "review",
    }
    _PROTOCOL_BY_ACTION = {
        "iterate_design": {"name": "design", "version": "2.1", "integrity_identity": "protocol-design-v2.1"},
        "evaluate_new_design_candidates": {
            "name": "prediction", "version": "1.3", "integrity_identity": "protocol-prediction-v1.3",
        },
        "review_prediction_handoff": {"name": "critic", "version": "1.0", "integrity_identity": "protocol-critic-v1.0"},
        "propose_threshold_calibration": {
            "name": "calibration", "version": "1.2", "integrity_identity": "protocol-calibration-v1.2",
        },
    }
    for task in plan["tasks"]:
        task.setdefault("kind", _KIND_BY_AGENT.get(task.get("agent"), "scientific"))
        action = task.get("action")
        if action in _PROTOCOL_BY_ACTION:
            task.setdefault("protocol", dict(_PROTOCOL_BY_ACTION[action]))
    plan_path = FIXTURE_DIR / "planner_plan.json"
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

    print("PLAN TASKS:")
    for task in plan["tasks"]:
        print("  ", task["task_id"], task["action"], "agent=", task.get("agent"),
              "disposition=", task.get("disposition"),
              "gate=", (task.get("execution_gate") or {}).get("status"),
              "approval=", (task.get("approval") or {}).get("required"))

    run_path = FIXTURE_DIR / "orchestrator_run.json"
    initialize_result = orchestrator_initialize(plan_path=plan_path, output_path=run_path)
    run = dict(initialize_result["run"])
    workflow_id = run["workflow_id"]
    run_id = run["run_id"]
    plan_id = run["plan"]["plan_id"]
    print("RUN:", run_id, workflow_id, plan_id, "status=", run["status"])

    run = json.loads(run_path.read_text(encoding="utf-8"))
    succeeded_tasks: list[dict] = []
    for task in plan["tasks"]:
        task_id = task["task_id"]
        task_state = run["tasks"][task_id]
        if task.get("disposition") == "optional":
            task_state["status"] = "skipped"
            task_state["attempts"] = 0
        else:
            task_state["status"] = "succeeded"
            task_state["attempts"] = 1
            task_state.pop("last_error", None)
            succeeded_tasks.append(task)
    run_path.write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")

    con = connect()
    reset_demo_rows(con)
    insert_candidates(con, workflow_id, run_id, plan_id)
    log_evidence(workflow_id, run_id, plan_id, report["report_id"], thresholds)
    insert_artifacts_and_transactions(con, workflow_id, run_id, plan_id, succeeded_tasks)
    con.close()

    print("Fixture seeded. Verifying read models ...")
    from data_layer import get_storage_backend
    store = get_storage_backend()
    view = WorkbenchReader(store).read()
    for key in ("candidates", "evidence", "artifacts", "transactions", "protocols", "blockers", "tasks"):
        collection = view.get(key) or {}
        print(f"  {key}: total={collection.get('total')}")
    print("  workflow:", view.get("workflow"))
    print("  run:", view.get("run"))

    results = ResultsReader(store).read()
    summary = results["summary"]
    print("  results: total=%s evaluated=%s pending=%s hard_cleared=%s rate=%s" % (
        summary["candidates_total"], summary["candidates_evaluated"],
        summary["candidates_pending_prediction"], summary["hard_cleared"],
        summary["hard_clearance_rate"]))
    for layer in results["layers"]:
        print(f"    {layer['key']:<18} evaluated={layer['evaluated']} passed={layer['passed']}")
    print("  conclusion:", results["conclusion"])


if __name__ == "__main__":
    main()
