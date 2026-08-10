import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  reconcileWorkbenchSelection,
} from "../app/workbench/selection.ts";

const fixtureUrl = new URL("fixtures/workbench-v2.json", import.meta.url);

async function model() {
  return JSON.parse(await readFile(fixtureUrl, "utf8")).data;
}

test("selection stores only a returned subject identity", async () => {
  const data = await model();
  const selection = reconcileWorkbenchSelection(
    { kind: "candidate", identity: "C0003" },
    data,
  );

  assert.deepEqual(selection, { kind: "candidate", identity: "C0003" });
  assert.deepEqual(Object.keys(selection).sort(), ["identity", "kind"]);
});

test("a bounded candidate selection falls back once and cannot resurrect", async () => {
  const data = await model();
  const removed = structuredClone(data);
  removed.candidates.items = removed.candidates.items.filter(
    (candidate) => candidate.candidate_id !== "C0003",
  );
  removed.candidates.returned = removed.candidates.items.length;

  const afterRemoval = reconcileWorkbenchSelection(
    { kind: "candidate", identity: "C0003" },
    removed,
  );
  assert.deepEqual(afterRemoval, { kind: "candidate", identity: "C0001" });

  const afterReturn = reconcileWorkbenchSelection(afterRemoval, data);
  assert.deepEqual(afterReturn, { kind: "candidate", identity: "C0001" });
});

test("an unavailable current-run subject falls back to overview", async () => {
  const data = await model();
  const withoutRun = structuredClone(data);
  withoutRun.workflow = null;
  withoutRun.run = null;
  withoutRun.tasks = {
    scope: "current_run",
    total: 0,
    returned: 0,
    truncated: false,
    items: [],
  };

  assert.deepEqual(
    reconcileWorkbenchSelection({ kind: "task", identity: "T002" }, withoutRun),
    { kind: "overview", identity: null },
  );
});
