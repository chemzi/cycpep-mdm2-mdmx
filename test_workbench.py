"""Frontend V2 workbench read-model contract tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agents.orchestrator import OrchestratorContractError
from prediction_pipeline.contracts import file_sha256
from storage import SQLiteStore
from web_api.workbench import WorkbenchReader


class FakeStore:
    project_id = "project-1"

    def __init__(self, *, state=None, candidates=(), evidence=(), artifacts=(), transactions=()):
        self.state = dict(state or {})
        self.candidates = list(candidates)
        self.evidence = list(evidence)
        self.artifacts = list(artifacts)
        self.transactions = list(transactions)
        self.calls = []

    def get_state(self, project_id):
        self.calls.append(("get_state", project_id))
        return dict(self.state)

    def list(self):
        self.calls.append(("list",))
        return list(self.candidates)

    def query(self, **filters):
        self.calls.append(("query", filters))
        return [
            item for item in self.evidence
            if all(item.get(key) == value for key, value in filters.items())
        ]

    def list_artifacts(self):
        self.calls.append(("list_artifacts",))
        return list(self.artifacts)

    def list_transactions(self, **filters):
        self.calls.append(("list_transactions", filters))
        return [
            item for item in self.transactions
            if all(item.get(key) == value for key, value in filters.items() if value is not None)
        ]


def _task(task_id, action, depends_on=(), *, gate="proposed", approval=False):
    return {
        "task_id": task_id,
        "action": action,
        "agent": "design",
        "kind": "scientific",
        "phase": "iterate",
        "disposition": "required",
        "depends_on": list(depends_on),
        "resource_request": {"class": "cpu"},
        "approval": {"required": approval, "types": []},
        "execution_gate": {"status": gate, "block_reasons": ["manual_review"] if gate == "blocked" else []},
    }


def _commit_production_shaped_candidate(store: SQLiteStore, root: Path) -> None:
    candidates = [{
        "candidate_id": f"C{index:04d}",
        "sequence": "GSLALESLAG",
        "source_route": "route_A_mdm2",
    } for index in range(1, 15)]
    store.commit_transaction(
        context={
            "transaction_id": "tx-design", "workflow_id": "workflow-design",
            "run_id": "run-design", "task_id": "T001",
            "attempt_id": "T001-A01", "action": "iterate_design",
            "status": "COMMITTING", "metadata": {"project_id": "project-1"},
        },
        candidate_updates=candidates, state_updates={}, state_appends=(), artifacts=(),
    )
    inventory, artifacts = [], []
    for index in range(1, 102):
        artifact_id = f"prediction-input-{index:04d}"
        path = root / f"input-{index:04d}.dat"
        path.write_text(f"scientific artifact {index}", encoding="utf-8")
        role = "global.post_relax_pdb" if index == 1 else f"global.metric[{index}]"
        artifact_type = (
            "prediction_input:global.post_relax_pdb"
            if index == 1 else f"prediction_input:{role}"
        )
        inventory.append({
            "artifact_id": artifact_id, "path": str(path), "role": role,
            "sha256": file_sha256(path),
        })
        artifacts.append({
            "artifact_id": artifact_id, "artifact_type": artifact_type,
            "path": str(path), "size_bytes": path.stat().st_size,
            "sha256": file_sha256(path),
        })
    record_path = root / "C0006.prediction.json"
    record_path.write_text(json.dumps({
        "candidate": {"candidate_id": "C0006", "sequence": "GSLALESLAG"},
        "status": "needs_optimization", "artifact_inventory": inventory,
    }), encoding="utf-8")
    record_id = "prediction-record-C0006"
    artifacts.append({
        "artifact_id": record_id, "artifact_type": "prediction_record",
        "path": str(record_path), "size_bytes": record_path.stat().st_size,
        "sha256": file_sha256(record_path),
    })
    evidence_events = [{
        "event_id": "prediction-recorded-C0006", "agent": "prediction",
        "event_type": "prediction_recorded", "phase": "evaluate",
        "candidate_id": "C0006", "prediction_status": "needs_optimization",
        "prediction_run_id": "prediction-run-2", "record_artifact_id": record_id,
    }]
    evidence_events.extend({
        "event_id": f"candidate-scored-C0006-{index:04d}",
        "agent": "prediction", "event_type": "candidate_scored",
        "phase": "evaluate", "candidate_id": "C0006", "layer": (index % 7) + 1,
    } for index in range(101))
    store.commit_transaction(
        context={
            "transaction_id": "tx-prediction", "workflow_id": "workflow-prediction",
            "run_id": "run-prediction", "task_id": "T002",
            "attempt_id": "T002-A01", "action": "evaluate_new_design_candidates",
            "status": "COMMITTING", "metadata": {"project_id": "project-1"},
        },
        candidate_updates=(),
        candidate_patches=[{
            "candidate_id": "C0006",
            "patch": {
                "final_status": "needs_optimization",
                "metrics_json": json.dumps({
                    "global": {"plddt": 81.5},
                    "targets": {"MDM2": {"iptm": 0.67}},
                }),
            },
        }],
        state_updates={}, state_appends=(), artifacts=artifacts,
        evidence_events=evidence_events,
    )
    store.append({
        "event_id": "shortlist-C0006", "timestamp": "2026-08-13T18:00:00+00:00",
        "project_id": "project-1", "agent": "critic",
        "event_type": "exploration_shortlist", "phase": "critic",
        "shortlist": [{
            "candidate_id": "C0006", "passed": False,
            "reason": "optimization candidate",
        }],
    })


class WorkbenchReaderTests(unittest.TestCase):
    def test_candidate_normalizes_final_status_and_object_valued_metrics_json(self):
        store = FakeStore(
            state={"project_id": "project-1"},
            candidates=[{
                "candidate_id": "C0006",
                "sequence": "GSLALESLAG",
                "project_id": "project-1",
                "final_status": "needs_optimization",
                "metrics_json": json.dumps({
                    "global": {"plddt": 81.5},
                    "targets": {"MDM2": {"iptm": 0.67}},
                }),
            }],
        )

        candidate = WorkbenchReader(store).read()["candidates"]["items"][0]

        self.assertEqual(candidate["status"], "needs_optimization")
        self.assertEqual(candidate["metrics"]["global"]["plddt"], 81.5)
        self.assertEqual(candidate["associations"]["limitations"], [])

    def test_candidate_projection_survives_bounded_global_collections(self):
        root = Path(tempfile.mkdtemp(prefix="workbench-science-"))
        store = SQLiteStore(root / "store.db", project_id="project-1")
        _commit_production_shaped_candidate(store, root)

        byte_reads = []
        result = WorkbenchReader(
            store,
            artifact_bytes_reader=lambda path: (
                byte_reads.append(str(path)) or Path(path).read_bytes()
            ),
        ).read(limit=10)
        candidate = next(
            item for item in result["candidates"]["items"]
            if item["candidate_id"] == "C0006"
        )

        self.assertTrue(result["artifacts"]["truncated"])
        self.assertTrue(result["evidence"]["truncated"])
        self.assertEqual(candidate["status"], "needs_optimization")
        self.assertEqual(candidate["run_relation"], "historical_run")
        self.assertEqual(candidate["associations"]["status_owner"], {
            "run_id": "run-prediction",
            "run_relation": "historical_run",
        })
        self.assertEqual(candidate["associations"]["artifact_total"], 102)
        self.assertEqual(len(candidate["associations"]["artifact_ids"]), 102)
        self.assertIn("prediction-input-0101", candidate["associations"]["artifact_ids"])
        self.assertGreater(candidate["associations"]["evidence_total"], 100)
        self.assertTrue(candidate["associations"]["complete"])
        self.assertEqual(candidate["associations"]["limitations"], [])
        self.assertEqual(candidate["associations"]["structures"], [{
            "artifact_id": "prediction-input-0001",
            "artifact_type": "prediction_input:global.post_relax_pdb",
            "role": "global.post_relax_pdb",
            "content_link": "/api/v2/artifacts/prediction-input-0001/content",
        }])
        self.assertEqual(candidate["associations"]["shortlist"][0]["event_id"], "shortlist-C0006")
        self.assertEqual(len(byte_reads), 1, "only Prediction record bytes are re-hashed")
        self.assertNotIn(str(root), str(result))
        linked = [
            item for item in store.list_artifacts()
            if item["artifact_id"] == "prediction-input-0001"
        ][0]
        self.assertNotIn("candidate_id", linked)
        projected = next(
            item for item in result["artifacts"]["items"]
            if item["artifact_id"] == "prediction-input-0001"
        )
        self.assertEqual(projected["trace"]["candidate_id"], "C0006")

    def test_malformed_metrics_are_isolated_as_a_candidate_limitation(self):
        result = WorkbenchReader(FakeStore(
            state={"project_id": "project-1"},
            candidates=[{
                "candidate_id": "C0006",
                "sequence": "GSLALESLAG",
                "final_status": "needs_optimization",
                "metrics_json": "{malformed",
            }],
        )).read()

        candidate = result["candidates"]["items"][0]
        self.assertNotIn("metrics", candidate)
        self.assertEqual(candidate["associations"]["limitations"], [{
            "code": "candidate_metrics_malformed",
            "summary": "Candidate metrics are present but could not be read.",
        }])
        self.assertFalse(candidate["associations"]["complete"])

    def test_candidate_metrics_omit_internal_locator_keys_without_losing_science(self):
        result = WorkbenchReader(FakeStore(
            state={"project_id": "project-1"},
            candidates=[{
                "candidate_id": "C0006",
                "sequence": "GSLALESLAG",
                "metrics_json": json.dumps({
                    "prediction": {
                        "record_path": "C:/internal/prediction/C0006.json",
                        "record_artifact_id": "record-C0006",
                    },
                    "global": {"plddt": 81.5},
                }),
            }],
        )).read()

        metrics = result["candidates"]["items"][0]["metrics"]
        self.assertNotIn("record_path", metrics["prediction"])
        self.assertEqual(metrics["prediction"]["record_artifact_id"], "record-C0006")
        self.assertEqual(metrics["global"]["plddt"], 81.5)
        self.assertNotIn("C:/internal", str(result))

        direct = WorkbenchReader(FakeStore(
            state={"project_id": "project-1"},
            candidates=[{
                "candidate_id": "C0007",
                "sequence": "AAAAAAAA",
                "metrics": {
                    "prediction": {
                        "record_path": "/internal/C0007.json",
                        "record_artifact_id": "record-C0007",
                    },
                    "global": {"plddt": 79.0},
                },
            }],
        )).read()["candidates"]["items"][0]["metrics"]
        self.assertNotIn("record_path", direct["prediction"])
        self.assertEqual(direct["prediction"]["record_artifact_id"], "record-C0007")
        self.assertEqual(direct["global"]["plddt"], 79.0)

    def test_unverified_prediction_record_fails_closed_without_id_or_path_inference(self):
        root = Path(tempfile.mkdtemp(prefix="workbench-science-tamper-"))
        record_path = root / "C0006-prediction-record.json"
        record_path.write_text(json.dumps({
            "candidate": {"candidate_id": "C0006"},
            "artifact_inventory": [],
        }), encoding="utf-8")
        digest = file_sha256(record_path)
        common = {
            "state": {"project_id": "project-1"},
            "candidates": [{
                "candidate_id": "C0006",
                "sequence": "GSLALESLAG",
                "final_status": "needs_optimization",
            }],
            "evidence": [{
                "event_id": "prediction-recorded-C0006",
                "timestamp": "2026-08-13T18:00:00+00:00",
                "project_id": "project-1",
                "agent": "prediction",
                "event_type": "prediction_recorded",
                "candidate_id": "C0006",
                "transaction_id": "tx-prediction",
                "run_id": "run-prediction",
                "record_artifact_id": "prediction-record-C0006",
            }],
            "artifacts": [{
                "artifact_id": "prediction-record-C0006",
                "artifact_type": "prediction_record",
                "path": str(record_path),
                "sha256": digest,
                "transaction_id": "tx-other",
                "project_id": "project-1",
            }],
            "transactions": [{
                "transaction_id": "tx-prediction",
                "project_id": "project-1",
                "workflow_id": "workflow-prediction",
                "run_id": "run-prediction",
                "task_id": "T002",
                "attempt_id": "T002-A01",
                "status": "COMMITTED",
                "artifact_ids": ["prediction-record-C0006"],
            }],
        }

        candidate = WorkbenchReader(FakeStore(**common)).read()["candidates"]["items"][0]

        self.assertEqual(candidate["associations"]["artifact_total"], 0)
        self.assertEqual(candidate["associations"]["artifact_ids"], [])
        self.assertEqual(candidate["associations"]["structures"], [])
        self.assertEqual(candidate["associations"]["limitations"][0]["code"], "prediction_record_unverified")
        self.assertFalse(candidate["associations"]["complete"])
        self.assertNotIn("candidate_id", WorkbenchReader(FakeStore(**common)).read()["artifacts"]["items"][0]["trace"])

    def test_non_committed_and_trace_mismatched_prediction_transactions_fail_closed(self):
        root = Path(tempfile.mkdtemp(prefix="workbench-science-transaction-"))
        record_path = root / "record.json"
        record_path.write_text(json.dumps({
            "candidate": {"candidate_id": "C0006"},
            "artifact_inventory": [],
        }), encoding="utf-8")
        transaction = {
            "transaction_id": "tx-prediction",
            "status": "FAILED",
            "workflow_id": "workflow-prediction",
            "run_id": "run-prediction",
            "task_id": "T002",
            "attempt_id": "T002-A01",
            "artifact_ids": ["record-C0006"],
            "project_id": "project-1",
        }
        store = FakeStore(
            state={"project_id": "project-1"},
            candidates=[{"candidate_id": "C0006", "sequence": "AAAA"}],
            evidence=[{
                "event_id": "e1",
                "event_type": "prediction_recorded",
                "candidate_id": "C0006",
                "project_id": "project-1",
                "transaction_id": "tx-prediction",
                "workflow_id": "workflow-prediction",
                "run_id": "run-prediction",
                "task_id": "T002",
                "attempt_id": "T002-A01",
                "record_artifact_id": "record-C0006",
            }],
            artifacts=[{
                "artifact_id": "record-C0006",
                "artifact_type": "prediction_record",
                "path": str(record_path),
                "sha256": file_sha256(record_path),
                "transaction_id": "tx-prediction",
                "project_id": "project-1",
            }],
            transactions=[transaction],
        )

        not_committed = WorkbenchReader(store).read()["candidates"]["items"][0]
        self.assertEqual(
            not_committed["associations"]["limitations"][0]["code"],
            "prediction_transaction_unverified",
        )

        transaction["status"] = "COMMITTED"
        store.transactions[0]["status"] = "COMMITTED"
        store.evidence[0]["task_id"] = "T999"
        mismatched = WorkbenchReader(store).read()["candidates"]["items"][0]
        self.assertEqual(
            mismatched["associations"]["limitations"][0]["code"],
            "prediction_transaction_unverified",
        )
        self.assertNotIn("status_owner", mismatched["associations"])
        self.assertEqual(mismatched["run_relation"], "unlinked")

    def test_inventory_sha_mismatch_fails_closed_for_the_whole_association(self):
        root = Path(tempfile.mkdtemp(prefix="workbench-science-inventory-"))
        input_path = root / "structure.pdb"
        input_path.write_text("ATOM\n", encoding="utf-8")
        record_path = root / "record.json"
        record_path.write_text(json.dumps({
            "candidate": {"candidate_id": "C0006"},
            "artifact_inventory": [{
                "artifact_id": "input-C0006",
                "role": "global.post_relax_pdb",
                "path": str(input_path),
                "sha256": "0" * 64,
            }],
        }), encoding="utf-8")
        transaction = {
            "transaction_id": "tx-prediction",
            "status": "COMMITTED",
            "workflow_id": "workflow-prediction",
            "run_id": "run-prediction",
            "task_id": "T002",
            "attempt_id": "T002-A01",
            "artifact_ids": ["record-C0006", "input-C0006"],
            "project_id": "project-1",
        }
        store = FakeStore(
            state={"project_id": "project-1"},
            candidates=[{"candidate_id": "C0006", "sequence": "AAAA"}],
            evidence=[{
                "event_id": "e1",
                "event_type": "prediction_recorded",
                "candidate_id": "C0006",
                "project_id": "project-1",
                "transaction_id": "tx-prediction",
                "workflow_id": "workflow-prediction",
                "run_id": "run-prediction",
                "task_id": "T002",
                "attempt_id": "T002-A01",
                "record_artifact_id": "record-C0006",
            }],
            artifacts=[
                {
                    "artifact_id": "record-C0006",
                    "artifact_type": "prediction_record",
                    "path": str(record_path),
                    "sha256": file_sha256(record_path),
                    "transaction_id": "tx-prediction",
                    "project_id": "project-1",
                },
                {
                    "artifact_id": "input-C0006",
                    "artifact_type": "prediction_input:global.post_relax_pdb",
                    "path": str(input_path),
                    "sha256": file_sha256(input_path),
                    "transaction_id": "tx-prediction",
                    "project_id": "project-1",
                },
            ],
            transactions=[transaction],
        )

        candidate = WorkbenchReader(store).read()["candidates"]["items"][0]
        self.assertEqual(candidate["associations"]["artifact_total"], 0)
        self.assertEqual(candidate["associations"]["artifact_ids"], [])
        self.assertEqual(candidate["associations"]["structures"], [])
        self.assertEqual(
            candidate["associations"]["limitations"][0]["code"],
            "prediction_inventory_unverified",
        )

    def test_sha_tamper_and_candidate_mismatch_each_fail_closed(self):
        root = Path(tempfile.mkdtemp(prefix="workbench-science-mismatch-"))
        record_path = root / "record.json"
        record_path.write_text(json.dumps({
            "candidate": {"candidate_id": "C9999"},
            "artifact_inventory": [],
        }), encoding="utf-8")
        artifact = {
            "artifact_id": "record-C0006",
            "artifact_type": "prediction_record",
            "path": str(record_path),
            "sha256": file_sha256(record_path),
            "transaction_id": "tx-prediction",
            "project_id": "project-1",
        }
        store = FakeStore(
            state={"project_id": "project-1"},
            candidates=[{"candidate_id": "C0006", "sequence": "AAAA"}],
            evidence=[{
                "event_id": "e1",
                "event_type": "prediction_recorded",
                "candidate_id": "C0006",
                "project_id": "project-1",
                "transaction_id": "tx-prediction",
                "run_id": "run-prediction",
                "record_artifact_id": "record-C0006",
            }],
            artifacts=[artifact],
            transactions=[{
                "transaction_id": "tx-prediction",
                "status": "COMMITTED",
                "run_id": "run-prediction",
                "artifact_ids": ["record-C0006"],
                "project_id": "project-1",
            }],
        )

        mismatch = WorkbenchReader(store).read()["candidates"]["items"][0]
        self.assertEqual(
            mismatch["associations"]["limitations"][0]["code"],
            "prediction_record_candidate_mismatch",
        )
        self.assertNotIn("status_owner", mismatch["associations"])
        self.assertEqual(mismatch["run_relation"], "unlinked")

        record_path.write_text("tampered", encoding="utf-8")
        tampered = WorkbenchReader(store).read()["candidates"]["items"][0]
        self.assertEqual(
            tampered["associations"]["limitations"][0]["code"],
            "prediction_record_unverified",
        )
        self.assertNotIn("status_owner", tampered["associations"])
        self.assertEqual(tampered["run_relation"], "unlinked")

    def test_no_run_returns_project_scoped_history_with_explicit_collection_counts(self):
        store = FakeStore(
            state={"project_id": "project-1", "project": "Demo"},
            candidates=[
                {"candidate_id": "C1", "sequence": "AAAA", "project_id": "project-1", "run_id": "old-run"},
                {"candidate_id": "C2", "sequence": "BBBB", "project_id": "project-1"},
            ],
            evidence=[{"event_id": "e1", "event_type": "test", "project_id": "project-1", "run_id": "old-run"}],
        )

        result = WorkbenchReader(store).read(limit=1)

        self.assertEqual(result["project"], {"project_id": "project-1", "name": "Demo", "targets": []})
        self.assertIsNone(result["workflow"])
        self.assertIsNone(result["run"])
        self.assertEqual(result["tasks"], {"scope": "current_run", "total": 0, "returned": 0, "truncated": False, "items": []})
        self.assertEqual(result["candidates"]["scope"], "project")
        self.assertEqual((result["candidates"]["total"], result["candidates"]["returned"], result["candidates"]["truncated"]), (2, 1, True))
        self.assertEqual(result["candidates"]["items"][0]["run_relation"], "historical_run")
        self.assertEqual(result["blockers"]["items"][0]["code"], "no_current_run")

    def test_non_linear_tasks_preserve_graph_and_canonical_action_availability(self):
        plan = {
            "plan_id": "plan-1",
            "workflow_id": "workflow-1",
            "tasks": [
                _task("T001", "iterate_design"),
                _task("T002", "dock_shortlisted_candidates", ("T001",)),
                _task("T003", "review_prediction_handoff", ("T001",)),
            ],
        }
        run = {
            "run_id": "run-1",
            "workflow_id": "workflow-1",
            "status": "ready",
            "plan": {"plan_id": "plan-1", "project_id": "project-1", "plan_path": "internal"},
            "tasks": {
                "T001": {"status": "ready", "attempts": 0, "attempt_history": []},
                "T002": {"status": "blocked", "attempts": 0, "attempt_history": []},
                "T003": {"status": "pending_dependency", "attempts": 0, "attempt_history": []},
            },
        }
        store = FakeStore(state={"project_id": "project-1", "orchestrator": {"run_path": "internal"}})
        reader = WorkbenchReader(
            store,
            status_reader=lambda **_: {"run": run, "summary": {"status": "ready"}},
            plan_reader=lambda *_: plan,
        )

        result = reader.read()

        tasks = {item["task_id"]: item for item in result["tasks"]["items"]}
        self.assertEqual(tasks["T002"]["depends_on"], ["T001"])
        self.assertEqual(tasks["T003"]["depends_on"], ["T001"])
        self.assertFalse(tasks["T002"]["action"]["executable"])
        self.assertIn("action_not_executable", tasks["T002"]["availability"]["reason_codes"])
        self.assertTrue(tasks["T001"]["action"]["handler_available"])

    def test_transactions_keep_formal_status_and_trace_linkage(self):
        transactions = [
            {"transaction_id": f"tx-{status.lower()}", "project_id": "project-1", "workflow_id": "workflow-1", "run_id": "run-1", "task_id": "T001", "attempt_id": "T001-A01", "status": status}
            for status in ("COMMITTED", "FAILED", "ROLLED_BACK", "COMPENSATION_CONFLICT")
        ]
        run = {"run_id": "run-1", "workflow_id": "workflow-1", "status": "running", "plan": {"plan_id": "plan-1", "project_id": "project-1", "plan_path": "internal"}, "tasks": {}}
        reader = WorkbenchReader(
            FakeStore(state={"project_id": "project-1", "orchestrator": {"run_path": "internal"}}, transactions=transactions),
            status_reader=lambda **_: {"run": run, "summary": {"status": "running"}},
            plan_reader=lambda *_: {"plan_id": "plan-1", "workflow_id": "workflow-1", "tasks": []},
        )

        items = reader.read()["transactions"]["items"]

        self.assertEqual({item["status"] for item in items}, {"COMMITTED", "FAILED", "ROLLED_BACK", "COMPENSATION_CONFLICT"})
        self.assertTrue(all(item["run_id"] == "run-1" for item in items))

    def test_execution_states_cover_approval_claim_without_transaction_and_failure(self):
        plan = {
            "plan_id": "plan-1",
            "workflow_id": "workflow-1",
            "tasks": [
                _task("T001", "review_prediction_handoff", approval=True),
                _task("T002", "review_prediction_handoff", ("T001",)),
                _task("T003", "review_prediction_handoff", ("T001",)),
            ],
        }
        run = {
            "run_id": "run-1",
            "workflow_id": "workflow-1",
            "status": "running",
            "plan": {"plan_id": "plan-1", "project_id": "project-1", "plan_path": "internal"},
            "tasks": {
                "T001": {"status": "awaiting_approval", "attempts": 0},
                "T002": {"status": "claimed", "attempts": 1, "claim": {"attempt_id": "T002-A01", "worker": "worker-1"}},
                "T003": {"status": "failed", "attempts": 1, "last_error": {"code": "scientific_input_invalid", "message": "Input was rejected", "component": "execution", "retryable": False}},
            },
        }
        reader = WorkbenchReader(
            FakeStore(state={"project_id": "project-1", "orchestrator": {"run_path": "internal"}}),
            status_reader=lambda **_: {"run": run},
            plan_reader=lambda *_: plan,
        )

        result = reader.read()

        tasks = {item["task_id"]: item for item in result["tasks"]["items"]}
        executions = {item["task_id"]: item for item in result["executions"]["items"]}
        self.assertIn("approval_required", tasks["T001"]["availability"]["reason_codes"])
        self.assertEqual(executions["T002"]["transaction_visibility"], "not_yet_recorded")
        self.assertEqual(executions["T003"]["error"]["code"], "scientific_input_invalid")
        self.assertIn("scientific_input_invalid", {item["code"] for item in result["blockers"]["items"]})

    def test_current_attempt_does_not_inherit_an_earlier_transaction(self):
        plan = {"plan_id": "plan-1", "workflow_id": "workflow-1", "tasks": [_task("T001", "review_prediction_handoff")]}
        run = {
            "run_id": "run-1", "workflow_id": "workflow-1", "status": "running",
            "plan": {"plan_id": "plan-1", "project_id": "project-1", "plan_path": "internal"},
            "tasks": {"T001": {"status": "claimed", "attempts": 2, "claim": {"worker": "worker-1"}}},
        }
        old_transaction = {
            "transaction_id": "tx-a01", "project_id": "project-1", "workflow_id": "workflow-1",
            "run_id": "run-1", "task_id": "T001", "attempt_id": "T001-A01", "status": "FAILED",
        }
        reader = WorkbenchReader(
            FakeStore(
                state={"project_id": "project-1", "orchestrator": {"run_path": "internal"}},
                transactions=[old_transaction],
            ),
            status_reader=lambda **_: {"run": run},
            plan_reader=lambda *_: plan,
        )

        execution = reader.read()["executions"]["items"][0]

        self.assertEqual(execution["attempt_id"], "T001-A02")
        self.assertEqual(execution["transaction_visibility"], "not_yet_recorded")

    def test_unresolved_recovery_evidence_is_a_structured_blocker(self):
        plan = {"plan_id": "plan-1", "workflow_id": "workflow-1", "tasks": []}
        run = {
            "run_id": "run-1", "workflow_id": "workflow-1", "status": "failed",
            "plan": {"plan_id": "plan-1", "project_id": "project-1", "plan_path": "internal"},
            "tasks": {},
        }
        transaction = {
            "transaction_id": "tx-1", "project_id": "project-1", "workflow_id": "workflow-1",
            "run_id": "run-1", "task_id": "T001", "attempt_id": "T001-A01", "status": "COMMITTED",
        }
        store = FakeStore(
            state={"project_id": "project-1", "orchestrator": {"run_path": "internal"}},
            transactions=[transaction],
            evidence=[{
                "event_id": "e1", "event_type": "execution_transaction_post_commit_failure",
                "code": "transaction_recovery_unresolved", "project_id": "project-1",
                "workflow_id": "workflow-1", "run_id": "run-1", "transaction_id": "tx-1",
            }],
        )

        blockers = WorkbenchReader(
            store, status_reader=lambda **_: {"run": run}, plan_reader=lambda *_: plan
        ).read()["blockers"]["items"]

        self.assertIn(
            ("transaction_compensation_unresolved", "tx-1"),
            {(item["code"], item.get("transaction_id")) for item in blockers},
        )

    def test_duplicate_recovery_signals_produce_one_blocker(self):
        plan = {"plan_id": "plan-1", "workflow_id": "workflow-1", "tasks": []}
        run = {
            "run_id": "run-1", "workflow_id": "workflow-1", "status": "failed",
            "plan": {"plan_id": "plan-1", "project_id": "project-1", "plan_path": "internal"},
            "tasks": {},
        }
        signal = {
            "event_id": "e1", "event_type": "execution_transaction_compensation_unresolved",
            "project_id": "project-1", "workflow_id": "workflow-1", "run_id": "run-1",
            "transaction_id": "tx-1",
        }
        transaction = {
            "transaction_id": "tx-1", "project_id": "project-1", "workflow_id": "workflow-1",
            "run_id": "run-1", "task_id": "T001", "attempt_id": "T001-A01",
            "status": "COMPENSATION_CONFLICT",
        }
        reader = WorkbenchReader(
            FakeStore(
                state={"project_id": "project-1", "orchestrator": {"run_path": "internal"}},
                evidence=[signal], transactions=[transaction],
            ),
            status_reader=lambda **_: {"run": run}, plan_reader=lambda *_: plan,
        )

        blockers = reader.read()["blockers"]["items"]

        matching = [item for item in blockers if item["code"] == "transaction_compensation_unresolved"]
        self.assertEqual(len(matching), 1)

    def test_resolved_transaction_suppresses_stale_recovery_evidence(self):
        plan = {"plan_id": "plan-1", "workflow_id": "workflow-1", "tasks": []}
        run = {
            "run_id": "run-1", "workflow_id": "workflow-1", "status": "failed",
            "plan": {"plan_id": "plan-1", "project_id": "project-1", "plan_path": "internal"},
            "tasks": {},
        }
        transaction = {
            "transaction_id": "tx-1", "project_id": "project-1", "workflow_id": "workflow-1",
            "run_id": "run-1", "task_id": "T001", "attempt_id": "T001-A01", "status": "ROLLED_BACK",
        }
        stale_signal = {
            "event_id": "e1", "event_type": "execution_transaction_compensation_conflict",
            "project_id": "project-1", "workflow_id": "workflow-1", "run_id": "run-1",
            "transaction_id": "tx-1",
        }
        reader = WorkbenchReader(
            FakeStore(
                state={"project_id": "project-1", "orchestrator": {"run_path": "internal"}},
                evidence=[stale_signal], transactions=[transaction],
            ),
            status_reader=lambda **_: {"run": run}, plan_reader=lambda *_: plan,
        )

        blockers = reader.read()["blockers"]["items"]

        self.assertNotIn("transaction_compensation_unresolved", {item["code"] for item in blockers})

    def test_invalid_binding_returns_only_trustworthy_partial_data(self):
        store = FakeStore(
            state={"project_id": "project-1", "project": "Demo", "orchestrator": {"run_path": "secret/run.json"}},
            candidates=[{"candidate_id": "C1", "sequence": "AAAA", "project_id": "project-1", "run_id": "old-run"}],
        )

        def invalid_status(**_):
            raise OrchestratorContractError("run_plan_hash_mismatch", "secret/run.json")

        result = WorkbenchReader(store, status_reader=invalid_status).read()

        self.assertIsNone(result["workflow"])
        self.assertIsNone(result["run"])
        self.assertEqual(result["tasks"]["items"], [])
        self.assertEqual(result["executions"]["items"], [])
        self.assertEqual(result["transactions"]["items"], [])
        self.assertEqual(result["candidates"]["items"][0]["candidate_id"], "C1")
        blocker = result["blockers"]["items"][0]
        self.assertEqual((blocker["code"], blocker["scope"]), ("workflow_binding_invalid", "workflow"))
        self.assertNotIn("secret", str(result))

    def test_read_uses_only_read_methods_and_never_relabels_protocol_or_exposes_paths(self):
        store = FakeStore(
            state={"project_id": "project-1"},
            candidates=[{"candidate_id": "C1", "sequence": "AAAA", "design_pdb_path": "C:/secret/candidate.pdb"}],
            evidence=[{"event_id": "e1", "event_type": "test", "project_id": "project-1", "report_path": "C:/secret/report.json"}],
            artifacts=[{
                "artifact_id": "artifact-1",
                "artifact_type": "prediction",
                "path": "C:/secret/output.json",
                "project_id": "project-1",
                "run_id": "old-run",
                "protocol_name": "prediction",
                "protocol_version": "1.4",
                "protocol_sha256": "existing-integrity-identity",
            }],
        )

        result = WorkbenchReader(store).read()

        self.assertEqual(
            [call[0] for call in store.calls],
            ["get_state", "list", "query", "list_artifacts", "list_transactions"],
        )
        self.assertEqual(store.calls[2][1], {"project_id": "project-1"})
        self.assertEqual(store.calls[4][1], {})
        artifact = result["artifacts"]["items"][0]
        self.assertNotIn("path", artifact)
        self.assertEqual(artifact["protocol"]["version"], "1.4")
        self.assertEqual(artifact["protocol"]["integrity_identity"], "existing-integrity-identity")
        self.assertNotIn("C:/secret", str(result))

    def test_path_shaped_messages_and_unsupported_content_links_are_redacted(self):
        store = FakeStore(
            state={"project_id": "project-1"},
            evidence=[{
                "event_id": "e1", "event_type": "test", "project_id": "project-1",
                "message": "failed at C:/server/private/report.json",
            }],
            artifacts=[{
                "artifact_id": "artifact-1", "artifact_type": "report", "project_id": "project-1",
                "content_link": "file:///server/private/report.json",
            }],
        )

        result = WorkbenchReader(store).read()

        self.assertNotIn("C:/server", str(result))
        self.assertNotIn("file://", str(result))
        self.assertNotIn("content_link", result["artifacts"]["items"][0])

    def test_prediction_start_receipt_does_not_expose_internal_run_root(self):
        store = FakeStore(
            state={"project_id": "project-1"},
            evidence=[{
                "event_id": "e1",
                "event_type": "prediction_invocation_started",
                "project_id": "project-1",
                "launcher_run_id": "launcher-1",
                "prediction_invocation_id": "prediction-invocation-1",
                "prediction_run_id": "prediction-run-1",
                "prediction_run_root": "C:/internal/prediction/root",
                "prediction_run_locator": {
                    "root": "C:/internal/prediction/root",
                    "run_id": "prediction-run-1",
                },
                "runtime_locator_binding": {
                    "project_locator": "C:/internal/project.json",
                    "data_dir": "C:/internal/data",
                    "evidence_dir": "C:/internal/evidence",
                    "database_path": "C:/internal/formal/store.db",
                },
            }],
        )

        evidence = WorkbenchReader(store).read()["evidence"]["items"][0]

        self.assertEqual(evidence["event_type"], "prediction_invocation_started")
        self.assertNotIn("prediction_run_root", evidence)
        self.assertNotIn("prediction_run_locator", evidence)
        self.assertNotIn("runtime_locator_binding", evidence)
        self.assertNotIn("C:/internal", str(evidence))


if __name__ == "__main__":
    unittest.main()
