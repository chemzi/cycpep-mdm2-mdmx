import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  ControlContractError,
  parseApprovalControlProjection,
  parseControlFailure,
  parseManualApprovalRequest,
  parseProjectLaunchRequest,
} from "../app/workbench/control-contract.ts";

const fixtureUrl = new URL("fixtures/control-models.json", import.meta.url);

async function fixture() {
  return JSON.parse(await readFile(fixtureUrl, "utf8"));
}

test("parses the frozen launch and exact manual approval request contracts", async () => {
  const source = await fixture();

  assert.equal(parseProjectLaunchRequest(source.launch_request), source.launch_request);
  assert.equal(
    parseManualApprovalRequest(source.manual_approval_request),
    source.manual_approval_request,
  );
  assert.equal(source.manual_approval_request.ceilings.max_design_proposals, 0);
});

test("preserves provisional and unavailable Planner estimate states exactly", async () => {
  const source = await fixture();
  const provisional = parseApprovalControlProjection(
    source.approval_projection_provisional,
  );
  const unavailable = parseApprovalControlProjection(
    source.approval_projection_unavailable,
  );

  assert.equal(provisional.tasks[0].estimated_gpu_minutes, 2.5);
  assert.equal(provisional.tasks[0].calibration_status, "provisional");
  assert.equal(provisional.budget.estimator_version, "simple-v1");
  assert.equal(unavailable.tasks[0].estimated_gpu_minutes, null);
  assert.equal(unavailable.tasks[0].estimate_status, "benchmark_required");
  assert.equal(unavailable.budget.gpu_minutes, null);
});

test("rejects malformed bindings, inconsistent estimates, and extra unsafe fields", async () => {
  const source = await fixture();
  const cases = [
    ["launcher_run_id", (value) => { value.launcher_run_id = "../launcher_bad"; }],
    ["plan_sha256", (value) => { value.plan_sha256 = "not-a-digest"; }],
    ["required_task_ids", (value) => { value.required_task_ids = ["T002"]; }],
    ["estimated_gpu_minutes", (value) => {
      value.tasks[0].estimated_gpu_minutes = 3;
      value.tasks[0].estimate_status = "benchmark_required";
    }],
    ["unexpected", (value) => { value.plan_path = "C:/internal/plan.json"; }],
  ];

  for (const [field, mutate] of cases) {
    const malformed = structuredClone(source.approval_projection_unavailable);
    mutate(malformed);
    assert.throws(
      () => parseApprovalControlProjection(malformed),
      (error) => error instanceof ControlContractError && error.message.includes(field),
      field,
    );
  }
});

test("parses only the bounded structured control failure contract", async () => {
  const source = await fixture();
  assert.equal(parseControlFailure(source.control_failure), source.control_failure);

  const malformed = { ...source.control_failure, traceback: "internal" };
  assert.throws(() => parseControlFailure(malformed), /unexpected/);
});
