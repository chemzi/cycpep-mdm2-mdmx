import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test, { after } from "node:test";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import { tmpdir } from "node:os";

import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { createServer } from "vite";

const root = fileURLToPath(new URL("../", import.meta.url));
const server = await createServer({
  root,
  appType: "custom",
  logLevel: "silent",
  configFile: false,
  cacheDir: join(tmpdir(), `cycpep-vite-workflow-${process.pid}`),
  server: { middlewareMode: true },
});
after(() => server.close());

const { TaskGraph } = await server.ssrLoadModule("/app/workbench/components/task-graph.tsx");
const { ExecutionTransactionDetail, correlateTaskAttempts } = await server.ssrLoadModule(
  "/app/workbench/components/execution-transaction.tsx",
);
const data = JSON.parse(await readFile(new URL("fixtures/workbench-v2.json", import.meta.url), "utf8")).data;

test("renders returned dependency edges and typed action availability without a fixed pipeline", () => {
  const html = renderToStaticMarkup(createElement(TaskGraph, {
    tasks: data.tasks,
    executions: data.executions,
    transactions: data.transactions,
    blockers: data.blockers.items,
  }));

  assert.match(html, /Task \/ Action graph/);
  assert.match(html, /T002/);
  assert.match(html, /Depends on:<\/strong> T001/);
  assert.match(html, /predict_structures/);
  assert.match(html, /Approval required<\/dt><dd>true/);
  assert.match(html, /approval_required/);
  assert.match(html, /Task blockers/);
  assert.match(html, /scientific_input_invalid/);
  assert.match(html, /tasks are not mapped to a fixed Agent pipeline/i);
  assert.doesNotMatch(html, /Research → Design → Prediction → Critic/);
});

test("correlates transactions only by returned task and attempt identities", () => {
  const attempts = correlateTaskAttempts("T003", data.executions.items, [
    ...data.transactions.items,
    { transaction_id: "wrong-attempt", task_id: "T003", attempt_id: "T003-A99", status: "COMMITTED" },
    { transaction_id: "wrong-task", task_id: "T999", attempt_id: "T003-A01", status: "COMMITTED" },
  ]);

  assert.equal(attempts.length, 1);
  assert.deepEqual(attempts[0].transactions.map((item) => item.transaction_id), ["tx-3"]);
});

test("presents not-yet-recorded, structured failure, and unresolved recovery truth", () => {
  const runningHtml = renderToStaticMarkup(ExecutionTransactionDetail({
    task: data.tasks.items[1],
    executions: data.executions.items,
    transactions: data.transactions.items,
    blockers: data.blockers.items,
  }));
  assert.match(runningHtml, /not yet recorded/);
  assert.match(runningHtml, /No transaction record exists/);
  assert.doesNotMatch(runningHtml, />pending</i);

  const failedHtml = renderToStaticMarkup(ExecutionTransactionDetail({
    task: data.tasks.items[2],
    executions: data.executions.items,
    transactions: data.transactions.items,
    blockers: data.blockers.items,
  }));
  assert.match(failedHtml, /scientific_input_invalid/);
  assert.match(failedHtml, /Input was rejected/);
  assert.match(failedHtml, /COMPENSATION_CONFLICT/);
  assert.match(failedHtml, /transaction_compensation_unresolved/);
  assert.doesNotMatch(failedHtml, /stdout|log console/i);
});

test("preserves returned committed, failed, and rolled-back lifecycle labels", () => {
  const task = data.tasks.items[0];
  const execution = data.executions.items[0];
  const renderStatus = (status) => renderToStaticMarkup(ExecutionTransactionDetail({
    task,
    executions: [{ ...execution, transaction_visibility: status }],
    transactions: [{
      ...data.transactions.items[0],
      status,
    }],
    blockers: [],
  }));

  assert.match(renderStatus("COMMITTED"), /COMMITTED/);
  assert.match(renderStatus("FAILED"), /FAILED/);
  assert.match(renderStatus("ROLLED_BACK"), /ROLLED_BACK/);
});

test("shows an honest empty graph for an unavailable current run", () => {
  const html = renderToStaticMarkup(createElement(TaskGraph, {
    tasks: { scope: "current_run", total: 0, returned: 0, truncated: false, items: [] },
    executions: { scope: "current_run", total: 0, returned: 0, truncated: false, items: [] },
    transactions: { scope: "current_run", total: 0, returned: 0, truncated: false, items: [] },
    blockers: [],
  }));
  assert.match(html, /No trustworthy current-run task graph/);
  assert.match(html, /0 returned \/ 0 total/);
});
