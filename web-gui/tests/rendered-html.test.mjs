import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the honest Frontend V2 initial state", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>CycPep Studio — Frontend V2 Workbench<\/title>/i);
  assert.match(html, /Loading Frontend V2 workbench/);
  assert.match(html, /role="status"/);
  assert.doesNotMatch(html, /example candidate|fake progress|demo molecule/i);
});

test("keeps the root page thin and removes legacy workflow authorities", async () => {
  const [page, workbenchPage, client, structureViewer] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/workbench/workbench-page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/workbench/client.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/workbench/components/structure-viewer.tsx", import.meta.url), "utf8"),
  ]);

  assert.ok(page.split(/\r?\n/).length <= 8, "page.tsx must remain a thin composition root");
  assert.match(page, /<WorkbenchPage \/>/);
  assert.match(client, /"\/api\/v2\/workbench"/);

  const productionSource = `${page}\n${workbenchPage}\n${client}\n${structureViewer}`;
  assert.doesNotMatch(productionSource, /\/api\/v1\/snapshot|State\.phase|const\s+AGENTS\b/);
  assert.doesNotMatch(productionSource, /connections\/ssh|project-drafts|GPU QUEUE/);
  assert.doesNotMatch(structureViewer, /\/api\/v1\/artifacts|artifacts\/\$\{/);
  assert.match(structureViewer, /contentLink/);
});
