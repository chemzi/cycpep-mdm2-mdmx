"""Ownership and append-only invariants for SQLite transaction compensation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from storage import SQLiteStore


class StoreTransactionOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="store-transaction-ownership-"))
        self.store = SQLiteStore(self.root / "store.db", project_id="p1")

    @staticmethod
    def _context(number: int, *, status: str = "COMMITTING") -> dict:
        return {
            "transaction_id": f"tx-{number}",
            "workflow_id": "workflow-store",
            "run_id": "run-store",
            "task_id": f"T{number:03d}",
            "attempt_id": f"T{number:03d}-A01",
            "action": "review_prediction_handoff",
            "status": status,
            "metadata": {"project_id": "p1"},
        }

    def _commit(
        self,
        number: int,
        *,
        candidate_updates=(),
        candidate_patches=(),
        state_updates=None,
        state_appends=(),
        evidence_events=(),
    ) -> dict:
        context = self._context(number)
        self.store.commit_transaction(
            context=context,
            candidate_updates=candidate_updates,
            candidate_patches=candidate_patches,
            state_updates=state_updates or {},
            state_appends=state_appends,
            artifacts=(),
            evidence_events=evidence_events,
        )
        return context

    def _state_version(self, key: str) -> tuple[int, str | None]:
        with self.store._read() as connection:
            row = connection.execute(
                "SELECT revision, last_writer_transaction_id FROM state_key_versions "
                "WHERE project_id = 'p1' AND key = ?",
                (key,),
            ).fetchone()
        return int(row["revision"]), row["last_writer_transaction_id"]

    def _candidate_version(self, candidate_id: str) -> tuple[int, str | None]:
        with self.store._read() as connection:
            row = connection.execute(
                "SELECT revision, last_writer_transaction_id FROM candidate_versions "
                "WHERE project_id = 'p1' AND candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        return int(row["revision"]), row["last_writer_transaction_id"]

    def _transaction_payload(self, transaction_id: str) -> dict:
        with self.store._read() as connection:
            row = connection.execute(
                "SELECT payload_json FROM execution_transactions WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()
        return json.loads(row["payload_json"])

    def test_same_value_state_writer_blocks_aba_rollback(self):
        self.store.replace_state("p1", {"phase": "iterate"})
        first = self._commit(1, state_updates={"phase": "critic"})
        effect = self._transaction_payload("tx-1")["state_effects"][0]
        self.assertEqual(effect["after_writer_transaction_id"], "tx-1")
        self.assertEqual(effect["after_revision"], effect["before_revision"] + 1)
        second = self._commit(2, state_updates={"phase": "critic"})

        conflicts = self.store.rollback_transaction(first["transaction_id"])

        self.assertEqual(self.store.get_state("p1")["phase"], "critic")
        self.assertEqual(self.store.get_transaction_status(first["transaction_id"]), "COMPENSATION_CONFLICT")
        self.assertEqual(self._state_version("phase")[1], second["transaction_id"])
        self.assertEqual(conflicts[0]["key"], "phase")

    def test_unrelated_later_state_write_does_not_create_false_conflict(self):
        self.store.replace_state("p1", {"base": True})
        first = self._commit(1, state_updates={"from_a": 1})
        self._commit(2, state_updates={"from_b": 2})

        self.assertEqual(self.store.rollback_transaction(first["transaction_id"]), [])
        self.assertEqual(self.store.get_state("p1"), {"base": True, "from_b": 2})
        self.assertEqual(self.store.get_transaction_status(first["transaction_id"]), "ROLLED_BACK")

    def test_candidate_count_same_value_writer_blocks_older_rollback(self):
        self.store.replace_state("p1", {"phase": "design", "candidate_count": 0})
        first = self._commit(
            1,
            candidate_updates=({"candidate_id": "C0002", "sequence": "AAAA"},),
        )
        second = self._commit(
            2,
            candidate_updates=({"candidate_id": "C0001", "sequence": "BBBB"},),
        )

        conflicts = self.store.rollback_transaction(first["transaction_id"])

        self.assertEqual(self.store.get_state("p1")["candidate_count"], 2)
        self.assertEqual(self._state_version("candidate_count")[1], second["transaction_id"])
        self.assertIn("candidate_count", {item.get("key") for item in conflicts})
        self.assertIsNotNone(self.store.get("C0002"))

    def test_candidate_insert_initializes_count_and_revision_from_empty_state(self):
        transaction = self._commit(
            1,
            candidate_updates=({"candidate_id": "C0003", "sequence": "AAAA"},),
        )

        self.assertEqual(self.store.get_state("p1"), {"candidate_count": 3})
        self.assertEqual(
            self._state_version("candidate_count"), (1, transaction["transaction_id"])
        )
        self.assertEqual(self.store.rollback_transaction(transaction["transaction_id"]), [])
        self.assertEqual(self.store.get_state("p1"), {})

    def test_candidate_patch_deep_merges_metrics_and_rollback_is_owned(self):
        self.store.upsert({
            "candidate_id": "C0001",
            "sequence": "AAAA",
            "final_status": "pending",
            "metrics_json": json.dumps({"targets": {"MDM2": {"ipsae": 0.4}}}),
        })
        first = self._commit(1, candidate_patches=({
            "candidate_id": "C0001",
            "patch": {
                "final_status": "evaluated",
                "metrics": {"targets": {"MDM2": {"dg": -8.0}}},
            },
        },))
        effect = self._transaction_payload("tx-1")["candidate_effects"][0]
        self.assertEqual(effect["after_writer_transaction_id"], "tx-1")
        self.assertEqual(effect["after_revision"], effect["before_revision"] + 1)
        metrics = json.loads(self.store.get("C0001")["metrics_json"])
        self.assertEqual(metrics["targets"]["MDM2"], {"ipsae": 0.4, "dg": -8.0})

        self._commit(2, candidate_patches=({
            "candidate_id": "C0001",
            "patch": {"final_status": "evaluated"},
        },))
        conflicts = self.store.rollback_transaction(first["transaction_id"])

        self.assertEqual(conflicts[0]["candidate_id"], "C0001")
        self.assertEqual(self.store.get("C0001")["final_status"], "evaluated")
        self.assertEqual(self._candidate_version("C0001")[1], "tx-2")

    def test_successful_candidate_patch_rollback_restores_before_row(self):
        original_metrics = json.dumps({"global": {"quality": 0.5}})
        self.store.upsert({
            "candidate_id": "C0001",
            "sequence": "AAAA",
            "final_status": "pending",
            "metrics_json": original_metrics,
        })
        transaction = self._commit(1, candidate_patches=({
            "candidate_id": "C0001",
            "patch": {
                "final_status": "evaluated",
                "metrics": {"global": {"confidence": 0.9}},
            },
        },))

        self.assertEqual(self.store.rollback_transaction(transaction["transaction_id"]), [])
        restored = self.store.get("C0001")
        self.assertEqual(restored["final_status"], "pending")
        self.assertEqual(restored["metrics_json"], original_metrics)

    def test_rollback_evidence_is_append_only_and_queryable_by_transaction(self):
        transaction = self._commit(1, state_updates={"reviewed": True}, evidence_events=({
            "event_id": "critic-review-1",
            "timestamp": "2026-08-07T00:00:00+00:00",
            "project_id": "p1",
            "workflow_id": "workflow-store",
            "run_id": "run-store",
            "task_id": "T001",
            "attempt_id": "T001-A01",
            "agent": "critic",
            "event_type": "critic_review",
            "phase": "critic",
            "summary": "reviewed",
        },))
        before = self.store.query(transaction_id=transaction["transaction_id"])

        self.store.rollback_transaction(transaction["transaction_id"])
        after = self.store.query(transaction_id=transaction["transaction_id"])

        self.assertEqual(after[:len(before)], before)
        self.assertEqual(
            [event["event_type"] for event in after],
            [
                "critic_review",
                "execution_transaction_committed",
                "execution_transaction_compensation_started",
                "execution_transaction_rolled_back",
            ],
        )

    def test_transaction_rejects_invalid_evidence_before_any_write(self):
        context = self._context(1)
        with self.assertRaisesRegex(ValueError, "unknown evidence agent"):
            self.store.commit_transaction(
                context=context,
                candidate_updates=({"candidate_id": "C0001", "sequence": "AAAA"},),
                candidate_patches=(),
                state_updates={"reviewed": True},
                state_appends=(),
                artifacts=(),
                evidence_events=({
                    "event_id": "invalid-event",
                    "timestamp": "2026-08-07T00:00:00+00:00",
                    "agent": "bogus",
                    "event_type": "critic_review",
                },),
            )

        self.assertIsNone(self.store.get("C0001"))
        self.assertEqual(self.store.get_state("p1"), {})
        self.assertIsNone(self.store.get_transaction_status("tx-1"))
        self.assertEqual(self.store.query(), [])

    def test_record_failure_never_downgrades_terminal_transaction(self):
        committed = self._commit(1, state_updates={"committed": True})
        rolled_back = self._commit(2, state_updates={"temporary": True})
        self.store.rollback_transaction(rolled_back["transaction_id"])
        conflicted = self._commit(3, state_updates={"owned": "first"})
        self._commit(4, state_updates={"owned": "later"})
        self.store.rollback_transaction(conflicted["transaction_id"])

        error = {
            "code": "post_commit_error",
            "message": "bookkeeping failed",
            "component": "test",
            "retryable": False,
        }
        for context, expected in (
            (committed, "COMMITTED"),
            (rolled_back, "ROLLED_BACK"),
            (conflicted, "COMPENSATION_CONFLICT"),
        ):
            self.store.record_task_failure(context=context, error=error)
            self.assertEqual(
                self.store.get_transaction_status(context["transaction_id"]), expected
            )
            self.assertEqual(
                self.store.query(transaction_id=context["transaction_id"])[-1]["event_type"],
                "execution_transaction_post_commit_failure",
            )

    def test_no_row_rolled_back_failure_is_persisted_as_rolled_back(self):
        context = self._context(1, status="ROLLED_BACK")
        self.store.record_task_failure(context=context, error={
            "code": "commit_error",
            "message": "commit did not publish",
            "component": "test",
            "retryable": False,
        })
        self.assertEqual(self.store.get_transaction_status("tx-1"), "ROLLED_BACK")

    def test_non_transaction_writes_advance_revisions_even_for_same_values(self):
        self.store.replace_state("p1", {"phase": "critic", "items": []})
        phase_revision, _ = self._state_version("phase")
        self.store.update_state("p1", {"phase": "critic"})
        self.assertEqual(self._state_version("phase"), (phase_revision + 1, None))

        self.store.append_state_item_if_absent(
            "p1", "items", {"id": "one"}, identity_path=("id",), identity_value="one"
        )
        applied_revision, _ = self._state_version("items")
        self.store.append_state_item_if_absent(
            "p1", "items", {"id": "one"}, identity_path=("id",), identity_value="one"
        )
        self.assertEqual(self._state_version("items"), (applied_revision, None))

        self.store.upsert({"candidate_id": "C0001", "sequence": "AAAA"})
        candidate_revision, _ = self._candidate_version("C0001")
        self.store.upsert({"candidate_id": "C0001", "sequence": "AAAA"})
        self.assertEqual(
            self._candidate_version("C0001"), (candidate_revision + 1, None)
        )


if __name__ == "__main__":
    unittest.main()
