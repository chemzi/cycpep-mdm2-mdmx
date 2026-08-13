import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { after, before } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let vite;
let ApprovalControlCard;
let WorkbenchWorkspace;
let fixture;

const approval = {
  launcher_run_id: "launcher_0123456789abcdef0123456789abcdef",
  project_id: "project-1",
  approved_content_binding: "a".repeat(64),
  plan_id: "planner_0123456789ab",
  plan_sha256: "b".repeat(64),
  source_kind: "initial_prediction_bootstrap",
  required_task_ids: ["T001"],
  tasks: [{
    task_id: "T001",
    action: "evaluate_new_design_candidates",
    resource_class: "gpu",
    gpu_job_slots: 1,
    proposal_count: 3,
    candidate_limit: 8,
    estimated_gpu_minutes: 42.5,
    estimate_status: "estimated",
    estimator_version: "simple-v1",
    calibration_status: "provisional",
  }],
  budget: {
    gpu_minutes: 42.5,
    gpu_minutes_status: "estimated",
    estimator_version: "simple-v1",
    calibration_status: "provisional",
  },
};

const request = {
  launcher_run_id: approval.launcher_run_id,
  project_id: approval.project_id,
  approved_content_binding: approval.approved_content_binding,
  plan_id: approval.plan_id,
  plan_sha256: approval.plan_sha256,
  required_task_ids: approval.required_task_ids,
  approver: "Demo operator",
  justification: "Reviewed exact task and compute ceilings.",
  ceilings: {
    max_gpu_job_slots: 1,
    max_gpu_minutes: 60,
    max_design_proposals: 3,
    max_prediction_candidates: 8,
  },
};

before(async () => {
  vite = await createServer({
    appType: "custom",
    cacheDir: join(tmpdir(), `cycpep-vite-approval-card-${process.pid}`),
    configFile: false,
    logLevel: "silent",
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true },
  });
  ({ ApprovalControlCard } = await vite.ssrLoadModule(
    "/app/workbench/components/approval-control-card.tsx",
  ));
  ({ WorkbenchWorkspace } = await vite.ssrLoadModule(
    "/app/workbench/components/workbench-workspace.tsx",
  ));
  fixture = JSON.parse(await readFile(
    new URL("fixtures/workbench-v2.json", import.meta.url),
    "utf8",
  )).data;
});

after(() => vite?.close());

function renderCard(overrides = {}) {
  return renderToStaticMarkup(createElement(ApprovalControlCard, {
    approval,
    request,
    autoApprovalEligible: false,
    ...overrides,
  }));
}

test("exact approval card renders plan binding, resources, budget, and manual ceilings", () => {
  const html = renderCard();

  assert.match(html, /Exact plan approval/);
  assert.match(html, /planner_0123456789ab/);
  assert.match(html, new RegExp("b{64}"));
  assert.match(html, /T001/);
  assert.match(html, /evaluate_new_design_candidates/);
  assert.match(html, /42\.5 GPU-min/);
  assert.match(html, /simple-v1/);
  assert.match(html, /provisional/);
  for (const label of ["Approver", "Justification", "GPU slots", "GPU minutes", "Design proposals", "Prediction candidates"])
    assert.match(html, new RegExp(label));
  assert.match(html, /Approve and continue/);
  assert.doesNotMatch(html, /Auto-approve first GPU gate/);
});

test("auto option is controlled only by eligibility prop and unavailable estimates disable it", () => {
  const eligible = renderCard({ autoApprovalEligible: true });
  assert.match(eligible, /Auto-approve first GPU gate/);
  assert.doesNotMatch(eligible, /type="checkbox"[^>]*disabled/);

  const unavailableApproval = {
    ...approval,
    source_kind: "initial_prediction_bootstrap",
    tasks: [{
      ...approval.tasks[0],
      estimated_gpu_minutes: null,
      estimate_status: "benchmark_required",
      estimator_version: null,
      calibration_status: "unavailable",
    }],
    budget: {
      gpu_minutes: null,
      gpu_minutes_status: "benchmark_required",
      estimator_version: null,
      calibration_status: "unavailable",
    },
  };
  const unavailable = renderCard({
    approval: unavailableApproval,
    autoApprovalEligible: true,
  });
  assert.match(unavailable, /Pending benchmark/);
  assert.match(unavailable, /type="checkbox"[^>]*disabled/);
  assert.match(unavailable, /Approve and continue[^]*?disabled|disabled[^]*?Approve and continue/);
});

test("workspace inserts the optional card first without changing legacy callers", () => {
  const withCard = renderToStaticMarkup(createElement(WorkbenchWorkspace, {
    data: fixture,
    requestStatus: "ready",
    refreshError: null,
    autoRefreshEnabled: true,
    onRefresh() {},
    onAutoRefreshChange() {},
    approvalControl: approval,
    manualApprovalRequest: request,
  }));
  const withoutCard = renderToStaticMarkup(createElement(WorkbenchWorkspace, {
    data: fixture,
    requestStatus: "ready",
    refreshError: null,
    autoRefreshEnabled: true,
    onRefresh() {},
    onAutoRefreshChange() {},
  }));

  assert.ok(withCard.indexOf("Exact plan approval") < withCard.indexOf("Current scientific run"));
  assert.doesNotMatch(withoutCard, /Exact plan approval/);
  for (const region of ["Workbench navigator", "Selected workspace", "Workbench inspector", "Workbench history"])
    assert.match(withoutCard, new RegExp(region));
});

test("approval styles remain scoped and preserve the WorkbenchFrame grid", async () => {
  const css = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");

  assert.match(css, /\.approval-control-card\s*\{[^}]*border-left:\s*3px solid var\(--selection\)/s);
  assert.match(css, /\.approval-control-body\s*\{[^}]*grid-template-columns:/s);
  assert.match(css, /@media\s*\(max-width:\s*900px\)[\s\S]*\.approval-control-body\s*\{[^}]*grid-template-columns:\s*1fr/s);
  assert.match(css, /\.workbench-frame\s*\{[^}]*grid-template-areas:[^}]*navigator primary inspector[^}]*history history history/s);
  assert.doesNotMatch(css, /\.approval-[^{]*\{[^}]*(?:--canvas|--surface|--selection)\s*:/s);
});
