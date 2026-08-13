"""Focused real-writer regression for Prediction publication identity."""

from dataclasses import replace
import unittest

import test_prediction_transactional as transactional
from workflow.prediction_publication import PredictionPublicationError


class PredictionPublicationBindingTests(unittest.TestCase):
    def setUp(self):
        self.harness = transactional.PredictionTransactionalTests(methodName="runTest")
        self.harness.setUp()

    def test_committed_prediction_record_run_binding_fails_closed(self):
        for mutation in ("missing", "mismatched"):
            real_handler = self.harness._handler()

            def mutated_handler(context, mutation=mutation):
                result = real_handler(context)
                events = [dict(event) for event in result.evidence_events]
                recorded = next(
                    event for event in events
                    if event["event_type"] == "prediction_recorded"
                )
                if mutation == "missing":
                    recorded.pop("prediction_run_id")
                else:
                    recorded["prediction_run_id"] = "prediction-wrong"
                return replace(result, evidence_events=tuple(events))

            with self.subTest(mutation=mutation):
                store, _, transaction, _ = self.harness._run(
                    self.harness.root / f"record-run-{mutation}",
                    handler=mutated_handler,
                )
                with self.assertRaises(PredictionPublicationError):
                    self.harness._publication_proof(store, transaction)
