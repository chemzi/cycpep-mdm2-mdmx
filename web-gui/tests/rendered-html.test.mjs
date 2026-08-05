import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const templateRoot = new URL("../", import.meta.url);

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

test("server-renders the CycPep Studio workbench", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>CycPep Studio — 双靶环肽设计台<\/title>/);
  assert.match(html, /AI DRUG DISCOVERY WORKBENCH/);
  assert.match(html, /AGENT WORKFLOW/);
  assert.match(html, /SEVEN-LAYER BATTERY/);
  assert.match(html, /连接真实工作环境/);
});

test("defaults the browser to the JSON adapter instead of the frontend route", async () => {
  const page = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
  assert.match(page, /apiBase:\s*"http:\/\/127\.0\.0\.1:8765\/api\/v1"/);
  assert.match(page, /该地址没有提供 CycPep JSON 数据服务/);
  assert.match(page, /body\.data/);
});

test("does not carry the retired starter preview into the workbench", async () => {
  const page = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(page, /SkeletonPreview|codex-preview|_sites-preview/);
  await assert.rejects(
    readFile(new URL("../app/_sites-preview/SkeletonPreview.tsx", templateRoot)),
  );
});
