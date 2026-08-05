"""9B-1 test: legacy handler adapter bridge (PR36 migration).

Verifies ``make_legacy_handler_adapter`` wraps a legacy
``handler(HandlerContext)`` into the transaction signature, forwarding
outputs/processes with EMPTY transaction fields (candidate_updates /
state_updates / artifacts). The adapter must not synthesize transaction
state — legacy handler side effects remain a Phase 2 item.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contracts.transaction import TransactionContext
from execution.adapters import make_legacy_handler_adapter
from execution.results import ExecutionActionResult
from execution.staging import StagingArea


class LegacyHandlerAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cycpep-legacy-adapter-"))
        self.staging = StagingArea(self.root / "staging", "tx-legacy").create()
        self.context = TransactionContext.create(
            workflow_id="w1", run_id="r1", task_id="T002", attempt_id="T002-A01",
        )

    def test_legacy_adapter_forwards_outputs_processes_only(self) -> None:
        """Legacy adapter returns outputs/processes with empty transaction fields."""
        fake_outputs = (("prediction_handoff", self.root / "handoff.json"),)
        fake_processes = ({"label": "prediction", "returncode": 0},)

        def legacy_handler(handler_context) -> ExecutionActionResult:
            return ExecutionActionResult(outputs=fake_outputs, processes=fake_processes)

        adapter = make_legacy_handler_adapter(
            legacy_handler,
            packet={"task": {"action": "evaluate_new_design_candidates"}},
            config=None,
            task_dir=self.root,
        )
        result = adapter(self.context, self.staging)
        self.assertEqual(result.outputs, fake_outputs)
        self.assertEqual(result.processes, fake_processes)
        # transaction fields stay empty: legacy side effects are not transactional
        self.assertEqual(result.candidate_updates, ())
        self.assertEqual(result.state_updates, {})
        self.assertEqual(result.artifacts, ())

    def test_legacy_adapter_rejects_non_execution_action_result(self) -> None:
        """Legacy adapter enforces the typed return contract."""

        def bad_handler(handler_context):
            return {"candidate_updates": []}

        adapter = make_legacy_handler_adapter(
            bad_handler, packet={"task": {}}, config=None, task_dir=self.root,
        )
        with self.assertRaises(TypeError):
            adapter(self.context, self.staging)


if __name__ == "__main__":
    unittest.main()
