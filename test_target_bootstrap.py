"""Offline contract tests for target bootstrap, review, and structure resolution."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from structure_resolution import (
    StructureNotReadyError,
    assert_target_structure_ready,
    materialize_target_coordinates,
    refresh_target_structure_readiness,
    resolve_target_structure,
)
from target_bootstrap import (
    BootstrapError,
    ReviewRequiredError,
    TargetBootstrapper,
    approve_draft,
    assert_project_approved,
    edit_draft,
    edit_target_draft,
    materialize_draft_coordinates,
    select_resolved_candidate,
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
            "pdb_url": "https://files.rcsb.org/download/9XYZ.pdb",
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
            self.assertTrue(edited["targets"][0]["structure_plan"]["binding_site_reviewed"])
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

            edit_target_draft(path, "NOVEL1", {
                "binding_site": {
                    "description": "old identity pocket", "residues": [11, 21],
                    "status": "user_reviewed", "confidence": "high",
                },
                "known_binders": [{"name": "old-target binder"}],
            })
            selected = select_resolved_candidate(
                path, "Q99999",
                experimental_provider=ExperimentalProvider(),
                predicted_provider=PredictedProvider(),
            )
            self.assertFalse(selected["bootstrap"]["ambiguous_identifier"])
            self.assertEqual(selected["bootstrap"]["selected_candidate"]["uniprot"], "Q99999")
            self.assertEqual(selected["targets"][0]["uniprot"], "Q99999")
            self.assertEqual(selected["targets"][0]["binding_site"]["residues"], [])
            self.assertEqual(selected["targets"][0]["binding_site"]["status"], "unknown")
            self.assertEqual(selected["targets"][0]["known_binders"], [])

    def test_target_patch_rejects_server_managed_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.json"
            self.bootstrapper().create_draft(identifier="NOVEL1", output_path=path)
            with self.assertRaises(BootstrapError):
                edit_target_draft(path, "NOVEL1", {
                    "structure_plan": {"ready_for_design": True},
                })

    def test_target_patch_preserves_other_targets_and_refreshes_site_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.json"
            draft = self.bootstrapper().create_draft(identifier="NOVEL1", output_path=path)
            second = json.loads(json.dumps(draft["targets"][0]))
            second.update({"id": "NOVEL2", "uniprot": "P54321"})
            draft["targets"].append(second)
            path.write_text(json.dumps(draft), encoding="utf-8")

            edited = edit_target_draft(path, "NOVEL1", {"binding_site": {
                "description": "reviewed pocket",
                "residues": [11, 21],
                "status": "user_reviewed",
                "confidence": "medium",
            }})
            self.assertEqual([row["id"] for row in edited["targets"]], ["NOVEL1", "NOVEL2"])
            self.assertTrue(edited["targets"][0]["structure_plan"]["binding_site_reviewed"])

    def test_target_patch_allows_id_rename_and_rediscovers_structure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.json"
            self.bootstrapper().create_draft(identifier="NOVEL1", output_path=path)

            edited = edit_target_draft(
                path, "NOVEL1", {"id": "RENAMED"},
                experimental_provider=ExperimentalProvider(),
                predicted_provider=PredictedProvider(),
            )

            self.assertEqual(edited["targets"][0]["id"], "RENAMED")
            self.assertEqual(edited["targets"][0]["metric_slug"], "renamed")
            self.assertEqual(edited["targets"][0]["structure_plan"]["selected"]["id"], "9XYZ")

    def test_coordinates_must_be_materialized_before_design_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.json"
            artifact_root = Path(directory) / "targets"
            draft = self.bootstrapper().create_draft(identifier="NOVEL1", output_path=path)
            self.assertFalse(draft["targets"][0]["structure_plan"]["coordinates_ready"])

            edited = edit_target_draft(path, "NOVEL1", {
                "binding_site": {
                    "description": "reviewed pocket", "residues": [11, 21],
                    "status": "user_reviewed", "confidence": "medium",
                },
                "structure": {"chain": "A"},
            })
            self.assertFalse(edited["targets"][0]["structure_plan"]["ready_for_design"])

            materialized = materialize_draft_coordinates(
                path, "NOVEL1", artifact_root,
                downloader=lambda _url: b"ATOM      1  CA  ALA A   1      1.000   2.000   3.000\nEND\n",
            )
            plan = materialized["targets"][0]["structure_plan"]
            self.assertTrue(plan["coordinates_ready"])
            self.assertTrue(plan["ready_for_design"])
            coordinate_path = Path(materialized["targets"][0]["structure"]["coordinate_path"])
            self.assertTrue(coordinate_path.is_file())
            assert_target_structure_ready(materialized, "NOVEL1")
            missing_hash = json.loads(json.dumps(materialized))
            missing_hash["targets"][0]["structure"].pop("coordinate_sha256")
            with self.assertRaises(StructureNotReadyError):
                assert_target_structure_ready(missing_hash, "NOVEL1")
            coordinate_path.write_text("tampered", encoding="utf-8")
            with self.assertRaises(StructureNotReadyError):
                assert_target_structure_ready(materialized, "NOVEL1")

    def test_planned_coordinate_requires_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinate = Path(directory) / "target.pdb"
            coordinate.write_text("ATOM      1  CA  ALA A   1\n", encoding="utf-8")
            target = {
                "id": "NOVEL1",
                "binding_site": {"residues": [1], "status": "user_reviewed"},
                "structure": {"chain": "A", "coordinate_path": str(coordinate)},
                "structure_plan": {
                    "selected": {"id": "9XYZ", "kind": "experimental", "quality_grade": "A"},
                    "quality_grade": "A",
                },
            }
            refreshed = refresh_target_structure_readiness(target)
            self.assertFalse(refreshed["structure_plan"]["coordinates_ready"])
            with self.assertRaises(StructureNotReadyError):
                assert_target_structure_ready({"targets": [refreshed]}, "NOVEL1")

    def test_planned_coordinate_accepts_uppercase_sha256(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinate = Path(directory) / "target.pdb"
            payload = b"ATOM      1  CA  ALA A   1\n"
            coordinate.write_bytes(payload)
            target = {
                "id": "NOVEL1",
                "binding_site": {"residues": [1], "status": "user_reviewed"},
                "structure": {
                    "chain": "A",
                    "coordinate_path": str(coordinate),
                    "coordinate_sha256": hashlib.sha256(payload).hexdigest().upper(),
                },
                "structure_plan": {
                    "selected": {"id": "9XYZ", "kind": "experimental", "quality_grade": "A"},
                    "quality_grade": "A",
                },
            }
            refreshed = refresh_target_structure_readiness(target)
            self.assertTrue(refreshed["structure_plan"]["ready_for_design"])
            self.assertEqual(
                assert_target_structure_ready({"targets": [refreshed]}, "NOVEL1")["id"],
                "NOVEL1",
            )

    def test_coordinate_download_rejects_untrusted_url(self):
        config = {"targets": [{
            "id": "NOVEL1",
            "structure_plan": {"selected": {
                "id": "evil", "kind": "predicted", "quality_grade": "B",
                "pdb_url": "https://127.0.0.1/private.pdb",
            }},
        }]}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(StructureNotReadyError):
                materialize_target_coordinates(
                    config, "NOVEL1", directory,
                    downloader=lambda _url: b"ATOM      1  CA  ALA A   1\n",
                )

    def test_rediscovery_clears_stale_coordinate_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.json"
            artifact_root = Path(directory) / "targets"
            self.bootstrapper().create_draft(identifier="NOVEL1", output_path=path)
            materialized = materialize_draft_coordinates(
                path, "NOVEL1", artifact_root,
                downloader=lambda _url: b"ATOM      1  CA  ALA A   1\n",
            )
            self.assertIn("coordinate_path", materialized["targets"][0]["structure"])

            rediscovered = edit_target_draft(
                path, "NOVEL1", {"structure": {"source": "user_selected_rcsb"}},
                experimental_provider=ExperimentalProvider([{
                    "source": "rcsb", "kind": "experimental", "id": "8NEW",
                    "pdb_id": "8NEW", "resolution": 2.0, "has_bound_partner": True,
                    "pdb_url": "https://files.rcsb.org/download/8NEW.pdb",
                }]),
                predicted_provider=PredictedProvider(),
            )
            structure = rediscovered["targets"][0]["structure"]
            self.assertNotIn("coordinate_path", structure)
            self.assertNotIn("coordinate_sha256", structure)
            self.assertFalse(rediscovered["targets"][0]["structure_plan"]["coordinates_ready"])

    def test_rediscovery_preserves_unchanged_target_coordinate_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.json"
            artifact_root = Path(directory) / "targets"
            draft = self.bootstrapper().create_draft(identifier="NOVEL1", output_path=path)
            second = json.loads(json.dumps(draft["targets"][0]))
            second.update({"id": "NOVEL2", "uniprot": "P54321"})
            draft["targets"].append(second)
            path.write_text(json.dumps(draft), encoding="utf-8")
            payload = lambda _url: b"ATOM      1  CA  ALA A   1\n"
            materialize_draft_coordinates(path, "NOVEL1", artifact_root, downloader=payload)
            materialized = materialize_draft_coordinates(
                path, "NOVEL2", artifact_root, downloader=payload,
            )
            second_artifact = dict(materialized["targets"][1]["structure"])

            rediscovered = edit_target_draft(
                path, "NOVEL1", {"structure": {"source": "user_selected_rcsb"}},
                experimental_provider=ExperimentalProvider([{
                    "source": "rcsb", "kind": "experimental", "id": "8NEW",
                    "pdb_id": "8NEW", "resolution": 2.0, "has_bound_partner": True,
                    "pdb_url": "https://files.rcsb.org/download/8NEW.pdb",
                }]),
                predicted_provider=PredictedProvider(),
            )

            self.assertNotIn("coordinate_path", rediscovered["targets"][0]["structure"])
            self.assertEqual(
                rediscovered["targets"][1]["structure"]["coordinate_path"],
                second_artifact["coordinate_path"],
            )
            self.assertEqual(
                rediscovered["targets"][1]["structure"]["coordinate_sha256"],
                second_artifact["coordinate_sha256"],
            )

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
        self.assertTrue(plan["coordinates_selected"])
        self.assertFalse(plan["coordinates_ready"])
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
