import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { after, before } from "node:test";

import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const fullFixtureUrl = new URL("fixtures/workbench-v2.json", import.meta.url);
const invalidFixtureUrl = new URL(
  "fixtures/workbench-v2-invalid-binding.json",
  import.meta.url,
);

let vite;
let WorkbenchWorkspace;
let full;
let invalid;

before(async () => {
  vite = await createServer({
    appType: "custom",
    cacheDir: join(tmpdir(), `cycpep-vite-presentation-${process.pid}`),
    configFile: false,
    logLevel: "silent",
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true },
  });
  ({ WorkbenchWorkspace } = await vite.ssrLoadModule(
    "/app/workbench/components/workbench-workspace.tsx",
  ));
  full = JSON.parse(await readFile(fullFixtureUrl, "utf8")).data;
  invalid = JSON.parse(await readFile(invalidFixtureUrl, "utf8")).data;
});

after(() => vite?.close());

const callbacks = {
  onAutoRefreshChange() {},
  onRefresh() {},
};

function render(data, options = {}) {
  return renderToStaticMarkup(WorkbenchWorkspace({
    data,
    requestStatus: "ready",
    refreshError: null,
    autoRefreshEnabled: true,
    initialSelection: { kind: "task", identity: "T001" },
    initialCollapsedPanels: [],
    ...callbacks,
    ...options,
  }));
}

test("the populated desktop exposes all five workbench regions in reading order", () => {
  const html = render(full);
  assert.match(html, /<a[^>]*href="#workbench-primary"[^>]*>Skip to selected workspace<\/a>/);
  assert.match(html, /<section[^>]*id="workbench-primary"[^>]*aria-label="Selected workspace"/);
  const regions = [
    'aria-label="Workbench context"',
    'aria-label="Workbench navigator"',
    'aria-label="Selected workspace"',
    'aria-label="Workbench inspector"',
    'aria-label="Workbench history"',
  ];

  let previous = -1;
  for (const region of regions) {
    const position = html.indexOf(region);
    assert.notEqual(position, -1, `${region} must be rendered`);
    assert.ok(position > previous, `${region} must follow the visual reading order`);
    previous = position;
  }

  assert.match(html, /MDM2 \/ MDMX cyclic peptide campaign/);
  assert.match(html, /workflow-1/);
  assert.match(html, /run-1/);
  assert.match(html, /T001/);
  assert.doesNotMatch(html, /Collection coverage/i);
  assert.doesNotMatch(html, /READ-ONLY SCIENTIFIC OBSERVABILITY/);
});

test("collection coverage stays with navigator labels and preserves truncation truth", () => {
  const html = render(full);

  assert.match(html, /Tasks[^<]*3/i);
  assert.match(html, /Candidates[^<]*3\s*\/\s*4/i);
  assert.match(html, /Evidence[^<]*3\s*\/\s*4/i);
  assert.match(html, /(?:omitted|not shown|truncated)/i);
  assert.doesNotMatch(html, /<h[1-3][^>]*>Collection coverage<\/h[1-3]>/i);
});

test("invalid binding, no-run, stale, and blocker truth remain compact", () => {
  const noRun = structuredClone(invalid);
  noRun.blockers.items[0] = {
    code: "no_current_run",
    scope: "workflow",
    summary: "No current workflow run is recorded for this project.",
  };
  const html = render(noRun, {
    requestStatus: "stale-after-error",
    refreshError: "Service unavailable",
    initialSelection: { kind: "candidate", identity: "C-old" },
  });

  assert.match(html, /No active run/i);
  assert.match(html, /Data may be out of date/i);
  assert.match(html, /Service unavailable/);
  assert.match(html, /Needs attention/i);
  assert.match(html, /no_current_run/);
  assert.match(html, /C-old/);
  assert.doesNotMatch(html, /<h1[^>]*>Workflow \/ run<\/h1>/i);
  assert.doesNotMatch(html, /<h[1-3][^>]*>Structured blockers<\/h[1-3]>/i);

  const emptyNoRun = structuredClone(noRun);
  for (const name of ["candidates", "evidence", "artifacts"]) {
    emptyNoRun[name] = { ...emptyNoRun[name], total: 0, returned: 0, items: [] };
  }
  const overview = render(emptyNoRun, {
    initialSelection: { kind: "overview", identity: null },
  });
  assert.match(overview, /<dt>Candidates returned<\/dt><dd>0<\/dd>/i);
  assert.match(overview, /<dt>Evidence returned<\/dt><dd>0<\/dd>/i);
  assert.match(overview, /<dt>Artifacts returned<\/dt><dd>0<\/dd>/i);
});

test("the invalid-binding partial response stays trustworthy and project scoped", () => {
  const html = render(invalid, {
    initialSelection: { kind: "candidate", identity: "C-old" },
  });

  assert.match(html, /(?:Current run unavailable|Workflow binding invalid)/i);
  assert.match(html, /workflow_binding_invalid/);
  assert.match(html, /C-old/);
  assert.match(html, /historical/i);
  assert.doesNotMatch(html, /old-run[^<]*(?:current|active) run/i);
  assert.doesNotMatch(html, /State\.phase|directory scan|log-derived/i);
});

test("navigator and auxiliary panels expose keyboard-operable state", () => {
  const html = render(full, {
    initialSelection: { kind: "candidate", identity: "C0001" },
    initialCollapsedPanels: ["inspector", "history"],
  });

  assert.match(html, /role="group"[^>]*aria-label="Workbench collections"/);
  assert.match(html, /<button[^>]*aria-pressed="true"/);
  assert.match(html, /<button[^>]*aria-label="Restore inspector"[^>]*aria-expanded="false"/);
  assert.match(html, /<button[^>]*aria-label="Restore history"[^>]*aria-expanded="false"/);
  assert.match(html, /C0001/);
  assert.match(html, /Needs attention|transaction_compensation_unresolved/);
});

