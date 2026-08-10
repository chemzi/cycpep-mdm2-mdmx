import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  artifactContentState,
  candidateArtifacts,
  candidateEvidence,
  explorationShortlistPresentation,
  traceEntries,
} from "../app/workbench/scientific-selectors.ts";

const fixtureUrl = new URL("fixtures/workbench-v2.json", import.meta.url);

async function workbench() {
  const envelope = JSON.parse(await readFile(fixtureUrl, "utf8"));
  return envelope.data;
}

test("candidate associations use only formal trace candidate identifiers", async () => {
  const model = await workbench();

  assert.deepEqual(
    candidateEvidence("C0001", model.evidence.items).map((item) => item.event_id),
    ["evt-battery-1"],
  );
  assert.deepEqual(
    candidateArtifacts("C0001", model.artifacts.items).map((item) => item.artifact_id),
    ["artifact-1"],
  );
  assert.equal(
    candidateEvidence("C0001", model.evidence.items).some(
      (item) => item.event_id === "evt-shortlist",
    ),
    false,
    "shortlist payload membership is not a formal candidate trace link",
  );
});

test("exploration shortlist presentation preserves the frozen scientific payload", async () => {
  const model = await workbench();
  const shortlist = model.evidence.items.find(
    (item) => item.event_type === "exploration_shortlist",
  );
  const presentation = explorationShortlistPresentation(
    shortlist,
    model.evidence.items,
  );

  assert.equal(presentation.passedSummary, "0 / 6 passed");
  assert.deepEqual(presentation.shortlist, [
    {
      candidate_id: "C0001",
      passed: false,
      desirability: 0.25,
      pareto_front: true,
      reason: "pareto_front",
      top_margin_metric: "L2_ipsae_mdm2",
    },
    {
      candidate_id: "C0002",
      passed: false,
      desirability: null,
      pareto_front: false,
      reason: "partial_evidence",
      top_margin_metric: null,
    },
  ]);
  assert.deepEqual(presentation.calibration, {
    calibrated: 1,
    provisional: 1,
    unavailable: 1,
  });
  assert.deepEqual(
    presentation.sourceEvents.map(({ eventId, evidence }) => [eventId, evidence?.event_id ?? null]),
    [["evt-battery-1", "evt-battery-1"], ["evt-battery-2", null]],
  );
  assert.deepEqual(presentation.unmappedMetrics, ["totally_unknown"]);
});

test("artifact content remains unavailable without an explicit content_link", async () => {
  const model = await workbench();
  const linked = model.artifacts.items.find(
    (item) => item.artifact_id === "artifact-1",
  );
  const unlinked = model.artifacts.items.find(
    (item) => item.artifact_id === "artifact-2",
  );

  assert.deepEqual(artifactContentState(linked), {
    available: true,
    contentLink: "/api/v2/artifacts/artifact-1/content",
  });
  assert.deepEqual(artifactContentState(unlinked), {
    available: false,
    contentLink: null,
  });
  assert.deepEqual(
    traceEntries(linked.trace).find(([name]) => name === "candidate_id"),
    ["candidate_id", "C0001"],
  );
});
