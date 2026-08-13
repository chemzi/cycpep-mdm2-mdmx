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
  fixture = JSON.parse(await readFile(
    new URL("fixtures/workbench-v2.json", import.meta.url),
    "utf8",
  )).data;
});

after(() => vite?.close());

test("the launch sheet is an honest target-first full-screen control surface", () => {
  const html = renderToStaticMarkup(createElement(ProjectLaunchSheet, { onClose() {} }));

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
  assert.match(html, /Entering a target creates a review draft\. It does not start scientific or GPU work/);
  assert.doesNotMatch(html, /preview|not connected|not implemented/i);
  assert.doesNotMatch(html, /target resolved|project approved|GPU approved/i);
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

test("launch readiness requires a target and complete automatic ceilings", () => {
  const empty = { minutes: "", slots: "", designs: "", candidates: "" };
  const complete = { minutes: "60", slots: "1", designs: "3", candidates: "8" };

  assert.equal(isLaunchRequestReady("", "manual", empty), false);
  assert.equal(isLaunchRequestReady("MDM2", "manual", empty), true);
  assert.equal(isLaunchRequestReady("MDM2", "automatic", empty), false);
  assert.equal(isLaunchRequestReady("MDM2", "automatic", complete), true);
  assert.equal(isLaunchRequestReady("MDM2", "automatic", { ...complete, slots: "0" }), false);
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
  assert.match(page, /const content = !model/);
  assert.ok(page.indexOf("{content}") < page.indexOf("{launchSheetOpen ? <ProjectLaunchSheet"));
  assert.doesNotMatch(css, /\.launch-(?:overlay|sheet|ledger)[^{]*\{[^}]*(?:--canvas|--surface|--raised|--selection)\s*:/s);
});
