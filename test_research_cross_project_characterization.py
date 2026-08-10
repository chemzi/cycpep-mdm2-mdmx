"""RED characterization for project-scoped Research completion Evidence."""

from __future__ import annotations

import unittest

from agents.research_contract import (
    ResearchCorrelation,
    validate_research_invocation,
)


class _MultiProjectStore:
    def __init__(self, events):
        self.events = list(events)

    def query(self, **filters):
        return [
            event
            for event in self.events
            if all(event.get(key) == value for key, value in filters.items())
        ]


class ResearchCrossProjectCharacterizationTests(unittest.TestCase):
    def test_completion_rejects_research_evidence_owned_by_another_project(self):
        correlation = ResearchCorrelation(
            research_invocation_id="research-current",
            launcher_run_id="launcher-current",
            project_id="project-current",
            approved_content_binding="approved-current",
        )
        binding = correlation.to_payload()
        events = [
            {
                "event_id": "research-start-current",
                "agent": "research",
                "event_type": "research_invocation_started",
                **binding,
            },
            {
                "event_id": "research-targets-foreign",
                "agent": "research",
                "event_type": "research_targets",
                "project_id": "project-foreign",
            },
            {
                "event_id": "research-completion-current",
                "agent": "research",
                "event_type": "research_completion_receipt",
                **binding,
                "research_evidence_ids": ["research-targets-foreign"],
            },
        ]

        result = validate_research_invocation(
            correlation, store=_MultiProjectStore(events)
        )

        self.assertEqual(result.status, "conflicting")
        self.assertEqual(result.blocker_code, "research_completion_invalid")


if __name__ == "__main__":
    unittest.main()
