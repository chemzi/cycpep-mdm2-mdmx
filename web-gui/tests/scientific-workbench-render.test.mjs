import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { after, before } from "node:test";

import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const fixtureUrl = new URL("fixtures/workbench-v2.json", import.meta.url);
let vite;
let components;
let cacheDir;

before(async () => {
  cacheDir = await mkdtemp(join(tmpdir(), "cycpep-vite-test-"));
  vite = await createServer({
    appType: "custom",
    cacheDir,
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true },
  });
  components = {
    ...(await vite.ssrLoadModule("/app/workbench/components/candidate-workspace.tsx")),
    ...(await vite.ssrLoadModule("/app/workbench/components/exploration-shortlist.tsx")),
    ...(await vite.ssrLoadModule("/app/workbench/components/evidence-provenance.tsx")),
    ...(await vite.ssrLoadModule("/app/workbench/components/artifact-trace.tsx")),
  };
});

after(async () => {
  await vite?.close();
  await rm(cacheDir, { recursive: true, force: true });
});

async function workbench() {
  const envelope = JSON.parse(await readFile(fixtureUrl, "utf8"));
  return envelope.data;
}

test("renders zero passed separately from exploration shortlist membership", async () => {
  const model = await workbench();
  const shortlist = model.evidence.items.find(
    (item) => item.event_type === "exploration_shortlist",
  );
  const html = renderToStaticMarkup(
    components.ExplorationShortlist({
      shortlist,
      evidence: model.evidence.items,
      headingId: "exploration-shortlist-0-title",
      passedHeadingId: "exploration-shortlist-0-passed-title",
      onSelectEvidence() {},
    }),
  );

  assert.match(html, /0 \/ 6 passed/);
  assert.match(html, /Exploration shortlist/);
  assert.match(html, /Shortlist membership is not a scientific pass/);
  assert.equal((html.match(/passed: false/g) ?? []).length, 2);
  assert.match(html, /Desirability/);
  assert.match(html, /Pareto front/);
  assert.match(html, /Top margin metric/);
  assert.match(html, /Calibrated/);
  assert.match(html, /Provisional/);
  assert.match(html, /<button type="button"><code>evt-battery-1<\/code>/);
  assert.match(html, /evt-battery-2/);
  assert.match(html, /unavailable in this response/);
  assert.doesNotMatch(html, /<button type="button"><code>evt-battery-2<\/code>/);
  assert.match(html, /totally_unknown/);
});

test("uses caller-provided semantic IDs for repeated shortlist panels", async () => {
  const model = await workbench();
  const shortlist = model.evidence.items.find(
    (item) => item.event_type === "exploration_shortlist",
  );
  const first = renderToStaticMarkup(components.ExplorationShortlist({
    shortlist,
    evidence: model.evidence.items,
    headingId: "exploration-shortlist-0-title",
    passedHeadingId: "exploration-shortlist-0-passed-title",
    onSelectEvidence() {},
  }));
  const second = renderToStaticMarkup(components.ExplorationShortlist({
    shortlist,
    evidence: model.evidence.items,
    headingId: "exploration-shortlist-1-title",
    passedHeadingId: "exploration-shortlist-1-passed-title",
    onSelectEvidence() {},
  }));

  assert.match(first, /aria-labelledby="exploration-shortlist-0-title"/);
  assert.match(first, /aria-labelledby="exploration-shortlist-0-passed-title"/);
  assert.match(second, /aria-labelledby="exploration-shortlist-1-title"/);
  assert.match(second, /aria-labelledby="exploration-shortlist-1-passed-title"/);
  assert.doesNotMatch(second, /exploration-shortlist-0-title/);
});

test("renders an honest empty Candidate workspace", async () => {
  const model = await workbench();
  const html = renderToStaticMarkup(
    components.CandidateWorkspace({
      candidates: { total: 0, returned: 0, truncated: false, items: [] },
      evidence: model.evidence.items,
      artifacts: model.artifacts.items,
      selectedCandidateId: null,
      onSelectCandidate() {},
    }),
  );

  assert.match(html, /No candidates returned by the Store read model/);
});

test("renders only trace-linked candidate Evidence and artifacts", async () => {
  const model = await workbench();
  const html = renderToStaticMarkup(
    components.CandidateWorkspace({
      candidates: model.candidates,
      evidence: model.evidence.items,
      artifacts: model.artifacts.items,
      selectedCandidateId: "C0001",
      onSelectCandidate() {},
    }),
  );

  assert.match(html, /Evidence: evt-battery-1/);
  assert.match(html, /Artifact: artifact-1/);
  assert.doesNotMatch(html, /Evidence: evt-shortlist/);
  assert.match(html, /historical run/);
  assert.match(html, /unlinked/);
});

test("renders Evidence as structured provenance and artifact content as contract-bound", async () => {
  const model = await workbench();
  const evidenceHtml = renderToStaticMarkup(
    components.EvidenceProvenance({
      evidence: model.evidence.items,
      selectedEvidenceId: "evt-shortlist",
      onSelectEvidence() {},
    }),
  );
  const artifactHtml = renderToStaticMarkup(
    components.ArtifactTraceInspector({
      artifacts: model.artifacts.items,
      protocols: model.protocols.items,
      selectedArtifactId: "artifact-2",
      onSelectArtifact() {},
    }),
  );

  assert.match(evidenceHtml, /STRUCTURED PROVENANCE/);
  assert.match(evidenceHtml, /critic/);
  assert.match(evidenceHtml, /MDM2, MDMX/);
  assert.match(evidenceHtml, /protocol-exploration-v1/);
  assert.match(evidenceHtml, /parent_event_id/);
  assert.match(artifactHtml, /Content unavailable: no formal content_link returned/);
  assert.doesNotMatch(artifactHtml, /\/api\/v1/);
  assert.doesNotMatch(artifactHtml, /coordinates/);
});
