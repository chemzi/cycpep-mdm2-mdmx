import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { after, before } from "node:test";

import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { createServer } from "vite";

const fixtureUrl = new URL("fixtures/workbench-v2.json", import.meta.url);
let vite;
let components;

before(async () => {
  vite = await createServer({
    appType: "custom",
    cacheDir: join(tmpdir(), `cycpep-scientific-presentation-${process.pid}`),
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true },
  });
  components = {
    ...(await vite.ssrLoadModule("/app/workbench/components/task-graph.tsx")),
    ...(await vite.ssrLoadModule("/app/workbench/components/candidate-workspace.tsx")),
    ...(await vite.ssrLoadModule("/app/workbench/components/exploration-shortlist.tsx")),
    ...(await vite.ssrLoadModule("/app/workbench/components/evidence-provenance.tsx")),
    ...(await vite.ssrLoadModule("/app/workbench/components/artifact-trace.tsx")),
    ...(await vite.ssrLoadModule("/app/workbench/components/execution-transaction.tsx")),
    ...(await vite.ssrLoadModule("/app/workbench/components/shared-states.tsx")),
  };
});

after(async () => {
  await vite?.close();
});

async function workbench() {
  const envelope = JSON.parse(await readFile(fixtureUrl, "utf8"));
  return envelope.data;
}

test("controlled task detail preserves returned action, approval, and current attempt truth", async () => {
  const model = await workbench();
  const html = renderToStaticMarkup(createElement(components.TaskGraph, {
    tasks: model.tasks,
    executions: model.executions,
    transactions: model.transactions,
    blockers: model.blockers.items,
    selectedTaskId: "T003",
    onSelectTask() {},
  }));

  const detailStart = html.indexOf("Execution / transaction");
  assert.ok(detailStart >= 0);
  const detail = html.slice(detailStart);
  assert.match(detail, /T003/);
  assert.match(detail, /Attempt T003-A01/);
  assert.match(detail, /scientific_input_invalid/);
  assert.match(detail, /transaction_compensation_unresolved/);
  assert.doesNotMatch(detail, /Attempt T001-A01|Attempt T002-A01/);
  assert.doesNotMatch(html, /phase|progress/i);
});

test("candidate structure stage uses only a formally trace-linked artifact", async () => {
  const model = await workbench();
  const html = renderToStaticMarkup(components.CandidateWorkspace({
    candidates: model.candidates,
    evidence: model.evidence.items,
    artifacts: model.artifacts.items,
    selectedCandidateId: "C0001",
    onSelectCandidate() {},
  }));

  assert.match(html, /Structure availability/);
  assert.match(html, /artifact-1/);
  assert.match(html, /Browser-safe content link available/);
  assert.doesNotMatch(html, /artifact-2/);
  assert.match(html, /Exploration shortlist relationship/);
  assert.match(html, /evt-shortlist/);
  assert.match(html, /1 Evidence · 1 artifacts/);
});

test("exploratory shortlist items expose an explicit non-passed scientific status", async () => {
  const model = await workbench();
  const shortlist = model.evidence.items.find(
    (item) => item.event_type === "exploration_shortlist",
  );
  const html = renderToStaticMarkup(components.ExplorationShortlist({
    shortlist,
    evidence: model.evidence.items,
    headingId: "shortlist-title",
    passedHeadingId: "passed-title",
    onSelectEvidence() {},
  }));

  assert.match(html, /0 \/ 6 passed/);
  assert.equal((html.match(/data-scientific-status="exploratory"/g) ?? []).length, 2);
  assert.equal((html.match(/data-scientific-status="passed"/g) ?? []).length, 0);
  assert.match(html, /data-candidate-id="C0001"/);
  assert.match(html, /data-candidate-id="C0002"/);
  assert.match(html, /L2_ipsae_mdm2/);
  assert.match(html, /evt-battery-2/);
  assert.match(html, /totally_unknown/);
});

test("selection-sensitive Evidence and artifact details preserve opaque contract truth", async () => {
  const model = await workbench();
  const evidence = model.evidence.items.find((item) => item.event_id === "evt-shortlist");
  const artifact = model.artifacts.items.find((item) => item.artifact_id === "artifact-2");
  const evidenceHtml = renderToStaticMarkup(components.EvidenceRecordDetail({ evidence }));
  const artifactHtml = renderToStaticMarkup(components.ArtifactRecordDetail({ artifact }));

  assert.match(evidenceHtml, /evt-shortlist/);
  assert.match(evidenceHtml, /protocol-exploration-v1/);
  assert.match(evidenceHtml, /parent_event_id/);
  assert.match(artifactHtml, /artifact-2/);
  assert.match(artifactHtml, /Content unavailable: no formal content_link returned/);
  assert.doesNotMatch(artifactHtml, /\/api\/v1|coordinates|server path/i);
});

test("transaction and blocker records expose returned semantic states without inference", async () => {
  const model = await workbench();
  const executionHtml = renderToStaticMarkup(components.ExecutionTransactionDetail({
    task: model.tasks.items[2],
    executions: model.executions.items,
    transactions: model.transactions.items,
    blockers: model.blockers.items,
  }));
  const blockerHtml = renderToStaticMarkup(components.BlockerList({
    blockers: model.blockers.items,
    headingId: "blockers",
    compact: true,
  }));

  assert.match(executionHtml, /data-lifecycle-status="FAILED"/);
  assert.match(executionHtml, /data-lifecycle-status="COMPENSATION_CONFLICT"/);
  assert.match(blockerHtml, /data-blocker-code="approval_required"/);
  assert.match(blockerHtml, /data-blocker-code="transaction_compensation_unresolved"/);
  assert.match(blockerHtml, /class="workbench-blockers is-compact"/);
});
