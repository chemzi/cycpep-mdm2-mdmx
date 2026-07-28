import json
import os
import unittest
from unittest.mock import patch

from scripts import threshold_research


class _BytesResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self._payload


class ThresholdResearchFullTextTests(unittest.TestCase):
    def test_seed_pmids_are_kept_before_search_results(self):
        with patch.object(threshold_research, "search_pubmed", return_value=["99999", "40542165"]):
            captured = {}

            def fake_fetch_paper_texts(pmids):
                captured["pmids"] = pmids
                return {"40542165": "seed text"}, {"40542165": "pubmed_abstract"}

            with patch.dict(os.environ, {}, clear=True), \
                 patch.object(threshold_research, "fetch_abstracts", return_value={
                     "40542165": {"title": "RFpeptides"}
                 }), \
                 patch.object(threshold_research, "fetch_paper_texts", side_effect=fake_fetch_paper_texts):
                result = threshold_research.research_one_layer(
                    "L1_plddt",
                    threshold_research.LAYER_QUERIES["L1_plddt"],
                    "step-3.7-flash",
                )

        self.assertEqual(captured["pmids"][0], "40542165")
        self.assertEqual(captured["pmids"].count("40542165"), 1)
        self.assertEqual(result["pmids_checked"][0], "40542165")

    def test_pmc_fulltext_includes_figure_captions(self):
        xml = b"""<?xml version="1.0"?>
        <pmc-articleset>
          <article>
            <front>
              <article-meta>
                <article-id pub-id-type="pmid">12345</article-id>
                <abstract><p>Abstract text.</p></abstract>
              </article-meta>
            </front>
            <body><p>Body text.</p></body>
            <floats-group>
              <fig>
                <caption><p>Self-consistency used pLDDT &gt; 0.8 and 2.0 A backbone RMSD.</p></caption>
              </fig>
            </floats-group>
          </article>
        </pmc-articleset>
        """
        with patch("scripts.threshold_research.urllib.request.urlopen", return_value=_BytesResponse(xml)):
            texts = threshold_research.fetch_pmc_fulltext(["PMC123"])

        self.assertIn("12345", texts)
        self.assertIn("pLDDT > 0.8", texts["12345"])
        self.assertIn("2.0 A backbone RMSD", texts["12345"])

    def test_layer_uses_pmc_fulltext_for_llm_and_quote_verification(self):
        captured = {}
        quote = "Candidates were filtered using pLDDT > 0.8 before experimental testing."

        def fake_call_openai(system_prompt, user_content, model):
            captured["user_content"] = user_content
            return json.dumps({
                "found": True,
                "value": 0.8,
                "operator": ">",
                "unit": None,
                "metric_name": "pLDDT",
                "evidence_grade": "paper_explicit",
                "source_pmid": "12345",
                "evidence_quote": quote,
                "context": "full-text methods filter",
                "confidence": "high",
            })

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), \
             patch.object(threshold_research, "search_pubmed", return_value=["12345"]), \
             patch.object(threshold_research, "fetch_abstracts", return_value={
                 "12345": {"title": "Full text threshold paper"}
             }), \
             patch.object(threshold_research, "fetch_paper_texts", return_value=(
                 {"12345": f"The abstract omits the cutoff. {quote}"},
                 {"12345": "pmc_fulltext"},
             )), \
             patch.object(threshold_research, "call_openai", side_effect=fake_call_openai):
            result = threshold_research.research_one_layer(
                "L1_plddt",
                {"desc": "pLDDT threshold", "queries": ["RFpeptides"]},
                "step-3.7-flash",
            )

        self.assertTrue(result["auto_usable"])
        self.assertTrue(result["quote_verified"])
        self.assertEqual(result["source_type"], "pmc_fulltext")
        self.assertEqual(result["papers_with_pmc_fulltext"], 1)
        self.assertIn("SOURCE_TYPE: pmc_fulltext", captured["user_content"])
        self.assertIn("TEXT:", captured["user_content"])
        self.assertIn(quote, captured["user_content"])

    def test_curated_rfpeptides_threshold_is_verified_against_fulltext(self):
        quote = "refold with pLDDT > 0.8 and within 2.0 Å backbone r.m.s.d."
        with patch.object(threshold_research, "search_pubmed", return_value=[]), \
             patch.object(threshold_research, "fetch_abstracts", return_value={
                 "40542165": {"title": "RFpeptides"}
             }), \
             patch.object(threshold_research, "fetch_paper_texts", return_value=(
                 {"40542165": f"Figure caption: {quote}"},
                 {"40542165": "pmc_fulltext"},
             )), \
             patch.object(threshold_research, "call_openai") as call_openai:
            result = threshold_research.research_one_layer(
                "L1_plddt",
                threshold_research.LAYER_QUERIES["L1_plddt"],
                "step-3.7-flash",
            )

        self.assertTrue(result["auto_usable"])
        self.assertTrue(result["curated_seed"])
        self.assertEqual(result["source_pmid"], "40542165")
        self.assertEqual(result["value"], 0.8)
        self.assertEqual(result["operator"], ">")
        call_openai.assert_not_called()

    def test_curated_rfpeptides_threshold_requires_quote_verification(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(threshold_research, "search_pubmed", return_value=[]), \
             patch.object(threshold_research, "fetch_abstracts", return_value={
                 "40542165": {"title": "RFpeptides"}
             }), \
             patch.object(threshold_research, "fetch_paper_texts", return_value=(
                 {"40542165": "Full text without the curated threshold sentence."},
                 {"40542165": "pmc_fulltext"},
             )):
            result = threshold_research.research_one_layer(
                "L1_plddt",
                threshold_research.LAYER_QUERIES["L1_plddt"],
                "step-3.7-flash",
            )

        self.assertFalse(result["found"])
        self.assertEqual(result["reason"], "llm_unavailable_no_api_key")


if __name__ == "__main__":
    unittest.main()
