import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agents.research as research
import agents.research_contract as research_contract
import data_layer
from data_layer import EvidenceLogger, get_storage_backend
from project_config import load_project_config


class ResearchLauncherContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.config = load_project_config(
            Path(__file__).parent / "projects" / "mdm2_mdmx.json"
        )
        self.correlation = research.ResearchCorrelation(
            research_invocation_id="research_11111111-1111-4111-8111-111111111111",
            launcher_run_id="launcher_11111111-1111-4111-8111-111111111111",
            project_id=self.config["project_id"],
            approved_content_binding=self.config["review"]["approved_digest"],
        )
        self._patches = [
            patch.object(data_layer, "ACTIVE_PROJECT_CONFIG", self.config),
            patch.object(data_layer, "SQLITE_DB_PATH", root / "store.db"),
            patch.object(data_layer, "DATA_DIR", root / "data"),
            patch.object(data_layer, "EVIDENCE_DIR", root / "evidence"),
            patch.object(data_layer, "STATE_PATH", root / "data" / "state.json"),
            patch.object(data_layer, "LOG_PATH", root / "evidence" / "evidence_log.jsonl"),
            patch.object(data_layer, "INDEX_PATH", root / "data" / "candidate_index.csv"),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self):
        for item in reversed(self._patches):
            item.stop()
        self._tmp.cleanup()

    def test_legacy_run_signature_and_result_are_unchanged(self):
        self.assertEqual(
            str(inspect.signature(research.run)),
            "(state=None, force_recompute=False, skip_pipeline=False, project_config=None)",
        )
        expected = {"scientific": "result"}
        with patch.object(research, "_run_impl", return_value=expected):
            actual = research.run(project_config=self.config)
        self.assertIs(actual, expected)

    def test_start_is_durable_before_existing_research_implementation(self):
        observed = {}

        def fake_impl(*, state, force_recompute, skip_pipeline, receipt_evidence_ids):
            starts = get_storage_backend().query(
                project_id=self.config["project_id"],
                agent="research",
                event_type="research_invocation_started",
            )
            observed["starts"] = starts
            receipt_evidence_ids.append(
                EvidenceLogger.log("research", "research_targets", {"formal": True})
            )
            return {"scientific": "result"}

        with patch.object(research, "_run_impl", side_effect=fake_impl):
            result = research.run_with_receipt(
                project_config=self.config,
                correlation=self.correlation,
            )

        self.assertEqual(len(observed["starts"]), 1)
        self.assertEqual(
            observed["starts"][0]["research_invocation_id"],
            self.correlation.research_invocation_id,
        )
        self.assertEqual(result.result, {"scientific": "result"})
        self.assertEqual(len(result.research_evidence_ids), 1)
        self.assertTrue(result.receipt_event_id)

    def test_start_persistence_failure_prevents_research_side_effects(self):
        invoked = []
        with patch.object(
            research_contract,
            "append_start_receipt",
            side_effect=OSError("store unavailable"),
        ), patch.object(research, "_run_impl", side_effect=lambda **_: invoked.append(True)):
            with self.assertRaises(OSError):
                research.run_with_receipt(
                    project_config=self.config,
                    correlation=self.correlation,
                )
        self.assertEqual(invoked, [])

    def test_validator_resolves_completed_invocation_from_formal_store(self):
        research_target_id = EvidenceLogger.log(
            "research", "research_targets", {"formal": True}
        )
        self._append_start()
        self._append_completion([research_target_id])

        first = research.validate_research_invocation(
            self.correlation, store=get_storage_backend()
        )
        second = research.validate_research_invocation(
            self.correlation, store=get_storage_backend()
        )

        self.assertEqual(first.status, "completed")
        self.assertEqual(first, second)
        self.assertEqual(first.research_evidence_ids, (research_target_id,))

    def test_validator_reports_not_started_only_when_no_start_exists(self):
        status = research.validate_research_invocation(
            self.correlation, store=get_storage_backend()
        )
        self.assertEqual(status.status, "not_started")
        self.assertIsNone(status.blocker_code)

    def test_validator_fails_closed_for_completion_without_start(self):
        research_target_id = EvidenceLogger.log(
            "research", "research_targets", {"formal": True}
        )
        self._append_completion([research_target_id])
        status = research.validate_research_invocation(
            self.correlation, store=get_storage_backend()
        )
        self.assertEqual(status.status, "conflicting")
        self.assertEqual(status.blocker_code, "research_correlation_conflict")

    def test_validator_fails_closed_for_start_without_completion(self):
        self._append_start()
        status = research.validate_research_invocation(
            self.correlation, store=get_storage_backend()
        )
        self.assertEqual(status.status, "started_without_completion")
        self.assertEqual(status.blocker_code, "research_completion_ambiguous")

    def test_validator_fails_closed_for_multiple_starts(self):
        self._append_start()
        self._append_start()
        status = research.validate_research_invocation(
            self.correlation, store=get_storage_backend()
        )
        self.assertEqual(status.status, "conflicting")
        self.assertEqual(status.blocker_code, "research_correlation_conflict")

    def test_validator_fails_closed_for_conflicting_launcher_binding(self):
        self._append_start()
        conflicting = research.ResearchCorrelation(
            research_invocation_id="research_22222222-2222-4222-8222-222222222222",
            launcher_run_id=self.correlation.launcher_run_id,
            project_id=self.correlation.project_id,
            approved_content_binding=self.correlation.approved_content_binding,
        )
        self._append_start(conflicting)
        status = research.validate_research_invocation(
            self.correlation, store=get_storage_backend()
        )
        self.assertEqual(status.status, "conflicting")
        self.assertEqual(status.blocker_code, "research_correlation_conflict")

    def test_validator_rejects_completion_with_missing_formal_evidence(self):
        self._append_start()
        self._append_completion(["missing-event"])
        status = research.validate_research_invocation(
            self.correlation, store=get_storage_backend()
        )
        self.assertEqual(status.status, "conflicting")
        self.assertEqual(status.blocker_code, "research_completion_invalid")

    def _append_start(self, correlation=None):
        value = correlation or self.correlation
        return EvidenceLogger.log(
            "research",
            "research_invocation_started",
            value.to_payload(),
            phase="research",
        )

    def _append_completion(self, evidence_ids):
        return EvidenceLogger.log(
            "research",
            "research_completion_receipt",
            {
                **self.correlation.to_payload(),
                "research_evidence_ids": evidence_ids,
            },
            phase="research",
        )


if __name__ == "__main__":
    unittest.main()