test("task selection preserves current and prior transaction lifecycle truth", () => {
  const retryData = structuredClone(full);
  retryData.executions.items[1] = {
    ...retryData.executions.items[1],
    attempts: 2,
    attempt_id: "T002-A02",
    transaction_visibility: "not_yet_recorded",
  };
  retryData.transactions.items.push({
    transaction_id: "tx-t002-a01",
    task_id: "T002",
    attempt_id: "T002-A01",
    status: "ROLLED_BACK",
  });

  const html = render(retryData, {
    initialSelection: { kind: "task", identity: "T002" },
  });

  assert.match(html, /T002-A02/);
  assert.match(html, /not yet recorded/i);
  assert.match(html, /tx-t002-a01/);
  assert.match(html, /T002-A01/);
  assert.match(html, /ROLLED_BACK/);
  assert.match(html, /Untimed (?:attempts|records)/i);
  assert.match(html, /2026-08-10T01:0[0-9]:00(?:\.000)?Z/);
  assert.doesNotMatch(html, /Research\s*(?:→|-&gt;)\s*Design/i);
  assert.doesNotMatch(html, /workflow progress|completion percentage/i);
});

test("task primary keeps transaction-only recovery blockers", () => {
  const html = render(full, {
    initialSelection: { kind: "task", identity: "T003" },
  });
  const primaryStart = html.indexOf('id="workbench-primary"');
  const inspectorStart = html.indexOf('aria-label="Workbench inspector"');
  const primary = html.slice(primaryStart, inspectorStart);

  assert.match(primary, /Execution and recovery blockers/);
  assert.match(primary, /transaction_compensation_unresolved/);
  assert.match(primary, /tx-3/);
});

test("selection detail reports omitted supporting collections", () => {
  const model = structuredClone(full);
  model.transactions = { ...model.transactions, total: 3, returned: 2, truncated: true };
  model.blockers = { ...model.blockers, total: 5, returned: 3, truncated: true };
  model.artifacts = { ...model.artifacts, total: 5, returned: 2, truncated: true };

  const taskHtml = render(model, {
    initialSelection: { kind: "task", identity: "T003" },
  });
  const taskInspector = taskHtml.slice(taskHtml.indexOf('aria-label="Workbench inspector"'));
  assert.match(taskInspector, /Partial returned context/);
  assert.match(taskInspector, /Transactions 2 \/ 3 returned · omitted/);
  assert.match(taskInspector, /Blockers 3 \/ 5 returned · omitted/);

  const candidateHtml = render(model, {
    initialSelection: { kind: "candidate", identity: "C0001" },
  });
  const candidateInspector = candidateHtml.slice(candidateHtml.indexOf('aria-label="Workbench inspector"'));
  assert.match(candidateInspector, /Artifacts 2 \/ 5 returned · omitted/);
});

test("the task inspector preserves returned metadata, transaction trace, and blocker identities", () => {
  const model = structuredClone(full);
  model.blockers.items.push({
    code: "task_integrity_blocked",
    scope: "transaction",
    summary: "Returned formal identities remain inspectable.",
    workflow_id: "workflow-1",
    run_id: "run-1",
    task_id: "T003",
    transaction_id: "tx-3",
  });
  const html = render(model, {
    initialSelection: { kind: "task", identity: "T003" },
  });
  const inspector = html.slice(html.indexOf('aria-label="Workbench inspector"'));

  assert.match(inspector, /Task metadata/);
  assert.match(inspector, /critic/);
  assert.match(inspector, /review/);
  assert.match(inspector, /optional/);
  assert.match(inspector, /review_prediction_handoff/);
  assert.match(inspector, /Returned transactions/);
  assert.match(inspector, /tx-3/);
  assert.match(inspector, /attempt_id/);
  assert.match(inspector, /T003-A01/);
  assert.match(inspector, /workflow_id/);
  assert.match(inspector, /workflow-1/);
  assert.match(inspector, /run_id/);
  assert.match(inspector, /run-1/);
  assert.match(inspector, /task_id/);
  assert.match(inspector, /transaction_id/);
});

test("candidate detail uses trace-only associations and artifact content_link", () => {
  const html = render(full, {
    initialSelection: { kind: "candidate", identity: "C0001" },
  });

  assert.match(html, /evt-battery-1/);
  assert.match(html, /artifact-1/);
  assert.match(html, /\/api\/v2\/artifacts\/artifact-1\/content/);
  assert.match(html, /Exploration shortlist relationship/);
  assert.match(html, /evt-shortlist/);
  assert.match(html, /passed:\s*false/i);
  assert.match(html, /1 Evidence · 1 artifacts/);
  assert.doesNotMatch(html, /\/api\/v1|coordinates/);
});

test("an exploratory shortlist item never receives passed styling", () => {
  const html = render(full, {
    initialSelection: { kind: "evidence", identity: "evt-shortlist" },
  });
  const item = html.match(
    /<article[^>]*data-candidate-id="C0001"[^>]*>[\s\S]*?<\/article>/,
  )?.[0];

  assert.ok(item, "the C0001 shortlist item must be rendered as an article");
  assert.match(item, /data-scientific-status="exploratory"/);
  assert.match(item, /passed:\s*false/i);
  assert.doesNotMatch(item, /(?:class|data-scientific-status)="[^"]*passed[^"]*"/i);
  assert.match(html, /0\s*\/\s*6 passed/);
  assert.match(html, /Exploration shortlist/);
});
