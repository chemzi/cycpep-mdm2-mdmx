import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  WORKBENCH_ENDPOINT,
  fetchWorkbench,
  parseWorkbenchEnvelope,
} from "../app/workbench/client.ts";

const fixtureUrl = new URL("fixtures/workbench-v2.json", import.meta.url);
const partialUrl = new URL("fixtures/workbench-v2-invalid-binding.json", import.meta.url);

async function fixture(url = fixtureUrl) {
  return JSON.parse(await readFile(url, "utf8"));
}

test("parses the frozen V2 envelope without changing formal scientific fields", async () => {
  const source = await fixture();

  const parsed = parseWorkbenchEnvelope(source);
  const shortlist = parsed.data.evidence.items.find(
    (item) => item.event_type === "exploration_shortlist",
  );

  assert.equal(parsed, source);
  assert.equal(shortlist.n_passed, 0);
  assert.equal(shortlist.n_evaluated, 6);
  assert.deepEqual(shortlist.shortlist[0], {
    candidate_id: "C0001",
    passed: false,
    desirability: 0.25,
    pareto_front: true,
    reason: "pareto_front",
    top_margin_metric: "L2_ipsae_mdm2",
  });
  assert.deepEqual(shortlist.calibration, {
    calibrated: 1,
    provisional: 1,
    unavailable: 1,
  });
  assert.deepEqual(shortlist.source_event_ids, ["evt-battery-1", "evt-battery-2"]);
  assert.deepEqual(shortlist.unmapped_metrics, ["totally_unknown"]);
});

test("accepts the trustworthy invalid-binding partial response", async () => {
  const parsed = parseWorkbenchEnvelope(await fixture(partialUrl));

  assert.equal(parsed.data.workflow, null);
  assert.equal(parsed.data.run, null);
  assert.deepEqual(parsed.data.tasks.items, []);
  assert.equal(parsed.data.candidates.items[0].candidate_id, "C-old");
  assert.equal(parsed.data.blockers.items[0].code, "workflow_binding_invalid");
});

test("rejects unsupported and malformed contracts", async () => {
  const unsupported = structuredClone(await fixture());
  unsupported.data.schema_version = "frontend.workbench.v3";
  const malformed = structuredClone(await fixture());
  delete malformed.data.tasks.returned;

  assert.throws(() => parseWorkbenchEnvelope(unsupported), /frontend\.workbench\.v2/);
  assert.throws(() => parseWorkbenchEnvelope(malformed), /tasks/);
});

test("fetches only the exact V2 workbench route and rejects HTTP failures", async () => {
  const source = await fixture();
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push([url, init]);
    return new Response(JSON.stringify(source), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };

  const result = await fetchWorkbench({ apiOrigin: "https://example.test/", fetchImpl });

  assert.deepEqual(result, source);
  assert.deepEqual(calls, [[`https://example.test${WORKBENCH_ENDPOINT}`, { signal: undefined }]]);
  assert.equal(calls.some(([url]) => String(url).includes("/api/v1/")), false);

  await assert.rejects(
    fetchWorkbench({ fetchImpl: async () => new Response("unavailable", { status: 503 }) }),
    /503/,
  );
});
