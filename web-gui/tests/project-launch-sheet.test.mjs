import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { after, before } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let vite;
let ProjectLaunchSheet;
let isLaunchRequestReady;
let WorkbenchWorkspace;
let createEmptyMonitoringModel;
let fixture;

before(async () => {
  vite = await createServer({
    appType: "custom",
    cacheDir: join(tmpdir(), `cycpep-vite-launch-sheet-${process.pid}`),
    configFile: false,
    logLevel: "silent",
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true },
  });
  ({ ProjectLaunchSheet, isLaunchRequestReady } = await vite.ssrLoadModule(
    "/app/workbench/components/project-launch-sheet.tsx",
  ));
  ({ WorkbenchWorkspace } = await vite.ssrLoadModule(
    "/app/workbench/components/workbench-workspace.tsx",
  ));
  ({ createEmptyMonitoringModel } = await vite.ssrLoadModule(
    "/app/workbench/demo-monitoring-model.ts",
  ));
  fixture = JSON.parse(await readFile(
    new URL("fixtures/workbench-v2.json", import.meta.url),
    "utf8",
  )).data;
});

after(() => vite?.close());

test("the launch sheet is an honest target-first full-screen control surface", () => {
  const html = renderToStaticMarkup(createElement(ProjectLaunchSheet, { onClose() {}, onLaunch() {} }));

  assert.match(html, /role="dialog"/);
  assert.match(html, /aria-modal="true"/);
  assert.match(html, /Start with a target/);
  assert.match(html, /e\.g\. MDM2, Q00987 or 1YCR/);
  assert.match(html, /View existing tasks/);
  assert.match(html, /Close new project/);
  assert.match(html, /Auto-approve first GPU gate/);
  assert.match(html, /Initial Design/);
  assert.match(html, /heavy Prediction/);
  assert.match(html, /Create and launch/);
  assert.match(html, /Resolve target/);
  assert.match(html, /Organism ID/);
  assert.match(html, /Objective/);
  assert.match(html, /Approver/);
  assert.match(html, /Justification/);
  assert.match(html, /Resolve and approve the project before launch/);
  assert.match(html, /Create and launch[^]*?disabled|disabled[^]*?Create and launch/);
  assert.doesNotMatch(html, /preview|not connected|not implemented/i);
  assert.doesNotMatch(html, /target resolved|project approved|GPU approved/i);
});

test("a launched target enters the normal workbench with an empty monitoring model", () => {
  const model = createEmptyMonitoringModel("MDM2");
  const html = renderToStaticMarkup(WorkbenchWorkspace({
    data: model,
    requestStatus: "ready",
    refreshError: null,
    autoRefreshEnabled: true,
    onNewProject() {},
    onRefresh() {},
    onAutoRefreshChange() {},
  }));

  assert.match(html, /MDM2 cyclic peptide campaign/);
  assert.match(html, /No active run/);
  assert.match(html, /Candidates returned<\/dt><dd>0/);
  assert.match(html, /Evidence returned<\/dt><dd>0/);
  assert.doesNotMatch(html, /C0001|T001|run-1/);
});

test("the compact New project entry is immediately before Refresh", () => {
  const html = renderToStaticMarkup(WorkbenchWorkspace({
    data: fixture,
    requestStatus: "ready",
    refreshError: null,
    autoRefreshEnabled: true,
    onNewProject() {},
    onRefresh() {},
    onAutoRefreshChange() {},
  }));

  const newProject = html.indexOf("New project");
  const autoRefresh = html.indexOf("Auto refresh");
  const refresh = html.indexOf(">Refresh</button>");
  assert.ok(autoRefresh > -1 && autoRefresh < newProject && newProject < refresh);
  for (const region of [
    'aria-label="Workbench navigator"',
    'aria-label="Selected workspace"',
    'aria-label="Workbench inspector"',
    'aria-label="Workbench history"',
  ]) assert.match(html, new RegExp(region));
});

