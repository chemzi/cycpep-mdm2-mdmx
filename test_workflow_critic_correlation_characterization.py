"""RED characterization for Launcher Critic current-run correlation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from contracts.critic import critic_persistence_effects
from prediction_pipeline.contracts import file_sha256
from workflow.boundaries import FormalBoundaryInspector


class _Store:
    def __init__(self, events):
        self.events = list(events)

    def query(self, **filters):
        return [
            event
            for event in self.events
            if all(event.get(key) == value for key, value in filters.items())
        ]

    def get_artifact(self, _artifact_id):
        return None


class _SQLiteLikeStore(_Store):
    """Match SQLiteStore.query's supported top-level filters."""

    def query(self, **filters):
        supported = {
            key: value
            for key, value in filters.items()
            if key in {"project_id", "agent", "event_type"}
        }
        return super().query(**supported)


def _inspector(events, *, store_type=_Store):
    def not_started(*_args, **_kwargs):
        return SimpleNamespace(status="not_started")

    return FormalBoundaryInspector(
        store=store_type(events),
        research_validator=not_started,
        design_validator=not_started,
        prediction_validator=not_started,
        orchestrator_status=lambda **_kwargs: {},
    )


def _write_report(root: Path, name: str, run_id: str) -> tuple[Path, dict]:
    path = root / f"{name}.json"
    report = {"report_id": name, "source": {"prediction_run_id": run_id}}
    path.write_text(json.dumps(report), encoding="utf-8")
    return path, report


def _event(path: Path, report: dict, *, run_id_marker=...):
    event = {
        "event_id": f"event-{report['report_id']}",
        "project_id": "project-current",
        "agent": "critic",
        "event_type": "critic_review",
        "report_id": report["report_id"],
        "report_path": str(path),
        "report_sha256": file_sha256(path),
    }
    if run_id_marker is not ...:
        event["prediction_run_id"] = run_id_marker
    return event


def _prediction_start(*, run_id: str, timestamp: str) -> dict:
    return {
        "event_id": f"event-start-{run_id}",
        "project_id": "project-current",
        "agent": "prediction",
        "event_type": "prediction_invocation_started",
        "prediction_run_id": run_id,
        "timestamp": timestamp,
    }


