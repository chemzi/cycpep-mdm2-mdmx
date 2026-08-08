"""Data integrity and execution transaction regression tests."""

from __future__ import annotations

import json
import socket
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import data_layer
from contracts.candidate_update import (
    CANDIDATE_UPDATE_SCHEMA_VERSION,
    CandidateUpdate,
    CandidateUpdateBatch,
)
from contracts.transaction import TransactionContext, TransactionStatus
from data_layer import CandidateIndex, EvidenceLogger, State, refresh_projections
from execution.adapters import adapter_for
from execution.config import ExecutionConfig
from execution.contracts import _validate_design_result
from execution.handlers import HandlerContext
from execution.results import ExecutionActionResult, StateAppendMutation
from execution.recovery import RecoveryManager
from execution.staging import StagingArea
from execution.worker import ExecutionWorker, _validate_action_result
from prediction_pipeline.contracts import file_sha256, object_sha256
from storage import SQLiteStore


class DataIntegrityTransactionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="data-integrity-transaction-"))
        self.original_paths = (
            data_layer.DATA_DIR,
            data_layer.EVIDENCE_DIR,
            data_layer.STATE_PATH,
            data_layer.LOG_PATH,
            data_layer.INDEX_PATH,
        )
        data_layer.DATA_DIR = self.root / "data"
        data_layer.EVIDENCE_DIR = self.root / "evidence"
        data_layer.STATE_PATH = data_layer.DATA_DIR / "state.json"
        data_layer.LOG_PATH = data_layer.EVIDENCE_DIR / "evidence_log.jsonl"
        data_layer.INDEX_PATH = data_layer.DATA_DIR / "candidate_index.csv"
        State.save(dict(State._default))

    def tearDown(self):
        (
            data_layer.DATA_DIR,
            data_layer.EVIDENCE_DIR,
            data_layer.STATE_PATH,
            data_layer.LOG_PATH,
            data_layer.INDEX_PATH,
        ) = self.original_paths

    def _config(self) -> ExecutionConfig:
        return ExecutionConfig(
            repo_root=Path(__file__).resolve().parent,
            execution_root=self.root / "execution",
            core_python=Path(sys.executable),
            design_python=Path(sys.executable),
            prediction_python=Path(sys.executable),
            prediction_artifacts_root=self.root / "prediction_artifacts",
            prediction_runs_root=self.root / "prediction_runs",
            colabdesign_dir=self.root,
            colabdesign_params=self.root,
            cuda_data_dir=self.root,
            boltz_executable=None,
            boltz_cache=None,
            boltz_checkpoint=None,
            prodigy_executable=None,
            pyrosetta_python=None,
            control_data_path=None,
        )

    def _transaction(self, suffix: str = "1") -> TransactionContext:
        return TransactionContext.create(
            workflow_id="workflow-integrity",
            run_id="run-integrity",
            task_id="T001",
            attempt_id=f"T001-A0{suffix}",
            action="iterate_design",
        )

    @staticmethod
    def _mark_owner_dead(payload: dict) -> None:
        """Replace a live test-process lease with an explicitly dead owner."""
        payload["owner_host"] = socket.gethostname()
        payload["owner_pid"] = 99_999_999
        payload["owner_process_identity"] = "dead-test-process"
        payload["heartbeat_at"] = "2000-01-01T00:00:00+00:00"

    def test_concurrent_candidate_ids_are_unique_and_contiguous(self):
        store = SQLiteStore(self.root / "sequence.db", project_id="p1")
        with ThreadPoolExecutor(max_workers=12) as pool:
            values = list(pool.map(lambda _: store.reserve_candidate_ids(1)[0], range(48)))
        self.assertEqual(len(values), len(set(values)))
        self.assertEqual(sorted(values), [f"C{index:04d}" for index in range(1, 49)])

    def test_existing_store_schema_upgrades_before_creating_new_indexes(self):
        database = self.root / "legacy-store.db"
        with sqlite3.connect(database) as connection:
            connection.executescript("""
                CREATE TABLE candidates (
                    candidate_id TEXT PRIMARY KEY,
                    sequence TEXT NOT NULL,
                    status TEXT,
                    metrics_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    artifact_type TEXT,
                    path TEXT,
                    producer_task_id TEXT,
                    created_at TEXT NOT NULL
                );
            """)
        store = SQLiteStore(database, project_id="legacy")
        with store._connect() as connection:
            candidate_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(candidates)")
            }
            indexes = {
                row["name"] for row in connection.execute("PRAGMA index_list(candidates)")
            }
        self.assertIn("project_id", candidate_columns)
        self.assertIn("idx_candidates_project", indexes)
        with store._connect() as connection:
            artifact_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(artifacts)")
            }
        self.assertIn("sha256", artifact_columns)

    def test_state_store_honors_explicit_project_id(self):
        store = SQLiteStore(self.root / "projects.db", project_id="primary")
        store.replace_state("secondary", {"phase": "design", "round": 2})
        self.assertEqual(store.get_state("secondary")["phase"], "design")
        self.assertEqual(store.get_state("primary"), {})

    def test_projection_edits_never_flow_back_without_explicit_migration(self):
        CandidateIndex.add({"candidate_id": "C0001", "sequence": "GFEW"})
        data_layer.INDEX_PATH.write_text(
            "candidate_id,sequence\nC9999,EDITED\n", encoding="utf-8-sig"
        )
        self.assertIsNone(CandidateIndex.find("C9999"))
        refresh_projections()
        self.assertIn("C0001", data_layer.INDEX_PATH.read_text(encoding="utf-8-sig"))

    def test_multi_candidate_commit_orders_evidence_after_formal_rows(self):
        store = data_layer.get_storage_backend()
        context = self._transaction()
        worker = ExecutionWorker(
            store, self.root / "staging", self.root / "formal_artifacts"
        )

        def handler(_context, _staging):
            return ExecutionActionResult(candidate_updates=(
                {"candidate_id": "C0001", "sequence": "AAAA"},
                {"candidate_id": "C0002", "sequence": "BBBB"},
            ))

        worker.run(context, handler, validator=_validate_action_result)
        self.assertEqual(context.status, TransactionStatus.COMMITTED)
        self.assertEqual([item["candidate_id"] for item in store.list()], ["C0001", "C0002"])
        events = store.query(task_id="T001")
        self.assertEqual(
            [item["event_type"] for item in events],
            ["candidate_registered", "candidate_registered", "execution_transaction_committed"],
        )

    def test_committed_effects_can_be_compensated_and_retried(self):
        store = data_layer.get_storage_backend()
        worker = ExecutionWorker(
            store, self.root / "staging", self.root / "formal_artifacts"
        )
        source = self.root / "first" / "shared.pdb"
        source.parent.mkdir()
        source.write_text("MODEL", encoding="utf-8")

        def handler(_context, staging):
            artifact = staging.stage_artifact(
                source, artifact_id="C0001-design-pdb", artifact_type="design_pdb"
            )
            return ExecutionActionResult(
                candidate_updates=({"candidate_id": "C0001", "sequence": "AAAA"},),
                artifacts=(artifact,),
            )

        first = self._transaction("1")
        worker.run(first, handler, validator=_validate_action_result)
        self.assertEqual(store.get_state(store.project_id)["candidate_count"], 1)
        worker.rollback(first)
        self.assertEqual(first.status, TransactionStatus.ROLLED_BACK)
        self.assertEqual(store.list(), [])
        self.assertEqual(
            [event["event_type"] for event in store.query(
                transaction_id=first.transaction_id
            )],
            [
                "candidate_registered",
                "execution_transaction_committed",
                "execution_transaction_compensation_started",
                "execution_transaction_rolled_back",
            ],
        )
        self.assertFalse(any((self.root / "formal_artifacts").rglob("shared.pdb")))
        self.assertEqual(store.get_state(store.project_id)["candidate_count"], 0)

        retry = self._transaction("2")
        worker.run(retry, handler, validator=_validate_action_result)
        self.assertEqual(retry.status, TransactionStatus.COMMITTED)
        self.assertEqual([item["candidate_id"] for item in store.list()], ["C0001"])

    def test_critic_append_uses_latest_state_inside_commit(self):
        store = SQLiteStore(self.root / "state-append.db", project_id="p1")
        store.replace_state("p1", {"iteration_history": [], "phase": "iterate"})
        worker = ExecutionWorker(store, self.root / "staging", self.root / "artifacts")
        context = self._transaction()
        entry = {"agent": "critic", "summary": {"report_id": "critic-1"}}

        def handler(_context, _staging):
            store.update_state("p1", {"other_worker_effect": "preserved"})
            return ExecutionActionResult(state_appends=(StateAppendMutation(
                key="iteration_history",
                item=entry,
                identity_path=("summary", "report_id"),
                identity_value="critic-1",
            ),))

        worker.run(context, handler, validator=_validate_action_result)
        state = store.get_state("p1")
        self.assertEqual(state["other_worker_effect"], "preserved")
        self.assertEqual(state["iteration_history"], [entry])

    def test_rollback_only_compensates_own_state_effects_and_is_idempotent(self):
        store = SQLiteStore(self.root / "state-compensation.db", project_id="p1")
        store.replace_state("p1", {"base": True})
        worker = ExecutionWorker(store, self.root / "staging", self.root / "artifacts")
        first = self._transaction("1")
        second = self._transaction("2")
        worker.run(
            first,
            lambda *_: ExecutionActionResult(state_updates={"from_a": 1}),
            validator=_validate_action_result,
        )
        worker.run(
            second,
            lambda *_: ExecutionActionResult(state_updates={"from_b": 2}),
            validator=_validate_action_result,
        )
        worker.rollback(first)
        worker.rollback(first)
        self.assertEqual(store.get_state("p1"), {"base": True, "from_b": 2})

    def test_same_size_staged_artifact_tamper_is_rejected(self):
        store = SQLiteStore(self.root / "artifact-tamper.db", project_id="p1")
        worker = ExecutionWorker(store, self.root / "staging", self.root / "artifacts")
        source = self.root / "same-size.txt"
        source.write_text("AAAA", encoding="utf-8")

        def handler(_context, staging):
            artifact = staging.stage_artifact(
                source, artifact_id="same-size", artifact_type="text"
            )
            Path(artifact.staged_path).write_text("BBBB", encoding="utf-8")
            return ExecutionActionResult(artifacts=(artifact,))

        with self.assertRaisesRegex(ValueError, "sha256 changed"):
            worker.run(self._transaction(), handler, validator=_validate_action_result)
        self.assertIsNone(store.get_artifact("same-size"))

    def test_artifact_registry_persists_staged_sha256(self):
        store = SQLiteStore(self.root / "artifact-digest.db", project_id="p1")
        worker = ExecutionWorker(store, self.root / "staging", self.root / "artifacts")
        source = self.root / "digest.txt"
        source.write_text("artifact", encoding="utf-8")

        def handler(_context, staging):
            artifact = staging.stage_artifact(
                source, artifact_id="with-digest", artifact_type="text"
            )
            return ExecutionActionResult(artifacts=(artifact,))

        worker.run(self._transaction(), handler, validator=_validate_action_result)
        row = store.get_artifact("with-digest")
        self.assertEqual(row["sha256"], file_sha256(source))

    def test_artifact_content_changed_during_copy_is_rejected(self):
        """TOCTOU: content swapped between validate and copy must not register."""
        store = SQLiteStore(self.root / "copy-window.db", project_id="p1")
        worker = ExecutionWorker(store, self.root / "staging", self.root / "artifacts")
        source = self.root / "copy-source.txt"
        source.write_text("original", encoding="utf-8")

        def handler(_context, staging):
            artifact = staging.stage_artifact(
                source, artifact_id="copy-window", artifact_type="text"
            )
            return ExecutionActionResult(artifacts=(artifact,))

        import execution.commit_manager as commit_manager

        real_copyfile = commit_manager.shutil.copyfile

        def tampering_copyfile(src, dst):
            # Simulate a content swap landing only in the commit copy window:
            # the temporary formal copy differs from the staged digest.
            real_copyfile(src, dst)
            if str(dst).endswith(".tmp"):
                Path(dst).write_text("tampered-content", encoding="utf-8")

        with patch.object(
            commit_manager.shutil, "copyfile", side_effect=tampering_copyfile
        ), self.assertRaisesRegex(ValueError, "changed during commit"):
            worker.run(self._transaction(), handler, validator=_validate_action_result)
        self.assertIsNone(store.get_artifact("copy-window"))
        self.assertFalse(any((self.root / "artifacts").rglob("*.txt")))

    def test_recovery_reports_unresolved_compensation_conflict(self):
        store = SQLiteStore(self.root / "recovery-conflict.db", project_id="p1")
        store.replace_state("p1", {"candidate_count": 0})
        worker = ExecutionWorker(store, self.root / "staging", self.root / "artifacts")
        context = self._transaction()
        source = self.root / "conflict-artifact.txt"
        source.write_text("keep-me", encoding="utf-8")

        def handler(_context, staging):
            artifact = staging.stage_artifact(
                source, artifact_id="conflict-artifact", artifact_type="text"
            )
            return ExecutionActionResult(
                candidate_updates=({"candidate_id": "C0001", "sequence": "AAAA"},),
                state_updates={"shared": "from-a"},
                artifacts=(artifact,),
                evidence_events=({
                    "agent": "critic",
                    "event_type": "critic_review",
                    "phase": "critic",
                    "summary": "must survive conflict",
                },),
            )

        worker.run(
            context,
            handler,
            validator=_validate_action_result,
        )
        artifact = store.get_artifact("conflict-artifact")
        store.update_state("p1", {"shared": "from-b"})
        marker = (
            self.root / "staging" / context.transaction_id / "metadata" / "commit.json"
        )
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["status"] = "COMPENSATION_FAILED"
        marker.write_text(json.dumps(payload), encoding="utf-8")

        recovery = RecoveryManager(store)
        self.assertEqual(list(recovery.recover_pending(self.root / "staging").recovered), [])
        self.assertEqual(recovery.unresolved_transactions, [context.transaction_id])
        self.assertEqual(store.get_state("p1")["shared"], "from-b")
        self.assertIsNotNone(store.get("C0001"))
        self.assertTrue(store.query(task_id="T001"))
        self.assertIsNotNone(store.get_artifact("conflict-artifact"))
        self.assertTrue(Path(artifact["path"]).is_file())
        self.assertEqual(
            json.loads(marker.read_text(encoding="utf-8"))["status"],
            "COMPENSATION_CONFLICT",
        )

    def test_recovery_compensates_db_commit_before_orchestrator_closure(self):
        store = SQLiteStore(self.root / "crash-window.db", project_id="p1")
        store.replace_state("p1", {"candidate_count": 0, "phase": "iterate"})
        worker = ExecutionWorker(store, self.root / "staging", self.root / "artifacts")
        context = self._transaction()
        source = self.root / "crash-artifact.txt"
        source.write_text("committed", encoding="utf-8")

        def handler(_context, staging):
            artifact = staging.stage_artifact(
                source, artifact_id="crash-artifact", artifact_type="text"
            )
            return ExecutionActionResult(
                candidate_updates=({"candidate_id": "C0001", "sequence": "AAAA"},),
                state_updates={"phase": "critic"},
                artifacts=(artifact,),
            )

        worker.run(context, handler, validator=_validate_action_result)
        committed_file = Path(store.get_artifact("crash-artifact")["path"])
        marker = (
            self.root / "staging" / context.transaction_id / "metadata" / "commit.json"
        )
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["status"] = "PREPARED"
        self._mark_owner_dead(payload)
        marker.write_text(json.dumps(payload), encoding="utf-8")

        restarted = ExecutionWorker(
            store, self.root / "staging", self.root / "artifacts"
        )
        result = restarted.commit_manager.recover_pending(
            self.root / "staging", orchestrator_state=lambda _: "open"
        )
        self.assertEqual(list(result.recovered), [context.transaction_id])
        self.assertEqual(store.get_transaction_status(context.transaction_id), "ROLLED_BACK")
        self.assertEqual(store.list(), [])
        self.assertEqual(
            [event["event_type"] for event in store.query(
                transaction_id=context.transaction_id
            )],
            [
                "candidate_registered",
                "execution_transaction_committed",
                "execution_transaction_compensation_started",
                "execution_transaction_rolled_back",
            ],
        )
        self.assertIsNone(store.get_artifact("crash-artifact"))
        self.assertFalse(committed_file.exists())
        self.assertEqual(
            store.get_state("p1"), {"candidate_count": 0, "phase": "iterate"}
        )
        recovered_marker = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(
            recovered_marker["recovery_state"],
            "DB_COMMITTED_AWAITING_ORCHESTRATOR",
        )
        self.assertEqual(recovered_marker["status"], "ROLLED_BACK")

    def test_recovery_keeps_commit_when_matching_orchestrator_attempt_closed(self):
        store = SQLiteStore(self.root / "closed-window.db", project_id="p1")
        worker = ExecutionWorker(store, self.root / "staging", self.root / "artifacts")
        context = self._transaction()
        worker.run(
            context,
            lambda *_: ExecutionActionResult(state_updates={"formal": True}),
            validator=_validate_action_result,
        )
        marker = (
            self.root / "staging" / context.transaction_id / "metadata" / "commit.json"
        )
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["status"] = "PREPARED"
        self._mark_owner_dead(payload)
        marker.write_text(json.dumps(payload), encoding="utf-8")

        recovery = RecoveryManager(store)
        result = recovery.recover_pending(
            self.root / "staging", orchestrator_state=lambda _: "closed"
        )
        self.assertEqual(list(result.recovered), [context.transaction_id])
        self.assertTrue(store.get_state("p1")["formal"])
        self.assertEqual(
            json.loads(marker.read_text(encoding="utf-8"))["status"],
            "ORCHESTRATOR_CLOSED",
        )

    def test_recovery_skips_transaction_with_live_owner_heartbeat(self):
        """A COMMITTED transaction whose owner heartbeats fresh is never rolled back."""
        store = SQLiteStore(self.root / "live-owner.db", project_id="p1")
        store.replace_state("p1", {"candidate_count": 0})
        worker = ExecutionWorker(store, self.root / "staging", self.root / "artifacts")
        context = self._transaction()
        source = self.root / "live-artifact.txt"
        source.write_text("live", encoding="utf-8")

        def handler(_context, staging):
            artifact = staging.stage_artifact(
                source, artifact_id="live-artifact", artifact_type="text"
            )
            return ExecutionActionResult(
                candidate_updates=({"candidate_id": "C0001", "sequence": "AAAA"},),
                state_updates={"phase": "critic"},
                artifacts=(artifact,),
            )

        worker.run(context, handler, validator=_validate_action_result)
        committed_file = Path(store.get_artifact("live-artifact")["path"])
        marker = (
            self.root / "staging" / context.transaction_id / "metadata" / "commit.json"
        )
        payload = json.loads(marker.read_text(encoding="utf-8"))
        # Owner is still in the commit->closure window with a durable lease.
        for field in (
            "heartbeat_at",
            "owner_pid",
            "owner_host",
            "owner_instance_id",
            "owner_process_identity",
        ):
            self.assertIn(field, payload)

        # Another worker starts and scans while owner A is still alive.
        other = RecoveryManager(store)
        result = other.recover_pending(
            self.root / "staging", orchestrator_state=lambda _: "open"
        )
        self.assertEqual(list(result.skipped_active), [context.transaction_id])
        self.assertEqual(list(result.recovered), [])
        # Nothing was rolled back.
        self.assertEqual(
            store.get_transaction_status(context.transaction_id), "COMMITTED"
        )
        self.assertIsNotNone(store.get("C0001"))
        self.assertTrue(committed_file.exists())
        self.assertEqual(store.get_state("p1")["phase"], "critic")

    def test_recovery_unknown_orchestrator_state_never_compensates(self):
        """UNKNOWN closure verdict must surface as unresolved, never compensate."""
        store = SQLiteStore(self.root / "unknown-state.db", project_id="p1")
        store.replace_state("p1", {"candidate_count": 0})
        worker = ExecutionWorker(store, self.root / "staging", self.root / "artifacts")
        context = self._transaction()
        worker.run(
            context,
            lambda *_: ExecutionActionResult(state_updates={"formal": True}),
            validator=_validate_action_result,
        )
        marker = (
            self.root / "staging" / context.transaction_id / "metadata" / "commit.json"
        )
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["status"] = "PREPARED"
        self._mark_owner_dead(payload)
        marker.write_text(json.dumps(payload), encoding="utf-8")

        recovery = RecoveryManager(store)
        result = recovery.recover_pending(
            self.root / "staging", orchestrator_state=lambda _: "unknown"
        )
        self.assertEqual(list(result.recovered), [])
        self.assertEqual(list(result.unresolved), [context.transaction_id])
        self.assertFalse(result.clean)
        # Formal state untouched.
        self.assertTrue(store.get_state("p1")["formal"])
        self.assertEqual(
            store.get_transaction_status(context.transaction_id), "COMMITTED"
        )
        self.assertEqual(
            json.loads(marker.read_text(encoding="utf-8"))["status"],
            "RECOVERY_UNRESOLVED",
        )

    def test_execute_task_refuses_to_start_on_unresolved_recovery(self):
        """fail-closed: a new task must not start over unresolved recovery state."""
        from execution.worker import RecoveryError

        store = SQLiteStore(self.root / "fail-closed.db", project_id="p1")
        store.replace_state("p1", {"candidate_count": 0})
        # Seed an unresolved marker: compensation conflict left over.
        worker = ExecutionWorker(store, self.root / "staging", self.root / "artifacts")
        context = self._transaction()
        worker.run(
            context,
            lambda *_: ExecutionActionResult(state_updates={"shared": "from-a"}),
            validator=_validate_action_result,
        )
        store.update_state("p1", {"shared": "from-b"})
        marker = (
            self.root / "staging" / context.transaction_id / "metadata" / "commit.json"
        )
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["status"] = "COMPENSATION_FAILED"
        marker.write_text(json.dumps(payload), encoding="utf-8")

        # Directly exercise the fail-closed gate used by execute_task().
        from execution.worker import _assert_recovery_clean

        gate_worker = ExecutionWorker(store, self.root / "staging", self.root / "artifacts")
        result = gate_worker.commit_manager.recover_pending(self.root / "staging")
        self.assertFalse(result.clean)
        with self.assertRaises(RecoveryError) as raised:
            _assert_recovery_clean(result)
        self.assertIn(context.transaction_id, raised.exception.unresolved)

    def test_database_commit_failure_rolls_back_artifacts_and_evidence(self):
        store = data_layer.get_storage_backend()
        store.upsert({"candidate_id": "C0001", "sequence": "EXISTING"})
        source = self.root / "duplicate" / "shared.pdb"
        source.parent.mkdir()
        source.write_text("MODEL", encoding="utf-8")
        context = self._transaction()
        worker = ExecutionWorker(
            store, self.root / "staging", self.root / "formal_artifacts"
        )

        def handler(_context, staging):
            artifact = staging.stage_artifact(
                source, artifact_id="duplicate-artifact", artifact_type="design_pdb"
            )
            return ExecutionActionResult(
                candidate_updates=({"candidate_id": "C0001", "sequence": "NEW"},),
                artifacts=(artifact,),
            )

        with self.assertRaises(ValueError):
            worker.run(context, handler, validator=_validate_action_result)
        self.assertEqual(store.get("C0001")["sequence"], "EXISTING")
        self.assertIsNone(store.get_artifact("duplicate-artifact"))
        self.assertFalse(any((self.root / "formal_artifacts").rglob("shared.pdb")))
        self.assertFalse(store.query(event_type="candidate_registered"))

    def test_recovery_removes_uncommitted_prepared_artifact(self):
        store = data_layer.get_storage_backend()
        destination = self.root / "formal_artifacts" / "orphan.pdb"
        destination.parent.mkdir(parents=True)
        destination.write_text("ORPHAN", encoding="utf-8")
        temporary = destination.with_suffix(".tmp")
        temporary.write_text("TEMP", encoding="utf-8")
        marker = self.root / "staging" / "tx-recovery" / "metadata" / "commit.json"
        marker.parent.mkdir(parents=True)
        payload = {
            "transaction_id": "tx-recovery",
            "status": "PREPARED",
            "artifacts": [{
                "artifact_id": "orphan-artifact",
                "path": str(destination),
                "temporary": str(temporary),
            }],
        }
        self._mark_owner_dead(payload)
        marker.write_text(json.dumps(payload), encoding="utf-8")
        malformed = self.root / "staging" / "tx-malformed" / "metadata" / "commit.json"
        malformed.parent.mkdir(parents=True)
        malformed.write_text('{"transaction_id":', encoding="utf-8")
        recovery = RecoveryManager(store)
        result = recovery.recover_pending(self.root / "staging")
        self.assertEqual(list(result.recovered), ["tx-recovery"])
        self.assertEqual(len(recovery.marker_errors), 1)
        self.assertEqual(recovery.marker_errors[0]["code"], "JSONDecodeError")
        self.assertFalse(result.clean)
        self.assertFalse(destination.exists())
        self.assertFalse(temporary.exists())

    def test_same_basename_artifacts_are_isolated_by_artifact_id(self):
        staging = StagingArea(self.root / "staging", "tx-basename").create()
        first = self.root / "a" / "shared.pdb"
        second = self.root / "b" / "shared.pdb"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_text("FIRST", encoding="utf-8")
        second.write_text("SECOND", encoding="utf-8")
        one = staging.stage_artifact(first, artifact_id="candidate-one", artifact_type="pdb")
        two = staging.stage_artifact(second, artifact_id="candidate-two", artifact_type="pdb")
        self.assertNotEqual(one.staged_path, two.staged_path)
        self.assertEqual(Path(one.staged_path).read_text(encoding="utf-8"), "FIRST")
        self.assertEqual(Path(two.staged_path).read_text(encoding="utf-8"), "SECOND")

    def test_stage_artifact_rejects_dotdot_artifact_id(self):
        # P2-3: "." / ".." would escape the artifacts subdirectory; they must
        # be rejected even though no current caller produces them.
        staging = StagingArea(self.root / "staging", "tx-dotdot").create()
        source = self.root / "payload.txt"
        source.write_text("PAYLOAD", encoding="utf-8")
        for bad_id in (".", ".."):
            with self.assertRaises(ValueError):
                staging.stage_artifact(source, artifact_id=bad_id, artifact_type="pdb")
        self.assertFalse(
            (self.root / "staging" / "tx-dotdot" / "payload.txt").exists()
        )

    def test_zero_candidate_batch_is_valid(self):
        batch = CandidateUpdateBatch(
            schema_version=CANDIDATE_UPDATE_SCHEMA_VERSION,
            emitter="design",
            job_id="job-zero",
            candidate_updates=(),
        )
        restored = CandidateUpdateBatch.from_dict(batch.to_dict())
        self.assertEqual(restored.candidate_updates, ())

    def test_multi_job_iterate_design_keeps_all_updates(self):
        config = self._config()
        project = State.load()["project_config"]
        params = {
            "project_config_digest": object_sha256(project),
            "design_jobs": [
                {"route": "A", "target_id": "MDM2", "proposal_count": 2, "lengths": [8], "seed": 1},
                {"route": "B", "target_id": "MDMX", "proposal_count": 1, "lengths": [10], "seed": 2},
            ],
        }
        packet = {
            "run_id": "run-integrity",
            "task": {
                "task_id": "T001",
                "action": "iterate_design",
                "phase": "design",
                "resource_request": {"candidate_limit": 3, "class": "gpu"},
            },
        }
        task_dir = config.task_dir("run-integrity", "T001", 1)
        task_dir.mkdir(parents=True)
        calls = 0

        def fake_process(argv, **_kwargs):
            nonlocal calls
            calls += 1
            updates_path = Path(argv[argv.index("--candidate-updates-path") + 1])
            count = 2 if calls == 1 else 1
            updates = []
            for offset in range(count):
                candidate_id = data_layer.allocate_candidate_id()
                candidate_dir = self.root / f"job-{calls}" / candidate_id
                candidate_dir.mkdir(parents=True)
                pdb = candidate_dir / "shared.pdb"
                manifest = candidate_dir / "manifest.json"
                pdb.write_text(candidate_id, encoding="utf-8")
                manifest.write_text(json.dumps({"candidate_id": candidate_id}), encoding="utf-8")
                updates.append(CandidateUpdate({
                    "candidate_id": candidate_id,
                    "sequence": "A" * (8 + offset),
                    "source_route": f"route-{calls}",
                    "source_batch": f"batch-{calls}",
                    "manifest_path": str(manifest),
                    "design_pdb_path": str(pdb),
                }))
            updates_path.parent.mkdir(parents=True, exist_ok=True)
            updates_path.write_text(json.dumps(CandidateUpdateBatch(
                schema_version=CANDIDATE_UPDATE_SCHEMA_VERSION,
                emitter="design",
                job_id=f"job-{calls}",
                candidate_updates=tuple(updates),
            ).to_dict()), encoding="utf-8")
            return {"elapsed_seconds": 0.01, "exit_code": 0}

        context = self._transaction()
        worker = ExecutionWorker(
            data_layer.get_storage_backend(),
            self.root / "staging",
            self.root / "formal_artifacts",
        )
        with patch("execution.handlers.validate_task_parameters", return_value=params), patch(
            "execution.handlers.run_process", side_effect=fake_process
        ):
            result = worker.run(
                context,
                adapter_for(
                    "iterate_design",
                    lambda _value: None,
                    packet,
                    config,
                    task_dir,
                    project,
                ),
                validator=_validate_action_result,
            )
        self.assertEqual(len(result.candidate_updates), 3)
        self.assertEqual(len(CandidateIndex.load()), 3)
        design_result = json.loads(Path(result.outputs[0][1]).read_text(encoding="utf-8"))
        _validate_design_result(design_result, packet["task"])
        self.assertEqual(calls, 2)
        self.assertEqual(
            len(list((self.root / "formal_artifacts").rglob("shared.pdb"))), 3
        )
        self.assertEqual(context.action, "iterate_design")


if __name__ == "__main__":
    unittest.main()
