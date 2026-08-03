import json
import os
import socket
import ssl
import tempfile
import unittest
import urllib.error
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import agents.research as research
import data_layer
from data_layer import State, evaluate_battery
from scripts import threshold_research
from threshold_contract import normalize_thresholds


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


class ResearchStateAndCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="research-cache-test-")
        self.root = Path(self.temp.name)
        self.data = self.root / "not-created-yet" / "data"
        self.evidence = self.root / "not-created-yet" / "evidence"
        self.state_path = self.data / "state.json"
        self.log_path = self.evidence / "evidence_log.jsonl"
        self.cache_path = self.data / "_research_cache.json"
        self.threshold_path = self.data / "_thresholds_cache.json"
        self.path_patchers = [
            patch.object(data_layer, "DATA_DIR", self.data),
            patch.object(data_layer, "EVIDENCE_DIR", self.evidence),
            patch.object(data_layer, "STATE_PATH", self.state_path),
            patch.object(data_layer, "LOG_PATH", self.log_path),
            patch.object(research, "DATA_DIR", self.data),
            patch.object(research, "EVIDENCE_DIR", self.evidence),
            patch.object(research, "CACHE_PATH", self.cache_path),
            patch.object(research, "THRESHOLDS_CACHE", self.threshold_path),
        ]
        for patcher in self.path_patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.path_patchers):
            patcher.stop()
        self.temp.cleanup()

    @staticmethod
    def _pipeline_result():
        return {
            "targets": research.TARGETS,
            "pocket_differences": research.POCKET_DIFFERENCES,
            "known_dual_binders": [],
            "known_binder_source": "none_found",
            "thresholds": research._default_thresholds(research.PROJECT_CONFIG),
            "_pipeline_meta": {
                "stage_status": {
                    "rcsb_search": "complete", "rcsb_enrich": "complete",
                    "pubmed": "complete", "llm_extract": "degraded_no_api_key",
                    "threshold_research": "degraded_no_api_key",
                },
                "run_status": "degraded_with_fallbacks",
                "fallbacks_used": ["curated_mdm_binders", "provisional_default_thresholds"],
            },
        }

    def test_recompute_creates_completely_missing_runtime_directories(self):
        self.assertFalse(self.data.exists())
        with patch.object(research, "_run_pipeline", return_value=self._pipeline_result()):
            result = research.recompute()
        self.assertTrue(self.data.is_dir())
        self.assertTrue(self.evidence.is_dir())
        self.assertTrue(self.threshold_path.is_file())
        self.assertEqual(result["research_pipeline_meta"]["run_status"], "degraded_with_fallbacks")

    def test_threshold_cache_write_uses_atomic_replace(self):
        original_replace = os.replace
        with patch("agents.research.os.replace", wraps=original_replace) as replace:
            research._write_threshold_cache(
                research._default_thresholds(research.PROJECT_CONFIG), research.PROJECT_CONFIG
            )
        replace.assert_called_once()
        source, destination = map(Path, replace.call_args.args)
        self.assertEqual(source.parent, destination.parent)
        self.assertEqual(destination, self.threshold_path)

    def test_old_threshold_keys_are_canonicalized(self):
        normalized, audit = normalize_thresholds({
            "L4_ring_closure": {"value": 2.0, "evidence_grade": "team_provisional"},
            "L6_pose_convergence": {"value": 1.5, "evidence_grade": "positive_control"},
        })
        self.assertEqual(set(normalized), {"L4_nc_term_dist", "L6_pose_rmsd"})
        self.assertEqual(audit["conflicts"], [])

    def test_duplicate_old_and_new_keys_keep_higher_evidence(self):
        normalized, audit = normalize_thresholds({
            "L4_ring_closure": {"value": 3.0, "evidence_grade": "team_provisional"},
            "L4_nc_term_dist": {
                "value": 2.0, "evidence_grade": "paper_explicit",
                "source_pmid": "1", "evidence_quote": "verified explicit threshold sentence",
                "quote_verified": True,
            },
        })
        self.assertEqual(normalized["L4_nc_term_dist"]["value"], 2.0)
        self.assertEqual(len(audit["conflicts"]), 1)
        self.assertEqual(normalize_thresholds({})[0], {})

    def test_empty_state_recovers_from_cache_with_canonical_keys(self):
        State.save({"thresholds": {}})
        research._write_threshold_cache({
            "L4_ring_closure": {"value": 2.0, "evidence_grade": "team_provisional"}
        }, research.PROJECT_CONFIG)
        sync = State.sync_thresholds_from_cache(self.threshold_path)
        self.assertEqual(sync["status"], "complete")
        self.assertIn("L4_nc_term_dist", sync["state"]["thresholds"])
        self.assertNotIn("L4_ring_closure", sync["state"]["thresholds"])

    def test_provisional_cache_does_not_overwrite_calibrated_paper_state(self):
        State.save({"thresholds": {
            "L2_ipsae": {
                "value": 0.7, "evidence_grade": "paper_explicit",
                "calibration_status": "calibrated", "source_pmid": "1",
                "evidence_quote": "verified threshold", "quote_verified": True,
            }
        }})
        research._write_threshold_cache({
            "L2_ipsae": {"value": 0.55, "evidence_grade": "team_provisional"}
        }, research.PROJECT_CONFIG)
        sync = State.sync_thresholds_from_cache(self.threshold_path)
        self.assertEqual(sync["state"]["thresholds"]["L2_ipsae"]["value"], 0.7)
        self.assertEqual(sync["audit"]["skipped"], ["L2_ipsae"])

    def test_missing_and_corrupt_threshold_cache_are_safe(self):
        State.save({"thresholds": {}})
        missing = State.sync_thresholds_from_cache(self.threshold_path)
        self.assertEqual(missing["status"], "cache_missing")
        self.threshold_path.parent.mkdir(parents=True, exist_ok=True)
        self.threshold_path.write_text("{broken", encoding="utf-8")
        corrupt = State.sync_thresholds_from_cache(self.threshold_path)
        self.assertEqual(corrupt["status"], "cache_invalid")

    def test_cache_invalidates_on_approval_digest_or_target_identity_change(self):
        payload = {"_cache_meta": research._cache_meta(research.PROJECT_CONFIG), "thresholds": {}}
        research._atomic_write_json(self.cache_path, payload)
        changed_digest = deepcopy(research.PROJECT_CONFIG)
        changed_digest["review"]["approved_digest"] = "different"
        self.assertIsNone(research._load_valid_cache(self.cache_path, changed_digest))

        changed_target = deepcopy(research.PROJECT_CONFIG)
        changed_target["targets"][0]["uniprot"] = "CHANGED"
        self.assertIsNone(research._load_valid_cache(self.cache_path, changed_target))

    def test_sync_project_config_replaces_removed_targets(self):
        State.save({
            "project_id": "same", "approved_digest": "old-digest",
            "targets": {"OLD": {}, "KEEP": {}},
            "thresholds": {"L5_hotspot_coverage": {
                "value": 0.67, "evidence_grade": "design_rule",
            }},
        })
        config = deepcopy(research.PROJECT_CONFIG)
        config["project_id"] = "same"
        config["targets"] = [deepcopy(config["targets"][0])]
        config["targets"][0]["id"] = "NEW"
        synced = State.sync_project_config(config)
        self.assertEqual(set(synced["targets"]), {"NEW"})
        self.assertEqual(synced["approved_digest"], config["review"]["approved_digest"])
        self.assertEqual(synced["thresholds"], {})

    def test_generic_project_without_explicit_hotspot_rule_gets_unavailable_l5(self):
        config = deepcopy(research.PROJECT_CONFIG)
        config["project_id"] = "generic"
        config["targets"] = [{
            "id": "EGFR", "required": True,
            "binding_site": {"residues": [1, 2], "status": "user_reviewed"},
        }]
        l5 = research._default_thresholds(config)["L5_hotspot_coverage"]
        self.assertIsNone(l5["value"])
        self.assertEqual(l5["evidence_grade"], "unavailable")
        self.assertEqual(l5["calibration_status"], "unavailable")

    def test_generic_project_uses_only_explicit_reviewed_hotspot_rule(self):
        config = {
            "project_id": "generic", "schema_version": 1,
            "selection": {"hotspot_coverage_threshold": 0.75},
            "targets": [{
                "id": "EGFR", "required": True,
                "binding_site": {"residues": [1, 2], "status": "user_reviewed"},
            }],
        }
        l5 = research._default_thresholds(config)["L5_hotspot_coverage"]
        self.assertEqual(l5["value"], 0.75)
        self.assertEqual(l5["evidence_grade"], "design_rule")
        self.assertEqual(l5["applicable_targets"], ["EGFR"])

    def test_null_threshold_is_safe_and_blocks_clearance(self):
        thresholds = research._default_thresholds({
            "project_id": "generic", "schema_version": 1,
            "targets": [{"id": "EGFR", "required": True}],
        })
        outcome = evaluate_battery({}, thresholds, required_targets=["EGFR"])
        self.assertFalse(outcome["competition_clearance"])
        self.assertEqual(outcome["triage_status"], "needs_more_evidence")
        self.assertIn("L5_hotspot_coverage:EGFR", outcome["missing_thresholds"])

    def test_network_failure_and_empty_search_have_distinct_codes(self):
        empty_cfg = {"desc": "test", "queries": ["none"]}
        with patch.object(threshold_research, "search_pubmed", return_value=[]):
            empty = threshold_research.research_one_layer("L2_ipsae", empty_cfg, "model")
        self.assertEqual(empty["stage_error_code"], "api_empty_result")

        error = urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
        self.assertEqual(threshold_research.classify_network_error(error), "dns_error")
        tls = urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))
        self.assertEqual(threshold_research.classify_network_error(tls), "tls_ca_error")
        self.assertEqual(research._stage_diagnostic("empty")[0], "api_empty_result")
        self.assertEqual(
            research._stage_diagnostic("empty", "certificate verify failed")[0],
            "tls_ca_error",
        )

    def test_research_diagnostic_preserves_redacted_prefix_and_error_tail(self):
        message = "progress-start " + ("x" * 700) + " final HTTP 429 failure"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret-token"}):
            sanitized = research._sanitize_message(
                message + " secret-token"
            )
        self.assertIn("progress-start", sanitized)
        self.assertIn("final HTTP 429 failure", sanitized)
        self.assertIn("[truncated]", sanitized)
        self.assertIn("[REDACTED]", sanitized)
        self.assertNotIn("secret-token", sanitized)
        self.assertEqual(
            research._stage_diagnostic("failed", sanitized)[0],
            "http_429",
        )

    def test_force_recompute_bypasses_both_old_caches(self):
        with patch.object(research, "_run_pipeline", return_value=self._pipeline_result()) as runner, \
             patch.object(research, "_load_valid_cache") as cache_loader:
            research.run(force_recompute=True)
        runner.assert_called_once()
        cache_loader.assert_not_called()

    def test_no_api_key_is_degraded_even_when_core_network_stages_complete(self):
        statuses = {
            "rcsb_search": "complete", "rcsb_enrich": "complete", "pubmed": "complete",
            "llm_extract": "degraded_no_api_key", "threshold_research": "degraded_no_api_key",
        }
        self.assertEqual(research._overall_run_status(statuses), "degraded_with_fallbacks")


if __name__ == "__main__":
    unittest.main()
