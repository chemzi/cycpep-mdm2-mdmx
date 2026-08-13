"""Production E2 publication edge before closed-loop Planner execution."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from contracts.trace import TraceContext
from core.context import ProjectContext
from exploration import exploration_shortlist, record_exploration_shortlist
from storage.sqlite_store import SQLiteStore
from test_exploration_decision import (
    PREDICTION_RUN_ID,
    PROJECT,
    PROJECT_ID,
    RUN_ID,
    TARGETS,
    THRESHOLDS,
    WORKFLOW_ID,
    evidence_batch,
    handoff_row,
)
from workflow.boundaries import FormalBoundary
from workflow.adapters import DefaultWorkflowRuntime
from workflow.exploration_decision_publication import (
    ExplorationDecisionPublicationError,
    publish_exploration_decision,
)


class WorkflowExplorationDecisionPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="workflow-e3-publication-")
        self.root = Path(self.temp.name)
        self.store = SQLiteStore(self.root / "store.db", project_id=PROJECT_ID)
        self.project = deepcopy(PROJECT)
        self.batteries = evidence_batch()
        for row in self.batteries:
            self.store.append(row)
        self.handoff_event = handoff_row(self.batteries)
        self.run_dir = self.root / PREDICTION_RUN_ID
        (self.run_dir / "inputs").mkdir(parents=True)
        (self.run_dir / "inputs" / "thresholds.json").write_text(
            json.dumps(THRESHOLDS), encoding="utf-8"
        )
        self.handoff_path = self.run_dir / "prediction_handoff.json"
        self.handoff_path.write_text(json.dumps({"run_id": PREDICTION_RUN_ID}), encoding="utf-8")
        self.handoff_event["handoff_path"] = str(self.handoff_path)
        self.store.append(self.handoff_event)
        self.prediction = FormalBoundary.completed(
            "prediction",
            prediction_run_id=PREDICTION_RUN_ID,
            handoff_path=str(self.handoff_path),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _critic_report(self, *, iterate: bool = True) -> Path:
        report = {
            "source": {
                "project_id": PROJECT_ID,
                "prediction_run_id": PREDICTION_RUN_ID,
                "required_targets": list(TARGETS),
                "record_count": len(self.batteries),
            },
            "recommendations": ([{"action": "iterate_interface_design"}] if iterate else []),
        }
        path = self.root / ("critic_iterate.json" if iterate else "critic_clear.json")
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    def test_current_formal_sources_publish_and_reuse_one_decision(self) -> None:
        report = self._critic_report()

        first = publish_exploration_decision(
            store=self.store,
            project_config=self.project,
            critic_report_path=report,
            prediction=self.prediction,
            source_round=1,
        )
        replay = publish_exploration_decision(
            store=self.store,
            project_config=self.project,
            critic_report_path=report,
            prediction=self.prediction,
            source_round=1,
        )

        shortlists = self.store.query(
            project_id=PROJECT_ID,
            agent="critic",
            event_type="exploration_shortlist",
        )
        decisions = self.store.query(
            project_id=PROJECT_ID,
            agent="critic",
            event_type="exploration_decision",
        )
        self.assertEqual(len(shortlists), 1)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(first.decision_id, replay.decision_id)
        self.assertEqual(decisions[0]["decision_id"], first.decision_id)

    def test_non_iterative_critic_does_not_publish(self) -> None:
        result = publish_exploration_decision(
            store=self.store,
            project_config=self.project,
            critic_report_path=self._critic_report(iterate=False),
            prediction=self.prediction,
            source_round=1,
        )

        self.assertIsNone(result)
        self.assertFalse(self.store.query(event_type="exploration_shortlist"))
        self.assertFalse(self.store.query(event_type="exploration_decision"))

    def test_missing_threshold_snapshot_fails_before_publication(self) -> None:
        (self.run_dir / "inputs" / "thresholds.json").unlink()

        with self.assertRaises(ExplorationDecisionPublicationError) as captured:
            publish_exploration_decision(
                store=self.store,
                project_config=self.project,
                critic_report_path=self._critic_report(),
                prediction=self.prediction,
                source_round=1,
            )

        self.assertEqual(captured.exception.code, "thresholds_invalid")
        self.assertFalse(self.store.query(event_type="exploration_shortlist"))
        self.assertFalse(self.store.query(event_type="exploration_decision"))

    def test_threshold_digest_mismatch_fails_before_publication(self) -> None:
        (self.run_dir / "inputs" / "thresholds.json").write_text(
            json.dumps({"changed": {"value": 1}}), encoding="utf-8"
        )

        with self.assertRaises(ValueError):
            publish_exploration_decision(
                store=self.store,
                project_config=self.project,
                critic_report_path=self._critic_report(),
                prediction=self.prediction,
                source_round=1,
            )

        self.assertFalse(self.store.query(event_type="exploration_shortlist"))
        self.assertFalse(self.store.query(event_type="exploration_decision"))

    def test_incomplete_battery_coverage_fails_before_publication(self) -> None:
        incomplete = SQLiteStore(self.root / "incomplete.db", project_id=PROJECT_ID)
        for row in self.batteries[:-1]:
            incomplete.append(row)
        incomplete.append(self.handoff_event)

        with self.assertRaises(ExplorationDecisionPublicationError) as captured:
            publish_exploration_decision(
                store=incomplete,
                project_config=self.project,
                critic_report_path=self._critic_report(),
                prediction=self.prediction,
                source_round=1,
            )

        self.assertEqual(captured.exception.code, "exploration_decision_battery_incomplete")
        self.assertFalse(incomplete.query(event_type="exploration_shortlist"))
        self.assertFalse(incomplete.query(event_type="exploration_decision"))

    def test_store_project_mismatch_fails_before_publication(self) -> None:
        other = SQLiteStore(self.root / "other.db", project_id="other_project")

        with self.assertRaises(ExplorationDecisionPublicationError) as captured:
            publish_exploration_decision(
                store=other,
                project_config=self.project,
                critic_report_path=self._critic_report(),
                prediction=self.prediction,
                source_round=1,
            )

        self.assertEqual(captured.exception.code, "exploration_decision_store_mismatch")
        self.assertFalse(other.query(event_type="exploration_shortlist"))
        self.assertFalse(other.query(event_type="exploration_decision"))

    def test_duplicate_current_shortlist_fails_closed(self) -> None:
        publish_exploration_decision(
            store=self.store,
            project_config=self.project,
            critic_report_path=self._critic_report(),
            prediction=self.prediction,
            source_round=1,
        )
        duplicate = deepcopy(self.store.query(event_type="exploration_shortlist")[0])
        duplicate["event_id"] = "shortlist-duplicate"
        self.store.append(duplicate)

        with self.assertRaises(ExplorationDecisionPublicationError) as captured:
            publish_exploration_decision(
                store=self.store,
                project_config=self.project,
                critic_report_path=self._critic_report(),
                prediction=self.prediction,
                source_round=1,
            )

        self.assertEqual(captured.exception.code, "exploration_shortlist_ambiguous")
        self.assertEqual(len(self.store.query(event_type="exploration_decision")), 1)

    def test_interrupted_publication_reuses_exact_shortlist(self) -> None:
        payload = exploration_shortlist(
            self.batteries, targets=list(TARGETS), thresholds=THRESHOLDS
        )
        event_id = record_exploration_shortlist(
            payload,
            targets=list(TARGETS),
            round_num=1,
            trace_context=TraceContext(PROJECT_ID, WORKFLOW_ID, RUN_ID),
            store=self.store,
        )

        decision = publish_exploration_decision(
            store=self.store,
            project_config=self.project,
            critic_report_path=self._critic_report(),
            prediction=self.prediction,
            source_round=1,
        )

        self.assertEqual(len(self.store.query(event_type="exploration_shortlist")), 1)
        self.assertEqual(decision.shortlist_event_id, event_id)
        self.assertEqual(len(self.store.query(event_type="exploration_decision")), 1)

    def test_duplicate_current_decision_fails_closed(self) -> None:
        publish_exploration_decision(
            store=self.store,
            project_config=self.project,
            critic_report_path=self._critic_report(),
            prediction=self.prediction,
            source_round=1,
        )
        duplicate = deepcopy(self.store.query(event_type="exploration_decision")[0])
        duplicate["event_id"] = "decision-duplicate"
        self.store.append(duplicate)

        with self.assertRaises(ExplorationDecisionPublicationError) as captured:
            publish_exploration_decision(
                store=self.store,
                project_config=self.project,
                critic_report_path=self._critic_report(),
                prediction=self.prediction,
                source_round=1,
            )

        self.assertEqual(captured.exception.code, "exploration_decision_ambiguous")
        self.assertEqual(len(self.store.query(event_type="exploration_decision")), 2)

    def test_explicit_store_never_falls_back_to_ambient_backend(self) -> None:
        with patch(
            "data_layer.get_storage_backend",
            side_effect=AssertionError("ambient Store must not be read"),
        ):
            decision = publish_exploration_decision(
                store=self.store,
                project_config=self.project,
                critic_report_path=self._critic_report(),
                prediction=self.prediction,
                source_round=1,
            )

        self.assertEqual(decision.project_id, PROJECT_ID)

    def test_stale_approved_project_revision_fails_before_publication(self) -> None:
        stale = deepcopy(self.project)
        stale["review"]["content_digest"] = "0" * 64

        with self.assertRaises(ValueError):
            publish_exploration_decision(
                store=self.store,
                project_config=stale,
                critic_report_path=self._critic_report(),
                prediction=self.prediction,
                source_round=1,
            )

        self.assertFalse(self.store.query(event_type="exploration_shortlist"))
        self.assertFalse(self.store.query(event_type="exploration_decision"))

    def test_mismatched_formal_workflow_fails_before_publication(self) -> None:
        self.handoff_event["workflow_id"] = "different-workflow"
        mismatch = SQLiteStore(self.root / "mismatch.db", project_id=PROJECT_ID)
        for row in self.batteries:
            mismatch.append(row)
        mismatch.append(self.handoff_event)

        with self.assertRaises(ValueError):
            publish_exploration_decision(
                store=mismatch,
                project_config=self.project,
                critic_report_path=self._critic_report(),
                prediction=self.prediction,
                source_round=1,
            )

        self.assertFalse(mismatch.query(event_type="exploration_shortlist"))
        self.assertFalse(mismatch.query(event_type="exploration_decision"))

    def test_runtime_reuses_one_injected_store_state_snapshot_for_planner(self) -> None:
        runtime = object.__new__(DefaultWorkflowRuntime)
        runtime.context = ProjectContext(PROJECT_ID, self.project)
        runtime.store = MagicMock(project_id=PROJECT_ID)
        runtime.store.get_state.return_value = {"round": 3, "marker": "formal"}
        handoff = SimpleNamespace(
            workflow_id=WORKFLOW_ID,
            exploration_decision={"decision_id": "decision-current"},
            required=True,
        )

        with (
            patch(
                "data_layer.get_storage_backend",
                side_effect=AssertionError("ambient Store must not be read"),
            ),
            patch(
                "workflow.exploration_decision_publication.publish_exploration_decision"
            ) as publish,
            patch(
                "workflow.exploration_decision_handoff.resolve_exploration_decision_handoff",
                return_value=handoff,
            ) as resolve,
            patch("agents.planner.run", return_value={"plan_id": "plan"}) as planner,
        ):
            state = runtime.publish_exploration_decision(
                self.prediction, self._critic_report()
            )
            result = runtime.run_planner(self._critic_report(), state=state)

        self.assertEqual(result, {"plan_id": "plan"})
        runtime.store.get_state.assert_called_once_with(PROJECT_ID)
        self.assertEqual(publish.call_args.kwargs["source_round"], 3)
        self.assertEqual(resolve.call_args.kwargs["state"]["round"], 3)
        self.assertEqual(planner.call_args.kwargs["state"]["marker"], "formal")


if __name__ == "__main__":
    unittest.main()