class CriticCorrelationCharacterizationTests(unittest.TestCase):
    def test_new_critic_evidence_payload_carries_prediction_run_id(self):
        _, evidence = critic_persistence_effects(
            report={
                "critic_version": "test",
                "report_id": "critic-current",
                "source": {"prediction_run_id": "prediction-current"},
                "verdict": "clear",
                "passed": True,
                "issue_counts": {},
                "recommendations": [],
                "issues": {},
                "summary": "clear",
                "metrics_snapshot": {},
            },
            report_path="critic-current.json",
            report_digest="report-digest",
            state={},
        )

        self.assertEqual(evidence["prediction_run_id"], "prediction-current")

    def test_unrelated_broken_legacy_history_does_not_block_explicit_current_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current_path, current_report = _write_report(
                root, "critic-current", "prediction-current"
            )
            unrelated_broken_legacy = {
                "event_id": "event-old-broken",
                "project_id": "project-current",
                "agent": "critic",
                "event_type": "critic_review",
                "report_id": "critic-old",
                "report_path": str(root / "missing-old-report.json"),
                "report_sha256": "legacy-digest",
            }
            current = _event(
                current_path, current_report, run_id_marker="prediction-current"
            )

            result = _inspector([unrelated_broken_legacy, current]).critic(
                project_id="project-current",
                prediction_run_id="prediction-current",
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.references["report_id"], "critic-current")

    def test_old_broken_legacy_history_does_not_block_first_critic_for_current_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_broken_legacy = {
                "event_id": "event-old-broken",
                "project_id": "project-current",
                "agent": "critic",
                "event_type": "critic_review",
                "report_id": "critic-old",
                "report_path": str(root / "missing-old-report.json"),
                "report_sha256": "legacy-digest",
                "timestamp": "2026-08-09T10:00:00+00:00",
            }
            current_start = _prediction_start(
                run_id="prediction-current",
                timestamp="2026-08-10T10:00:00+00:00",
            )
            current_completion = {
                "event_id": "event-complete-prediction-current",
                "project_id": "project-current",
                "agent": "prediction",
                "event_type": "prediction_handoff_ready",
                "prediction_run_id": "prediction-current",
                "run_id": "prediction-current",
                "timestamp": "2026-08-10T10:00:30+00:00",
            }

            result = _inspector(
                [old_broken_legacy, current_start, current_completion]
            ).critic(
                project_id="project-current",
                prediction_run_id="prediction-current",
            )

            self.assertEqual(result.status, "not_started")

    def test_prediction_start_is_filtered_after_sqlite_supported_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_broken_legacy = {
                "event_id": "event-old-broken",
                "project_id": "project-current",
                "agent": "critic",
                "event_type": "critic_review",
                "report_id": "critic-old",
                "report_path": str(Path(tmp) / "missing-old-report.json"),
                "timestamp": "2026-08-09T10:00:00+00:00",
            }
            unrelated_start = _prediction_start(
                run_id="prediction-other",
                timestamp="2026-08-08T10:00:00+00:00",
            )
            current_start = _prediction_start(
                run_id="prediction-current",
                timestamp="2026-08-10T10:00:00+00:00",
            )

            result = _inspector(
                [old_broken_legacy, unrelated_start, current_start],
                store_type=_SQLiteLikeStore,
            ).critic(
                project_id="project-current",
                prediction_run_id="prediction-current",
            )

            self.assertEqual(result.status, "not_started")

    def test_possibly_current_unverifiable_legacy_report_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current_start = _prediction_start(
                run_id="prediction-current",
                timestamp="2026-08-10T10:00:00+00:00",
            )
            possibly_current = {
                "event_id": "event-possibly-current",
                "project_id": "project-current",
                "agent": "critic",
                "event_type": "critic_review",
                "report_id": "critic-possibly-current",
                "report_path": str(root / "missing-current-report.json"),
                "report_sha256": "legacy-digest",
                "timestamp": "2026-08-10T10:01:00+00:00",
            }

            result = _inspector([current_start, possibly_current]).critic(
                project_id="project-current",
                prediction_run_id="prediction-current",
            )

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.blocker_code, "critic_recovery_ambiguous")

    def test_unreadable_legacy_without_report_id_remains_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current_start = _prediction_start(
                run_id="prediction-current",
                timestamp="2026-08-10T10:00:00+00:00",
            )
            cases = {
                "post-start": "2026-08-10T10:01:00+00:00",
                "missing-time": None,
            }
            for name, timestamp in cases.items():
                with self.subTest(name=name):
                    possibly_current = {
                        "event_id": f"event-{name}",
                        "project_id": "project-current",
                        "agent": "critic",
                        "event_type": "critic_review",
                        "report_path": str(root / f"missing-{name}.json"),
                    }
                    if timestamp is not None:
                        possibly_current["timestamp"] = timestamp

                    result = _inspector([current_start, possibly_current]).critic(
                        project_id="project-current",
                        prediction_run_id="prediction-current",
                    )

                    self.assertEqual(result.status, "blocked")
                    self.assertEqual(
                        result.blocker_code, "critic_recovery_ambiguous"
                    )

    def test_broken_explicit_current_report_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event = {
                "event_id": "event-current-broken",
                "project_id": "project-current",
                "agent": "critic",
                "event_type": "critic_review",
                "prediction_run_id": "prediction-current",
                "report_id": "critic-current",
                "report_path": str(root / "missing-current-report.json"),
                "report_sha256": "current-digest",
            }

            result = _inspector([event]).critic(
                project_id="project-current",
                prediction_run_id="prediction-current",
            )

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.blocker_code, "critic_recovery_ambiguous")

    def test_conflicting_explicit_current_records_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = []
            for name in ("critic-current-a", "critic-current-b"):
                path, report = _write_report(root, name, "prediction-current")
                events.append(
                    _event(path, report, run_id_marker="prediction-current")
                )

            result = _inspector(events).critic(
                project_id="project-current",
                prediction_run_id="prediction-current",
            )

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.blocker_code, "critic_recovery_ambiguous")

    def test_valid_legacy_record_remains_usable_when_document_proves_current_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, report = _write_report(
                Path(tmp), "critic-legacy", "prediction-current"
            )

            result = _inspector([_event(path, report)]).critic(
                project_id="project-current",
                prediction_run_id="prediction-current",
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.references["report_id"], "critic-legacy")


if __name__ == "__main__":
    unittest.main()