test("launch readiness requires approved review, identity, and valid complete ceilings", () => {
  const empty = { max_gpu_minutes: "", max_gpu_job_slots: "", max_design_proposals: "", max_prediction_candidates: "" };
  const complete = { max_gpu_minutes: "60", max_gpu_job_slots: "0", max_design_proposals: "0", max_prediction_candidates: "8" };

  assert.equal(isLaunchRequestReady("MDM2", "manual", complete, false, "PI", "Reviewed"), false);
  assert.equal(isLaunchRequestReady("MDM2", "manual", complete, true, "", "Reviewed"), false);
  assert.equal(isLaunchRequestReady("MDM2", "manual", complete, true, "PI", "Reviewed"), true);
  assert.equal(isLaunchRequestReady("MDM2", "automatic", empty, true, "PI", "Reviewed"), false);
  assert.equal(isLaunchRequestReady("MDM2", "automatic", complete, true, "PI", "Reviewed"), true);
  assert.equal(isLaunchRequestReady("MDM2", "automatic", { ...complete, max_gpu_minutes: "0" }, true, "PI", "Reviewed"), false);
  assert.equal(isLaunchRequestReady("MDM2", "automatic", { ...complete, max_gpu_job_slots: "-1" }, true, "PI", "Reviewed"), false);
});

test("server review projection controls the formal approval stage", () => {
  const readyReview = {
    draft_id: "draft-1", project_id: "project-1", name: "MDM2 campaign",
    target_identifier: "MDM2", resolved_identity: "MDM2 / Q00987",
    structure_status: "Structure ready", review_status: "ready",
    blockers: [], uncertainties: ["Isoform selection retained for review"],
  };
  const ready = renderToStaticMarkup(createElement(ProjectLaunchSheet, {
    onClose() {}, review: readyReview, onApproveDraft() {},
    initialRequest: { target_identifier: "MDM2", options: { identifier_type: "gene", organism_id: 9606, epitope: null, objective: "binder", launcher_run_id: null, first_gate_auto_policy: null } },
  }));
  const blocked = renderToStaticMarkup(createElement(ProjectLaunchSheet, {
    onClose() {}, review: { ...readyReview, review_status: "review_required", blockers: ["Structure unresolved"] },
    initialRequest: { target_identifier: "MDM2", options: { identifier_type: "gene", organism_id: 9606, epitope: null, objective: "binder", launcher_run_id: null, first_gate_auto_policy: null } },
  }));

  assert.match(ready, /MDM2 campaign/);
  assert.match(ready, /MDM2 \/ Q00987/);
  assert.match(ready, /Approve project/);
  assert.match(ready, /Isoform selection retained for review/);
  assert.doesNotMatch(blocked, /Approve project/);
  assert.match(blocked, /Review blockers: Structure unresolved/);
});

test("launch styles are scoped and preserve the existing frame contract", async () => {
  const css = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");
  const page = await readFile(new URL("../app/workbench/workbench-page.tsx", import.meta.url), "utf8");

  assert.match(css, /\.launch-overlay\s*\{[^}]*position:\s*fixed[^}]*inset:\s*0[^}]*z-index:\s*12/s);
  assert.match(css, /\.launch-ledger\s*\{[^}]*grid-template-columns:\s*1\.12fr\s+\.9fr\s+1fr/s);
  assert.match(css, /@media\s*\(max-width:\s*900px\)[\s\S]*\.launch-ledger\s*\{[^}]*grid-template-columns:\s*1fr/s);
  assert.match(css, /\.workbench-frame\s*\{[^}]*grid-template-areas:[^}]*navigator primary inspector[^}]*history history history/s);
  assert.match(page, /sessionStorage\.getItem\(LAUNCH_SHEET_DISMISSED_KEY\)/);
  assert.match(page, /<ProjectLaunchSheet/);
  assert.match(page, /const content = !displayedModel/);
  assert.ok(page.indexOf("{content}") < page.indexOf("{launchSheetOpen ? <ProjectLaunchSheet"));
  assert.doesNotMatch(css, /\.launch-(?:overlay|sheet|ledger)[^{]*\{[^}]*(?:--canvas|--surface|--raised|--selection)\s*:/s);
});
