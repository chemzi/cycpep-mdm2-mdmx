import assert from "node:assert/strict";
import test from "node:test";

import { reconcileSelection } from "../app/workbench/selection.ts";
import { beginWorkbenchRequest } from "../app/workbench/request-lifecycle.ts";

test("an unavailable bounded selection cannot resurrect on a later refresh", () => {
  const afterRemoval = reconcileSelection("C3", ["C1", "C2"]);
  assert.equal(afterRemoval, "C1");

  const afterReturn = reconcileSelection(afterRemoval, ["C1", "C3"]);
  assert.equal(afterReturn, "C1");
});

test("automatic polling skips an active request without aborting it", () => {
  const active = new AbortController();
  const next = beginWorkbenchRequest(active, "automatic");

  assert.equal(next, null);
  assert.equal(active.signal.aborted, false);
});

test("manual refresh can replace an active request", () => {
  const active = new AbortController();
  const next = beginWorkbenchRequest(active, "manual");

  assert.ok(next instanceof AbortController);
  assert.notEqual(next, active);
  assert.equal(active.signal.aborted, true);
});
