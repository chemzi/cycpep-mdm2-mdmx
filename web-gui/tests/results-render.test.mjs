import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { after, before } from "node:test";

import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const fixtureUrl = new URL("fixtures/results-v1.json", import.meta.url);
let vite;
let ResultsSummary;
let cacheDir;

before(async () => {
  cacheDir = await mkdtemp(join(tmpdir(), "cycpep-results-vite-test-"));
  vite = await createServer({
    appType: "custom",
    cacheDir,
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true },
  });
  ResultsSummary = (
    await vite.ssrLoadModule("/app/workbench/components/results-summary.tsx")
  ).ResultsSummary;
});

after(async () => {
  await vite?.close();
  await rm(cacheDir, { recursive: true, force: true });
});

async function loadDigest() {
  return JSON.parse(await readFile(fixtureUrl, "utf8"));
}

test("renders hard-clearance summary, finalists, layers, and the data-basis disclaimer", async () => {
  const html = renderToStaticMarkup(
    ResultsSummary({ digest: await loadDigest(), refreshing: false }),
  );

  assert.match(html, /Results digest/);
  assert.match(html, /Demo fixture \(synthetic\)/);
  assert.match(html, /Candidates total/);
  assert.match(html, /Hard cleared/);
  assert.match(html, /50%/);
  assert.match(html, /C0101/);
  assert.match(html, /C0104/);
  assert.match(html, /l4_pass/);
  assert.match(html, /L1_plddt/);
  assert.match(html, /L7_scrmsd/);
  assert.match(html, /Battery layers/);
  assert.match(html, /Calibrated/);
  assert.match(html, /Conclusion/);
  assert.match(html, /demo fixture data \(synthetic\)/);
});

test("renders an honest empty state when there are no finalists", async () => {
  const digest = await loadDigest();
  digest.finalists = [];
  digest.summary.candidates_evaluated = 0;
  digest.summary.hard_cleared = 0;
  digest.summary.hard_clearance_rate = null;
  const html = renderToStaticMarkup(ResultsSummary({ digest }));

  assert.match(html, /No evaluated candidates yet/);
  assert.match(html, /n\/a/);
});

test("renders a failure note instead of crashing when the digest is unavailable", async () => {
  const html = renderToStaticMarkup(
    ResultsSummary({ digest: null, error: "boom", refreshing: false }),
  );
  assert.match(html, /Results digest unavailable/);
  assert.match(html, /boom/);
});
