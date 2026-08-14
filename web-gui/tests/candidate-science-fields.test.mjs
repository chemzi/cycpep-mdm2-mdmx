import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { after, before } from "node:test";

import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

import {
  WorkbenchContractError,
  parseWorkbenchEnvelope,
} from "../app/workbench/client.ts";
import { buildC0006ProductionEnvelope } from "./fixtures/workbench-c0006-production.mjs";

let vite;
let CandidateWorkspace;
let cacheDir;
let envelope;

before(async () => {
  const base = JSON.parse(await readFile(
    new URL("fixtures/workbench-v2.json", import.meta.url),
    "utf8",
  ));
  envelope = buildC0006ProductionEnvelope(base);
  cacheDir = await mkdtemp(join(tmpdir(), "cycpep-candidate-science-"));
  vite = await createServer({
    appType: "custom",
    cacheDir,
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true },
  });
  ({ CandidateWorkspace } = await vite.ssrLoadModule(
    "/app/workbench/components/candidate-workspace.tsx",
  ));
});

after(async () => {
  await vite?.close();
  await rm(cacheDir, { recursive: true, force: true });
});

test("the parser accepts and validates the optional candidate association summary", () => {
  const parsed = parseWorkbenchEnvelope(envelope);
  const candidate = parsed.data.candidates.items.find(
    (item) => item.candidate_id === "C0006",
  );
  assert.equal(candidate.associations.evidence_total, 10);
  assert.equal(candidate.associations.artifact_ids.length, 8);
  assert.equal(candidate.associations.status_owner.run_relation, "current_run");

  const malformed = structuredClone(envelope);
  malformed.data.candidates.items[5].associations.artifact_total = "8";
  assert.throws(
    () => parseWorkbenchEnvelope(malformed),
    (error) => error instanceof WorkbenchContractError
      && error.message.includes("candidates.items[5].associations.artifact_total"),
  );
});

test("C0006 renders exact formal science despite bounded project collections", () => {
  const data = parseWorkbenchEnvelope(envelope).data;
  const html = renderToStaticMarkup(CandidateWorkspace({
    candidates: data.candidates,
    evidence: data.evidence,
    artifacts: data.artifacts,
    selectedCandidateId: "C0006",
    onSelectCandidate() {},
  }));

  assert.match(html, /GSLALESLAG/);
  assert.match(html, /needs_optimization/);
  assert.match(html, /L2_ipsae_mdm2/);
  assert.match(html, /L7_post_relax_interface_energy/);
  assert.match(html, /Status-owning run/);
  assert.match(html, /run-1/);
  assert.match(html, /current run/);
  assert.match(html, /10 Evidence · 8 artifacts · complete/);
  assert.match(html, /Structure artifact recorded/);
  assert.match(html, /artifact-c0006-post-relax/);
  assert.match(html, /artifact-c0006-boltz/);
  assert.match(html, /prediction_input:global.post_relax_pdb/);
  assert.match(html, /Recorded in the formal Store; browser preview was not published\./);
  assert.match(html, /evt-shortlist-c0006/);
  assert.match(html, /retained_for_round_2/);
  assert.doesNotMatch(html, /No metrics returned/);
  assert.doesNotMatch(html, /No returned shortlist explicitly references this candidate/);
  assert.doesNotMatch(html, /No trace-linked structure artifact returned/);
  assert.doesNotMatch(html, /0 artifacts/);
});

test("legacy candidates qualify absence when a bounded association window is truncated", () => {
  const data = parseWorkbenchEnvelope(envelope).data;
  const legacy = structuredClone(data);
  legacy.candidates.items[0].candidate_id = "C0099";
  legacy.candidates.items[0].trace.candidate_id = "C0099";
  const html = renderToStaticMarkup(CandidateWorkspace({
    candidates: legacy.candidates,
    evidence: legacy.evidence,
    artifacts: legacy.artifacts,
    selectedCandidateId: "C0099",
    onSelectCandidate() {},
  }));

  assert.match(html, /Evidence window: 100 of 112 returned/);
  assert.match(html, /Artifact window: 100 of 108 returned/);
  assert.match(html, /Additional associations may be omitted/);
  assert.doesNotMatch(html, /No returned shortlist explicitly references this candidate/);
  assert.doesNotMatch(html, /No trace-linked structure artifact returned/);
  assert.doesNotMatch(html, />0 Evidence · 0 artifacts</);
});

test("malformed formal metrics are described as present but unreadable", () => {
  const data = parseWorkbenchEnvelope(envelope).data;
  const malformed = structuredClone(data);
  const candidate = malformed.candidates.items[5];
  delete candidate.metrics;
  candidate.associations.complete = false;
  candidate.associations.limitations = [{
    code: "candidate_metrics_malformed",
    summary: "Candidate metrics are present but could not be read.",
  }];
  const html = renderToStaticMarkup(CandidateWorkspace({
    candidates: malformed.candidates,
    evidence: malformed.evidence,
    artifacts: malformed.artifacts,
    selectedCandidateId: "C0006",
    onSelectCandidate() {},
  }));

  assert.match(html, /Formal metrics are present but could not be read/);
  assert.doesNotMatch(html, /No formal metrics are recorded/);
});
