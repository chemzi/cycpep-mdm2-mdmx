"""Offline contract tests for target bootstrap, review, and structure resolution."""

import json
import tempfile
import unittest
from pathlib import Path

from structure_resolution import (
    StructureNotReadyError,
    assert_target_structure_ready,
    resolve_target_structure,
)
from target_bootstrap import (
    ReviewRequiredError,
    TargetBootstrapper,
    approve_draft,
    assert_project_approved,
    edit_draft,
)


class FakeDiscovery:
    def __init__(self, ambiguous=False):
        self.ambiguous = ambiguous

    def resolve(self, identifier, identifier_type, organism_id):
        primary = {
            "id": "NOVEL1", "gene_name": "NOVEL1", "uniprot": "P12345",
            "protein_name": "Novel target", "organism": "Homo sapiens",
        }
        return {
            "primary": primary,
            "candidates": [primary, {"id": "NOVEL1B", "uniprot": "Q99999"}]
            if self.ambiguous else [primary],
            "ambiguous": self.ambiguous,
            "evidence": [{"source": "UniProt", "id": "P12345"}],
        }


class FakeLLM:
    model = "fake-json-model"

    def json(self, **kwargs):
        return {
            "project_name": "NOVEL1 binder project",
            "objective": "binder",
            "target_enrichment": {
                "aliases": ["N1"],
                "function_summary": "Evidence-constrained summary",
                "binding_site": {
                    "description": "candidate pocket", "residues": [10, 20],
                    "status": "hypothesis", "confidence": "low",
                    "source_refs": ["E001"],
                },
                "uncertainties": ["No validated peptide binder"],
            },
            "assumptions": ["Binding site requires experimental review"],
        }


class FailingLLM:
    model = "broken"

    def json(self, **kwargs):
        raise RuntimeError("offline")


class ExperimentalProvider:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [{
            "source": "rcsb", "kind": "experimental", "id": "9XYZ",
            "pdb_id": "9XYZ", "resolution": 2.1, "has_bound_partner": True,
        }]

    def find(self, target):
        return self.rows


class PredictedProvider:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [{
            "source": "alphafold_db", "kind": "predicted", "id": "AF-P12345",
            "mean_plddt": 87, "epitope_plddt": 82, "pae_available": True,
        }]

    def find(self, target):
        return self.rows


class BootstrapTests(unittest.TestCase):
    def bootstrapper(self, **kwargs):
        return TargetBootstrapper(
            discovery=kwargs.get("discovery", FakeDiscovery()),
            llm=kwargs.get("llm", FakeLLM()),
            experimental_provider=kwargs.get("experimental", ExperimentalProvider()),
            predicted_provider=kwargs.get("predicted", PredictedProvider()),
        )

    def test_draft_edit_approve_and_tamper_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.json"
            draft = self.bootstrapper().create_draft(
                identifier="NOVEL1", output_path=path,
            )
            self.assertEqual(draft["review"]["status"], "draft")
            self.assertEqual(draft["targets"][0]["structure_plan"]["status"], "experimental_selected")
            with self.assertRaises(ReviewRequiredError):
                assert_project_approved(draft)

            edited = edit_draft(path, {
                "targets": [{**draft["targets"][0], "binding_site": {
                    "description": "reviewed pocket", "residues": [11, 21],
                    "status": "user_reviewed", "confidence": "medium",
                }}]
            })
            self.assertEqual(edited["review"]["revision"], 2)
            approved = approve_draft(path)
            assert_project_approved(approved)

            approved["targets"][0]["binding_site"]["residues"] = [999]
            with self.assertRaises(ReviewRequiredError):
                assert_project_approved(approved)

    def test_ambiguous_identifier_blocks_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.json"
            self.bootstrapper(discovery=FakeDiscovery(ambiguous=True)).create_draft(
                identifier="NOVEL1", output_path=path,
            )
            with self.assertRaises(ReviewRequiredError):
                approve_draft(path)

    def test_llm_failure_keeps_reviewable_draft(self):
        draft = self.bootstrapper(llm=FailingLLM()).create_draft(identifier="NOVEL1")
        self.assertEqual(draft["bootstrap"]["llm_status"], "failed")
        self.assertIn("llm_enrichment_incomplete", draft["review"]["warnings"])

    def test_experimental_preferred_over_prediction(self):
        plan = resolve_target_structure(
            {"id": "NOVEL1", "uniprot": "P12345"},
            experimental_provider=ExperimentalProvider(),
            predicted_provider=PredictedProvider(),
        )
        self.assertEqual(plan["status"], "experimental_selected")
        self.assertEqual(plan["quality_grade"], "A")
        self.assertTrue(plan["coordinates_ready"])
        self.assertFalse(plan["ready_for_design"])

    def test_prediction_fallback_and_missing_structure_gate(self):
        plan = resolve_target_structure(
            {"id": "NOVEL1", "uniprot": "P12345"},
            experimental_provider=ExperimentalProvider([]),
            predicted_provider=PredictedProvider(),
        )
        self.assertEqual(plan["status"], "predicted_selected")
        self.assertTrue(plan["needs_ensemble"])

        config = {"targets": [{
            "id": "NOVEL1", "structure_plan": {
                "ready_for_design": False, "required_next_step": "run_target_structure_prediction",
            },
        }]}
        with self.assertRaises(StructureNotReadyError):
            assert_target_structure_ready(config, "NOVEL1")


if __name__ == "__main__":
    unittest.main()
