"""Focused tests for the binder PubMed text budget."""

import io
import json
import unittest
from unittest.mock import patch

from scripts import pubmed_search


class PubMedTextBudgetTests(unittest.TestCase):
    def test_pmc_fulltext_keeps_character_30000_and_truncates_after_it(self):
        paper_text = "A" * 29_999 + "B" + "C" * 10
        xml = (
            "<articles><article><front><article-meta>"
            '<article-id pub-id-type="pmid">12345</article-id>'
            "</article-meta></front><body><p>"
            f"{paper_text}"
            "</p></body></article></articles>"
        ).encode("utf-8")

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return xml

        with (
            patch("scripts.pubmed_search.urllib.request.urlopen", return_value=_Response()),
            patch("scripts.pubmed_search.time.sleep"),
        ):
            result = pubmed_search.fetch_pmc_fulltext(["PMC12345"])["12345"]

        self.assertEqual(len(result), 30_000)
        self.assertEqual(result[-1], "B")
        self.assertIn("B", result[8_000:])
        self.assertNotIn("C", result)

    def test_abstract_fallback_uses_the_same_30000_character_budget(self):
        abstract = "A" * 29_999 + "B" + "C" * 10
        config = {
            "project_id": "binder-budget-test",
            "targets": [{"id": "MDM2", "uniprot": "Q00987"}],
        }
        metadata = {
            "12345": {
                "title": "Budget test",
                "pubdate": "2026",
                "source": "Test Journal",
                "authors": [],
                "elocationid": "",
            }
        }
        stdout = io.StringIO()

        with (
            patch("scripts.pubmed_search.load_project_config", return_value=config),
            patch("scripts.pubmed_search.search_pubmed", return_value=["12345"]),
            patch("scripts.pubmed_search.fetch_metadata", return_value=metadata),
            patch("scripts.pubmed_search.fetch_pubmed_abstracts", return_value={"12345": abstract}),
            patch("scripts.pubmed_search.fetch_pmc_ids", return_value={}),
            patch("scripts.pubmed_search.sys.argv", ["pubmed_search"]),
            patch("scripts.pubmed_search.sys.stdout", stdout),
        ):
            self.assertEqual(pubmed_search.main(), 0)

        content = json.loads(stdout.getvalue())["papers"][0]["content"]
        self.assertEqual(len(content), 30_000)
        self.assertEqual(content[-1], "B")
        self.assertNotIn("C", content)


if __name__ == "__main__":
    unittest.main()
