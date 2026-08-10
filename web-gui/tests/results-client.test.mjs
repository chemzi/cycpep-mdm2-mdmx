import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  RESULTS_ENDPOINT,
  ResultsContractError,
  fetchResults,
  parseResultsEnvelope,
} from "../app/workbench/results-client.ts";

const fixtureUrl = new URL("fixtures/results-v1.json", import.meta.url);

async function envelope() {
  const data = JSON.parse(await readFile(fixtureUrl, "utf8"));
  return { request_id: "test-request", data };
}

test("parses the frozen results digest without changing formal fields", async () => {
  const source = await envelope();
  const parsed = parseResultsEnvelope(source);

  assert.equal(parsed, source);
  assert.equal(parsed.data.schema_version, "frontend.results.v1");
  assert.equal(parsed.data.summary.candidates_total, 4);
  assert.equal(parsed.data.summary.hard_cleared, 2);
  assert.equal(parsed.data.summary.data_basis, "demo_fixture");
  assert.equal(parsed.data.finalists[0].candidate_id, "C0101");
  assert.equal(parsed.data.finalists[0].rank, 1);
  assert.equal(parsed.data.finalists[0].hard_cleared, true);
  assert.equal(parsed.data.layers[0].key, "L1_plddt");
  assert.equal(parsed.data.thresholds.counts.calibrated, 5);
  assert.match(parsed.data.conclusion, /hard-clearance battery/);
  assert.match(parsed.data.conclusion, /demo fixture data/);
});

test("rejects unsupported schema versions and malformed payloads", async () => {
  const unsupported = await envelope();
  unsupported.data.schema_version = "frontend.results.v2";
  assert.throws(() => parseResultsEnvelope(unsupported), /frontend\.results\.v1/);

  const missingSummary = await envelope();
  delete missingSummary.data.summary.hard_cleared;
  assert.throws(() => parseResultsEnvelope(missingSummary), /summary\.hard_cleared/);

  const missingLayer = await envelope();
  delete missingLayer.data.layers[0].passed;
  assert.throws(() => parseResultsEnvelope(missingLayer), /layers\[0\]\.passed/);

  const missingFinalist = await envelope();
  delete missingFinalist.data.finalists[0].rank;
  assert.throws(() => parseResultsEnvelope(missingFinalist), /finalists\[0\]\.rank/);

  const badBasis = await envelope();
  badBasis.data.summary.data_basis = "synthetic";
  assert.throws(() => parseResultsEnvelope(badBasis), /data_basis/);

  const missingConclusion = await envelope();
  delete missingConclusion.data.conclusion;
  assert.throws(() => parseResultsEnvelope(missingConclusion), /conclusion/);
});

test("fetchResults unwraps the adapter envelope and surfaces HTTP failures", async () => {
  const source = await envelope();
  const calls = [];
  const fetchImpl = async (url) => {
    calls.push(String(url));
    return {
      ok: true,
      async json() {
        return source;
      },
    };
  };
  const parsed = await fetchResults({ fetchImpl });
  assert.equal(parsed.data.summary.candidates_total, 4);
  assert.equal(calls[0], RESULTS_ENDPOINT);

  const failingFetch = async () => ({ ok: false, status: 503 });
  await assert.rejects(fetchResults({ fetchImpl: failingFetch }), /HTTP 503/);
});

test("fetchResults honors apiOrigin prefixes", async () => {
  const source = await envelope();
  const calls = [];
  const fetchImpl = async (url) => {
    calls.push(String(url));
    return { ok: true, async json() { return source; } };
  };
  await fetchResults({ apiOrigin: "http://127.0.0.1:8765/", fetchImpl });
  assert.equal(calls[0], "http://127.0.0.1:8765/api/v2/results");
});

test("ResultsContractError has a stable name", () => {
  assert.equal(new ResultsContractError("boom").name, "ResultsContractError");
});
