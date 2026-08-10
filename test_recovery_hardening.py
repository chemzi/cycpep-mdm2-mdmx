"""Focused regressions for execution owner leases and ambiguous commits."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from contracts.transaction import TransactionContext, TransactionStatus
from execution.commit_manager import CommitManager
from execution.recovery import RecoveryManager, owner_lease
from execution.staging import StagingArea


class _LeaseStore:
    def __init__(self) -> None:
        self.statuses: dict[str, str] = {}
        self.artifacts: dict[str, dict] = {}
        self.rollback_calls: list[str] = []
        self.rollback_conflicts: list[dict] = []
        self.commit_entered = threading.Event()
        self.commit_release = threading.Event()
        self.block_commit = False
        self.commit_then_raise = False

    def commit_transaction(
        self,
        *,
        context,
        candidate_updates,
        candidate_patches=(),
        state_updates,
        state_appends,
        artifacts,
        evidence_events=(),
    ):
        del candidate_updates, candidate_patches, state_updates
        del state_appends, evidence_events
        transaction_id = str(context["transaction_id"])
        self.commit_entered.set()
        if self.block_commit:
            if not self.commit_release.wait(timeout=10):
                raise TimeoutError("test commit barrier timed out")
        self.statuses[transaction_id] = "COMMITTED"
        for artifact in artifacts:
            self.artifacts[str(artifact["artifact_id"])] = dict(artifact)
        if self.commit_then_raise:
            raise RuntimeError("connection dropped after database commit")
        return ["event-1"]

    def get_transaction_status(self, transaction_id):
        return self.statuses.get(transaction_id)

    def get_artifact(self, artifact_id):
        return self.artifacts.get(artifact_id)

    def rollback_transaction(self, transaction_id):
        self.rollback_calls.append(transaction_id)
        if self.rollback_conflicts:
            self.statuses[transaction_id] = "COMPENSATION_CONFLICT"
            return list(self.rollback_conflicts)
        self.statuses[transaction_id] = "ROLLED_BACK"
        self.artifacts.clear()
        return []


class RecoveryHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="recovery-hardening-")
        self.root = Path(self.temporary.name)
        self.staging_root = self.root / "staging"
        self.artifact_root = self.root / "artifacts"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _context(suffix: str) -> TransactionContext:
        context = TransactionContext.create(
            workflow_id="workflow-recovery",
            run_id="run-recovery",
            task_id=f"T-{suffix}",
            attempt_id=f"attempt-{suffix}",
            action="iterate_design",
            metadata={"worker_id": f"worker-{suffix}"},
        )
        context.transition(TransactionStatus.STAGING)
        context.transition(TransactionStatus.VALIDATING)
        return context

    def _staged_artifact(self, context: TransactionContext, artifact_id: str):
        source = self.root / f"{artifact_id}.txt"
        source.write_text(f"contents-{artifact_id}", encoding="utf-8")
        staging = StagingArea(self.staging_root, context.transaction_id).create()
        artifact = staging.stage_artifact(
            source, artifact_id=artifact_id, artifact_type="text"
        )
        return staging, artifact

    def _marker_path(self, transaction_id: str) -> Path:
        return self.staging_root / transaction_id / "metadata" / "commit.json"

    def _write_marker(self, transaction_id: str, payload: dict) -> Path:
        marker = self._marker_path(transaction_id)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(payload), encoding="utf-8")
        return marker

    def _dead_owner_marker(self, transaction_id: str, status: str, path: Path) -> dict:
        lease = owner_lease(worker_id="dead-worker", instance_id="dead-instance")
        lease["owner_pid"] = max(10_000_000, os.getpid() + 10_000_000)
        lease["owner_process_identity"] = "dead-process"
        return {
            "transaction_id": transaction_id,
            "context": {"transaction_id": transaction_id},
            "status": status,
            "artifacts": [{"artifact_id": "artifact", "path": str(path)}],
            **lease,
        }

    def test_read_only_inspection_reports_pending_marker_without_mutation(self):
        store = _LeaseStore()
        transaction_id = "TX123"
        marker = self._write_marker(
            transaction_id,
            {
                "transaction_id": transaction_id,
                "status": "RECOVERY_UNRESOLVED",
                "compensation_error": {"code": "operator_required"},
            },
        )
        before = marker.read_bytes()

        result = RecoveryManager(store).inspect_pending(self.staging_root)

        self.assertFalse(result.clean)
        self.assertEqual(result.unresolved, (transaction_id,))
        self.assertEqual(result.marker_errors, ())
        self.assertEqual(marker.read_bytes(), before)
        self.assertEqual(store.rollback_calls, [])

    @unittest.skipUnless(
        sys.platform.startswith("linux"),
        "owner-liveness identity proof requires Linux /proc; "
        "other platforms degrade to safe OWNER_UNKNOWN",
    )
    def test_prepared_window_live_owner_survives_concurrent_recovery(self):
        store = _LeaseStore()
        store.block_commit = True
        context = self._context("prepared")
        staging, artifact = self._staged_artifact(context, "prepared-artifact")
        manager = CommitManager(store, self.artifact_root)
        failures: list[BaseException] = []

        def commit_in_owner_thread() -> None:
            try:
                manager.commit(context, artifacts=(artifact,), staging_path=staging.path)
            except BaseException as exc:
                failures.append(exc)

        owner = threading.Thread(target=commit_in_owner_thread)
        owner.start()
        self.assertTrue(store.commit_entered.wait(timeout=10))
        marker = self._marker_path(context.transaction_id)
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["heartbeat_at"] = "2000-01-01T00:00:00+00:00"
        marker.write_text(json.dumps(payload), encoding="utf-8")
        destination = Path(payload["artifacts"][0]["path"])

        result = RecoveryManager(store, stall_seconds=0.001).recover_pending(
            self.staging_root,
            orchestrator_state=lambda _: "open",
            now=datetime.now(timezone.utc),
        )
        self.assertEqual(result.skipped_active, (context.transaction_id,))
        self.assertTrue(destination.is_file())
        self.assertEqual(store.rollback_calls, [])

        store.commit_release.set()
        owner.join(timeout=10)
        self.assertFalse(owner.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(store.statuses[context.transaction_id], "COMMITTED")

    @unittest.skipUnless(
        sys.platform.startswith("linux"),
        "owner-liveness identity proof requires Linux /proc; "
        "other platforms degrade to safe OWNER_UNKNOWN",
    )
    def test_committed_preclosure_live_owner_ignores_stale_heartbeat(self):
        store = _LeaseStore()
        context = self._context("committed")
        staging, artifact = self._staged_artifact(context, "committed-artifact")
        manager = CommitManager(store, self.artifact_root)
        owner_waiting = threading.Event()
        owner_release = threading.Event()

        def owner_thread() -> None:
            manager.commit(context, artifacts=(artifact,), staging_path=staging.path)
            owner_waiting.set()
            owner_release.wait(timeout=10)

        owner = threading.Thread(target=owner_thread)
        owner.start()
        self.assertTrue(owner_waiting.wait(timeout=10))
        marker = self._marker_path(context.transaction_id)
        payload = json.loads(marker.read_text(encoding="utf-8"))
        expected_fields = {
            "owner_worker_id",
            "owner_pid",
            "owner_host",
            "owner_process_identity",
            "owner_instance_id",
            "owner_boot_id",
            "owner_session_id",
            "heartbeat_at",
        }
        self.assertTrue(expected_fields.issubset(payload))
        self.assertNotIn("heartbeat_monotonic", payload)
        payload["heartbeat_at"] = "2000-01-01T00:00:00+00:00"
        marker.write_text(json.dumps(payload), encoding="utf-8")

        result = RecoveryManager(store, stall_seconds=0.001).recover_pending(
            self.staging_root, orchestrator_state=lambda _: "open"
        )
        self.assertEqual(result.skipped_active, (context.transaction_id,))
        self.assertEqual(store.statuses[context.transaction_id], "COMMITTED")
        self.assertTrue(Path(payload["artifacts"][0]["path"]).is_file())
        self.assertEqual(store.rollback_calls, [])
        owner_release.set()
        owner.join(timeout=10)

    @unittest.skipUnless(
        sys.platform.startswith("linux"),
        "owner-liveness identity proof requires Linux /proc; "
        "other platforms degrade to safe OWNER_UNKNOWN",
    )
    def test_dead_owner_and_rebooted_owner_are_recoverable(self):
        for suffix, mutate_lease in (
            ("dead", lambda payload: None),
            ("reboot", lambda payload: payload.__setitem__("owner_boot_id", "old-boot")),
        ):
            with self.subTest(suffix=suffix):
                store = _LeaseStore()
                transaction_id = f"tx-{suffix}"
                destination = self.root / f"{suffix}.txt"
                destination.write_text("orphan", encoding="utf-8")
                payload = self._dead_owner_marker(
                    transaction_id, "PREPARED", destination
                )
                if suffix == "reboot":
                    live = owner_lease(worker_id="worker", instance_id="instance")
                    payload.update(live)
                    mutate_lease(payload)
                self._write_marker(transaction_id, payload)

                result = RecoveryManager(store).recover_pending(self.staging_root)
                self.assertIn(transaction_id, result.recovered)
                self.assertFalse(destination.exists())

    def test_remote_stale_owner_is_unresolved_not_destructively_recovered(self):
        for status, database_status in (("PREPARED", None), ("COMMITTED", "COMMITTED")):
            with self.subTest(status=status):
                store = _LeaseStore()
                transaction_id = f"tx-remote-{status.lower()}"
                if database_status is not None:
                    store.statuses[transaction_id] = database_status
                destination = self.root / f"remote-{status.lower()}.txt"
                destination.write_text("formal-or-inflight", encoding="utf-8")
                payload = self._dead_owner_marker(transaction_id, status, destination)
                payload["owner_host"] = "different-worker-host.invalid"
                payload["heartbeat_at"] = "2000-01-01T00:00:00+00:00"
                case_staging_root = self.staging_root / status.lower()
                marker = case_staging_root / transaction_id / "metadata" / "commit.json"
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(json.dumps(payload), encoding="utf-8")

                result = RecoveryManager(store, stall_seconds=0.001).recover_pending(
                    case_staging_root,
                    orchestrator_state=lambda _: "open",
                    now=datetime.now(timezone.utc),
                )

                self.assertFalse(result.clean)
                self.assertEqual(result.unresolved, (transaction_id,))
                self.assertTrue(destination.is_file())
                self.assertEqual(store.rollback_calls, [])
                self.assertEqual(
                    json.loads(marker.read_text(encoding="utf-8"))["status"],
                    "RECOVERY_UNRESOLVED",
                )

    def test_unverifiable_local_process_identity_is_fail_closed(self):
        store = _LeaseStore()
        transaction_id = "tx-local-identity-unknown"
        destination = self.root / "identity-unknown.txt"
        destination.write_text("inflight", encoding="utf-8")
        payload = self._dead_owner_marker(transaction_id, "PREPARED", destination)
        payload.update(owner_lease(worker_id="worker", instance_id="instance"))
        payload["heartbeat_at"] = "2000-01-01T00:00:00+00:00"
        marker = self._write_marker(transaction_id, payload)

        with patch("execution.recovery._process_exists", return_value=True), patch(
            "execution.recovery._process_identity", return_value=None
        ):
            result = RecoveryManager(store, stall_seconds=0.001).recover_pending(
                self.staging_root,
                orchestrator_state=lambda _: "open",
                now=datetime.now(timezone.utc),
            )

        self.assertFalse(result.clean)
        self.assertEqual(result.unresolved, (transaction_id,))
        self.assertTrue(destination.is_file())
        self.assertEqual(store.rollback_calls, [])
        self.assertEqual(
            json.loads(marker.read_text(encoding="utf-8"))["status"],
            "RECOVERY_UNRESOLVED",
        )

    def test_unknown_remote_owner_can_still_be_closed_non_destructively(self):
        store = _LeaseStore()
        transaction_id = "tx-remote-closed"
        store.statuses[transaction_id] = "COMMITTED"
        destination = self.root / "remote-closed.txt"
        destination.write_text("formal", encoding="utf-8")
        payload = self._dead_owner_marker(transaction_id, "COMMITTED", destination)
        payload["owner_host"] = "different-worker-host.invalid"
        payload["heartbeat_at"] = "2000-01-01T00:00:00+00:00"
        marker = self._write_marker(transaction_id, payload)

        result = RecoveryManager(store, stall_seconds=0.001).recover_pending(
            self.staging_root,
            orchestrator_state=lambda _: "closed",
            now=datetime.now(timezone.utc),
        )

        self.assertTrue(result.clean)
        self.assertEqual(result.recovered, (transaction_id,))
        self.assertTrue(destination.is_file())
        self.assertEqual(store.rollback_calls, [])
        self.assertEqual(
            json.loads(marker.read_text(encoding="utf-8"))["status"],
            "ORCHESTRATOR_CLOSED",
        )

    def test_recovery_unresolved_remains_fail_closed_across_passes(self):
        store = _LeaseStore()
        transaction_id = "tx-unresolved"
        store.statuses[transaction_id] = "COMMITTED"
        destination = self.root / "unresolved.txt"
        destination.write_text("formal", encoding="utf-8")
        store.artifacts["artifact"] = {"artifact_id": "artifact", "path": str(destination)}
        marker = self._write_marker(
            transaction_id,
            self._dead_owner_marker(
                transaction_id, "RECOVERY_UNRESOLVED", destination
            ),
        )
        recovery = RecoveryManager(store)

        for _ in range(3):
            result = recovery.recover_pending(
                self.staging_root, orchestrator_state=lambda _: "unknown"
            )
            self.assertFalse(result.clean)
            self.assertEqual(result.unresolved, (transaction_id,))
            self.assertEqual(
                json.loads(marker.read_text(encoding="utf-8"))["status"],
                "RECOVERY_UNRESOLVED",
            )
            self.assertTrue(destination.exists())
            self.assertEqual(store.rollback_calls, [])

        closed = recovery.recover_pending(
            self.staging_root, orchestrator_state=lambda _: "closed"
        )
        self.assertTrue(closed.clean)
        self.assertEqual(closed.recovered, (transaction_id,))
        self.assertEqual(
            json.loads(marker.read_text(encoding="utf-8"))["status"],
            "ORCHESTRATOR_CLOSED",
        )

    def test_ambiguous_commit_preserves_formal_artifact_for_recovery(self):
        store = _LeaseStore()
        store.commit_then_raise = True
        context = self._context("ambiguous")
        staging, artifact = self._staged_artifact(context, "ambiguous-artifact")
        manager = CommitManager(store, self.artifact_root)

        with self.assertRaisesRegex(RuntimeError, "connection dropped"):
            manager.commit(context, artifacts=(artifact,), staging_path=staging.path)

        marker = self._marker_path(context.transaction_id)
        payload = json.loads(marker.read_text(encoding="utf-8"))
        destination = Path(payload["artifacts"][0]["path"])
        self.assertEqual(context.status, TransactionStatus.COMMITTED)
        self.assertEqual(payload["status"], "RECOVERY_UNRESOLVED")
        self.assertEqual(payload["database_status_after_commit_error"], "COMMITTED")
        self.assertTrue(destination.is_file())
        self.assertIsNotNone(store.get_artifact("ambiguous-artifact"))

    def test_compensation_conflict_marker_stays_unresolved(self):
        store = _LeaseStore()
        store.rollback_conflicts = [{"kind": "set", "key": "phase"}]
        transaction_id = "tx-conflict"
        store.statuses[transaction_id] = "COMMITTED"
        destination = self.root / "conflict.txt"
        destination.write_text("formal", encoding="utf-8")
        store.artifacts["artifact"] = {"artifact_id": "artifact", "path": str(destination)}
        marker = self._write_marker(
            transaction_id,
            self._dead_owner_marker(transaction_id, "COMMITTED", destination),
        )

        result = RecoveryManager(store).recover_pending(
            self.staging_root, orchestrator_state=lambda _: "open"
        )
        self.assertFalse(result.clean)
        self.assertEqual(result.unresolved, (transaction_id,))
        self.assertEqual(
            json.loads(marker.read_text(encoding="utf-8"))["status"],
            "COMPENSATION_CONFLICT",
        )
        self.assertTrue(destination.exists())

    def test_direct_rollback_conflict_uses_terminal_context_and_marker_state(self):
        store = _LeaseStore()
        context = self._context("direct-conflict")
        staging = StagingArea(self.staging_root, context.transaction_id).create()
        manager = CommitManager(store, self.artifact_root)
        manager.commit(context, staging_path=staging.path)
        store.rollback_conflicts = [{"kind": "set", "key": "phase"}]

        with self.assertRaisesRegex(RuntimeError, "compensation conflicts"):
            manager.rollback_committed(context, staging.path)

        marker = self._marker_path(context.transaction_id)
        self.assertEqual(context.status, TransactionStatus.COMPENSATION_CONFLICT)
        self.assertEqual(
            json.loads(marker.read_text(encoding="utf-8"))["status"],
            "COMPENSATION_CONFLICT",
        )


if __name__ == "__main__":
    unittest.main()
