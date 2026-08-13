import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  WORKBENCH_ENDPOINT,
  WorkbenchContractError,
  buildWorkbenchUrl,
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

test("rejects malformed required nested records before rendering", async () => {
  const cases = [
    ["project", (data) => { data.project.targets = "MDM2"; }],
    ["tasks.items[0].action", (data) => { delete data.tasks.items[0].action.name; }],
    ["executions.items[0]", (data) => { data.executions.items[0].attempts = "1"; }],
    ["transactions.items[0]", (data) => { delete data.transactions.items[0].task_id; }],
    ["candidates.items[0]", (data) => { delete data.candidates.items[0].run_relation; }],
    ["evidence.items[1]", (data) => { data.evidence.items[1].trace = []; }],
    ["artifacts.items[0]", (data) => { data.artifacts.items[0].run_relation = "current"; }],
    ["protocols.items[0]", (data) => { data.protocols.items[0].version = 2.1; }],
    ["trace", (data) => { data.trace.run_id = 42; }],
    ["blockers.items[0]", (data) => { delete data.blockers.items[0].summary; }],
  ];

  for (const [field, mutate] of cases) {
    const malformed = structuredClone(await fixture());
    mutate(malformed.data);

    assert.throws(
      () => parseWorkbenchEnvelope(malformed),
      (error) => error instanceof WorkbenchContractError && error.message.includes(field),
      field,
    );
  }
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

test("builds exact scoped and backward-compatible unscoped workbench URLs", () => {
  const launcherRunId = "launcher_0123456789abcdef0123456789abcdef";

  assert.equal(buildWorkbenchUrl(undefined), WORKBENCH_ENDPOINT);
  assert.equal(
    buildWorkbenchUrl("https://example.test/", launcherRunId),
    `https://example.test${WORKBENCH_ENDPOINT}?launcher_run_id=${launcherRunId}`,
  );
  assert.throws(
    () => buildWorkbenchUrl(undefined, "../launcher_bad"),
    /launcherRunId/,
  );
});

test("fetches the launcher-scoped workbench without changing request semantics", async () => {
  const source = await fixture();
  const calls = [];
  const launcherRunId = "launcher_0123456789abcdef0123456789abcdef";
  const fetchImpl = async (url, init) => {
    calls.push([url, init]);
    return new Response(JSON.stringify(source), { status: 200 });
  };

  await fetchWorkbench({ launcherRunId, fetchImpl });

  assert.deepEqual(calls, [[
    `${WORKBENCH_ENDPOINT}?launcher_run_id=${launcherRunId}`,
    { signal: undefined },
  ]]);
});
