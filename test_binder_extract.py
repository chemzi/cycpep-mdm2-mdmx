"""Focused tests for binder extraction and source quote provenance."""

import json
import unittest
from unittest.mock import patch

from scripts import llm_extract


class BinderExtractionTextBudgetTests(unittest.TestCase):
    def test_extract_one_paper_supplies_only_the_first_30000_characters(self):
        paper_text = "A" * 29_999 + "B" + "C" * 10
        captured = {}

        def _fake_call_openai(system_prompt, user_content, model):
            captured["user_content"] = user_content
            return json.dumps({
                "is_relevant": True,
                "name": "Test binder",
                "sequence": "ACDEFGHI",
            })

        with patch("scripts.llm_extract.call_openai", side_effect=_fake_call_openai):
            result = llm_extract.extract_one_paper(
                {
                    "pmid": "12345",
                    "title": "Budget test",
                    "source": "Test Journal",
                    "source_type": "pmc_fulltext",
                    "content": paper_text,
                },
                "test-model",
                ["MDM2"],
            )

        supplied_text = captured["user_content"].split("Full Text: ", 1)[1]
        self.assertEqual(len(supplied_text), 30_000)
        self.assertEqual(supplied_text[-1], "B")
        self.assertNotIn("C", supplied_text)
        self.assertEqual(result["pmid"], "12345")


class SourceEvidenceContractTests(unittest.TestCase):
    def test_real_and_fabricated_quotes_are_annotated_against_current_paper(self):
        content = "The peptide sequence was ACDEFGHI. Its affinity for MDM2 was 8.7 nM."
        evidence = [
            {
                "field": "sequence",
                "quote": "The peptide sequence was\nACDEFGHI.",
                "pmid": "llm-rewrite",
                "source_type": "llm-rewrite",
            },
            {
                "field": "affinity_by_target.MDM2",
                "quote": "Its affinity for MDM2 was 2 pM.",
            },
            {
                "field": "design_insight",
                "quote": "The peptide sequence was ACDEFGHI.",
            },
        ]

        annotated = llm_extract._annotate_source_evidence(
            evidence, content, "12345", "pmc_fulltext"
        )

        self.assertEqual(
            annotated[0],
            {
                "field": "sequence",
                "quote": "The peptide sequence was\nACDEFGHI.",
                "pmid": "12345",
                "quote_verified": True,
                "source_type": "pmc_fulltext",
            },
        )
        self.assertFalse(annotated[1]["quote_verified"])
        self.assertEqual(annotated[1]["pmid"], "12345")
        self.assertEqual(annotated[1]["source_type"], "pmc_fulltext")
        self.assertEqual(
            [item["field"] for item in annotated],
            ["sequence", "affinity_by_target.MDM2"],
        )

    def test_extract_one_paper_keeps_source_evidence_optional_and_forces_pmid(self):
        raw = {
            "is_relevant": True,
            "name": "Legacy-compatible binder",
            "sequence": "ACDEFGHI",
            "pmid": "llm-rewrite",
            "design_insight": "Model inference only",
        }
        with patch("scripts.llm_extract.call_openai", return_value=json.dumps(raw)):
            result = llm_extract.extract_one_paper(
                {"pmid": "12345", "content": "Paper text", "source_type": "pubmed_abstract"},
                "test-model",
                ["MDM2"],
            )

        self.assertEqual(result["pmid"], "12345")
        self.assertNotIn("source_evidence", result)
        self.assertEqual(result["design_insight"], "Model inference only")

    def test_extract_one_paper_verifies_evidence_without_rejecting_the_binder(self):
        raw = {
            "is_relevant": True,
            "name": "Evidence binder",
            "sequence": "ACDEFGHI",
            "pmid": "llm-rewrite",
            "source_evidence": [
                {
                    "field": "sequence",
                    "quote": "The sequence was ACDEFGHI.",
                    "pmid": "llm-rewrite",
                    "source_type": "llm-rewrite",
                },
                {
                    "field": "affinity_by_target.MDM2",
                    "quote": "The affinity was 2 pM.",
                },
            ],
        }
        with patch("scripts.llm_extract.call_openai", return_value=json.dumps(raw)):
            result = llm_extract.extract_one_paper(
                {
                    "pmid": "12345",
                    "content": "The sequence was ACDEFGHI. The affinity was 8.7 nM.",
                    "source_type": "pmc_fulltext",
                },
                "test-model",
                ["MDM2"],
            )

        self.assertEqual(result["name"], "Evidence binder")
        self.assertEqual(result["pmid"], "12345")
        self.assertTrue(result["source_evidence"][0]["quote_verified"])
        self.assertFalse(result["source_evidence"][1]["quote_verified"])
        for evidence in result["source_evidence"]:
            self.assertEqual(evidence["pmid"], "12345")
            self.assertEqual(evidence["source_type"], "pmc_fulltext")

    def test_prompt_requires_concise_verbatim_quotes_and_labels_inference(self):
        prompt = llm_extract.EXTRACTION_PROMPT
        self.assertIn("逐字", prompt)
        self.assertIn("不得根据领域常识", prompt)
        self.assertIn("null", prompt)
        self.assertIn("一小段", prompt)
        self.assertIn("design_insight 是模型推断", prompt)


if __name__ == "__main__":
    unittest.main()
