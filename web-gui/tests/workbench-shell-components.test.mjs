import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test, { after } from "node:test";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import { tmpdir } from "node:os";

import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const root = fileURLToPath(new URL("../", import.meta.url));
const server = await createServer({
  root,
  appType: "custom",
  logLevel: "silent",
  configFile: false,
  cacheDir: join(tmpdir(), `cycpep-vite-shell-${process.pid}`),
  server: { middlewareMode: true },
});
after(() => server.close());

const { WorkbenchShell } = await server.ssrLoadModule("/app/workbench/components/workbench-shell.tsx");
const { BlockerList } = await server.ssrLoadModule("/app/workbench/components/shared-states.tsx");
const full = JSON.parse(await readFile(new URL("fixtures/workbench-v2.json", import.meta.url), "utf8")).data;
const invalid = JSON.parse(await readFile(new URL("fixtures/workbench-v2-invalid-binding.json", import.meta.url), "utf8")).data;

const shellProps = {
  requestStatus: "ready",
  refreshError: null,
  autoRefreshEnabled: true,
  onRefresh() {},
  onAutoRefreshChange() {},
};

test("renders formal project, workflow, run status, and structured blockers", () => {
  const html = renderToStaticMarkup(WorkbenchShell({ ...shellProps, data: full }));

  assert.match(html, /MDM2 \/ MDMX cyclic peptide campaign/);
  assert.match(html, /workflow-1/);
  assert.match(html, /run-1/);
  assert.match(html, /Overall status/);
  assert.match(html, /transaction_compensation_unresolved/);
  assert.match(html, /Auto refresh/);
  assert.doesNotMatch(html, /Research.*Design.*Prediction.*Critic/);
});

test("keeps project-scoped content visible for an invalid workflow binding", () => {
  const html = renderToStaticMarkup(WorkbenchShell({
    ...shellProps,
    data: invalid,
    children: "HISTORICAL PROJECT DATA",
  }));

  assert.match(html, /Workflow binding invalid/);
  assert.match(html, /workflow_binding_invalid/);
  assert.match(html, /Workflow and run details are unavailable/);
  assert.match(html, /HISTORICAL PROJECT DATA/);
  assert.doesNotMatch(html, /old-run/);
});

test("distinguishes no-current-run and stale-after-refresh-error", () => {
  const noRun = structuredClone(invalid);
  noRun.blockers.items[0] = {
    code: "no_current_run",
    scope: "workflow",
    summary: "No current workflow run is recorded for this project.",
  };
  const html = renderToStaticMarkup(WorkbenchShell({
    ...shellProps,
    data: noRun,
    requestStatus: "stale-after-error",
    refreshError: "Service unavailable",
  }));

  assert.match(html, /No current run/);
  assert.match(html, /Showing the last successful response/);
  assert.match(html, /Service unavailable/);
});

test("uses caller-provided semantic IDs for repeated blocker regions", () => {
  const blockers = full.blockers.items;
  const globalHtml = renderToStaticMarkup(BlockerList({
    blockers,
    headingId: "workbench-blockers-title",
  }));
  const executionHtml = renderToStaticMarkup(BlockerList({
    blockers,
    headingId: "execution-recovery-blockers-title",
    title: "Execution and recovery blockers",
  }));

  assert.match(globalHtml, /aria-labelledby="workbench-blockers-title"/);
  assert.match(executionHtml, /aria-labelledby="execution-recovery-blockers-title"/);
  assert.doesNotMatch(executionHtml, /aria-labelledby="workbench-blockers-title"/);
});
