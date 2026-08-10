import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { parseWorkbenchEnvelope } from "../app/workbench/client.ts";
import {
  initialWorkbenchRequestState,
  workbenchRequestReducer,
} from "../app/workbench/request-lifecycle.ts";

async function workbench() {
  const raw = await readFile(new URL("fixtures/workbench-v2.json", import.meta.url), "utf8");
  return parseWorkbenchEnvelope(JSON.parse(raw));
}

test("starts in initial loading and becomes ready after the first success", async () => {
  const data = await workbench();
  const loading = initialWorkbenchRequestState();
  const ready = workbenchRequestReducer(loading, { type: "succeeded", data });

  assert.deepEqual(loading, { status: "initial-loading", data: null, error: null });
  assert.deepEqual(ready, { status: "ready", data, error: null });
});

test("successful refresh keeps data visible while refreshing and replaces it on success", async () => {
  const data = await workbench();
  const ready = workbenchRequestReducer(initialWorkbenchRequestState(), {
    type: "succeeded",
    data,
  });
  const refreshing = workbenchRequestReducer(ready, { type: "started" });
  const updated = structuredClone(data);
  updated.request_id = "req_refreshed";
  const refreshed = workbenchRequestReducer(refreshing, { type: "succeeded", data: updated });

  assert.deepEqual(refreshing, { status: "refreshing", data, error: null });
  assert.deepEqual(refreshed, { status: "ready", data: updated, error: null });
});

test("a failure before data is a failed-before-data state", () => {
  const failed = workbenchRequestReducer(initialWorkbenchRequestState(), {
    type: "failed",
    error: "Workbench request failed",
  });

  assert.deepEqual(failed, {
    status: "failed-before-data",
    data: null,
    error: "Workbench request failed",
  });
});

test("a refresh failure preserves the last successful response as stale", async () => {
  const data = await workbench();
  const ready = workbenchRequestReducer(initialWorkbenchRequestState(), {
    type: "succeeded",
    data,
  });
  const refreshing = workbenchRequestReducer(ready, { type: "started" });
  const stale = workbenchRequestReducer(refreshing, {
    type: "failed",
    error: "Refresh failed",
  });

  assert.deepEqual(stale, {
    status: "stale-after-error",
    data,
    error: "Refresh failed",
  });
});
