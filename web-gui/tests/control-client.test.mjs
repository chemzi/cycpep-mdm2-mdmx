import assert from "node:assert/strict";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { after, before } from "node:test";
import { createServer } from "vite";

let vite;
let CONTROL_ENDPOINTS;
let ControlRequestError;
let ProjectControlClient;
let parseControlViewEnvelope;
let parseDraftEnvelope;
let initialProjectLaunchState;
let prepareLaunchAttempt;
let getPersistedLaunchAttempt;
let recoverPersistedLaunch;
let refreshLauncherControl;
let projectLaunchReducer;

before(async () => {
  vite = await createServer({
    appType: "custom",
    cacheDir: join(tmpdir(), `cycpep-vite-control-client-${process.pid}`),
    configFile: false,
    logLevel: "silent",
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true },
  });
  ({
    CONTROL_ENDPOINTS, ControlRequestError, ProjectControlClient,
    parseControlViewEnvelope, parseDraftEnvelope,
  } = await vite.ssrLoadModule("/app/workbench/control-client.ts"));
  ({
    getPersistedLaunchAttempt, initialProjectLaunchState, prepareLaunchAttempt,
    projectLaunchReducer, recoverPersistedLaunch, refreshLauncherControl,
  } = await vite.ssrLoadModule("/app/workbench/use-project-launch-control.ts"));
});

after(() => vite?.close());

const launcherRunId = "launcher_0123456789abcdef0123456789abcdef";
const digest = "a".repeat(64);
const draft = {
  draft_id: "drf_demo",
  project_id: "project-1",
  name: "MDM2 campaign",
  objective: "binder",
  targets: [{ id: "MDM2", gene_name: "MDM2" }],
  review: {
    status: "approved",
    revision: 1,
    content_digest: digest,
    approved_digest: digest,
    blocking_issues: [],
    warnings: [],
    checklist: [],
  },
  bootstrap: {
    ambiguous_identifier: false,
    assumptions: [],
    resolved_candidates: [],
    selected_candidate: null,
  },
};
const launcher = {
  schema_version: 1,
  status: "awaiting_approval",
  launcher_run_id: launcherRunId,
  project_id: "project-1",
  approved_content_binding: digest,
  boundary: "approval",
  prediction_invocation_id: null,
  prediction_run_id: null,
  formal_trace: {
    workflow_id: null, run_id: null, plan_id: null, task_id: null,
    attempt_id: null, transaction_id: null,
  },
  evidence_ids: [], artifact_ids: [], required_task_ids: ["T001"],
  task_status_counts: {}, last_known_formal_status: "awaiting_approval", error: null,
};
const control = { launcher, approval_control: null, control_failure: null };

function response(data, status = 200) {
  return new Response(JSON.stringify({ request_id: "req_123", data }), { status });
}

test("strictly parses draft and control success envelopes", () => {
  assert.equal(parseDraftEnvelope({ request_id: "req_1", data: draft }).data, draft);
  assert.equal(
    parseControlViewEnvelope({ request_id: "req_2", data: control }).data,
    control,
  );
  assert.throws(
    () => parseControlViewEnvelope({ request_id: "req_2", data: { ...control, path: "x" } }),
    /unexpected/,
  );
});

test("calls the frozen control routes with exact methods and bodies", async () => {
  const calls = [];
  const client = new ProjectControlClient({ fetchImpl: async (url, init = {}) => {
    calls.push([url, init.method ?? "GET", init.body ? JSON.parse(init.body) : null]);
    if (url === CONTROL_ENDPOINTS.createDraft) return response(draft, 201);
    if (String(url).endsWith("/launch") || String(url).includes("launcher-runs")) {
      return response(control);
    }
    return response(draft);
  } });
  const request = {
    target_identifier: "MDM2",
    options: {
      identifier_type: "gene", organism_id: 9606, epitope: null, objective: "binder",
      launcher_run_id: null, first_gate_auto_policy: null,
    },
  };
  const manual = {
    launcher_run_id: launcherRunId, project_id: "project-1",
    approved_content_binding: digest, plan_id: "planner_0123456789ab",
    plan_sha256: digest, required_task_ids: ["T001"], approver: "operator",
    justification: "Reviewed.", ceilings: {
      max_gpu_job_slots: 1, max_gpu_minutes: 20, max_design_proposals: 0,
      max_prediction_candidates: 8,
    },
  };

  await client.createDraft(request);
  await client.retrieveDraft("drf_demo");
  await client.approveDraft("drf_demo", "Reviewed project.");
  await client.launchDraft("drf_demo", { ...request.options, launcher_run_id: launcherRunId });
  await client.status(launcherRunId);
  await client.continueRun(launcherRunId);
  await client.approveAndContinue(manual);

  assert.deepEqual(calls.map(([url, method]) => [url, method]), [
    ["/api/v2/control/project-drafts", "POST"],
    ["/api/v2/control/project-drafts/drf_demo", "GET"],
    ["/api/v2/control/project-drafts/drf_demo/approve", "POST"],
    ["/api/v2/control/project-drafts/drf_demo/launch", "POST"],
    [`/api/v2/control/launcher-runs/${launcherRunId}`, "GET"],
    [`/api/v2/control/launcher-runs/${launcherRunId}/continue`, "POST"],
    [`/api/v2/control/launcher-runs/${launcherRunId}/approval`, "POST"],
  ]);
  assert.equal(calls[3][2].draft_id, "drf_demo");
  assert.equal(calls[3][2].options.launcher_run_id, launcherRunId);
});

test("invokes fetch without rebinding the browser function to the client", async () => {
  const client = new ProjectControlClient({
    fetchImpl: function () {
      assert.equal(this, undefined);
      return Promise.resolve(response(draft, 201));
    },
  });

  await client.createDraft({
    target_identifier: "MDM2",
    options: {
      identifier_type: "gene", organism_id: 9606, epitope: null,
      objective: "binder", launcher_run_id: null, first_gate_auto_policy: null,
    },
  });
});

test("keeps the safe control view attached to structured HTTP failures", async () => {
  const failure = {
    code: "approval_ceiling_exceeded", category: "ceiling", component: "planner",
    message: "Ceiling exceeded.", ceiling: "max_gpu_minutes", control,
  };
  const client = new ProjectControlClient({ fetchImpl: async () => new Response(
    JSON.stringify({ request_id: "req_3", error: failure }), { status: 409 },
  ) });

  await assert.rejects(
    client.status(launcherRunId),
    (error) => error instanceof ControlRequestError &&
      error.failure.code === "approval_ceiling_exceeded" &&
      error.control?.launcher?.status === "awaiting_approval",
  );
});

test("launch attempt persists identity before request and reuses it after response loss", () => {
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
  const form = { target_identifier: "MDM2", options: {
    identifier_type: "gene", organism_id: 9606, epitope: null, objective: "binder",
    launcher_run_id: null, first_gate_auto_policy: null,
  } };
  const first = prepareLaunchAttempt(
    storage, draft, () => launcherRunId, form, form.options,
  );
  const second = prepareLaunchAttempt(
    storage,
    draft,
    () => "launcher_ffffffffffffffffffffffffffffffff",
  );

  assert.equal(first.launcher_run_id, launcherRunId);
  assert.deepEqual(second, first);
  assert.equal(JSON.parse([...values.values()][0]).draft_id, "drf_demo");
  assert.equal(getPersistedLaunchAttempt(storage).request.target_identifier, "MDM2");
  assert.equal(getPersistedLaunchAttempt(storage).options.launcher_run_id, launcherRunId);
});

test("state transitions preserve form, review, and last control on failures", () => {
  const form = { target_identifier: "MDM2", options: {
    identifier_type: "gene", organism_id: 9606, epitope: null, objective: "binder",
    launcher_run_id: null, first_gate_auto_policy: null,
  } };
  let state = initialProjectLaunchState(form);
  state = projectLaunchReducer(state, { type: "draft-succeeded", review: draft });
  state = projectLaunchReducer(state, { type: "control-succeeded", control });
  state = projectLaunchReducer(state, { type: "mutation-failed", error: "lost response" });

  assert.equal(state.form, form);
  assert.equal(state.review, draft);
  assert.equal(state.lastControl, control);
  assert.equal(state.status, "failed");
});

test("reload recovery checks status first and retries the exact launch only when missing", async () => {
  const form = { target_identifier: "MDM2", options: {
    identifier_type: "gene", organism_id: 9606, epitope: null, objective: "binder",
    launcher_run_id: null, first_gate_auto_policy: null,
  } };
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
  const attempt = prepareLaunchAttempt(
    storage, draft, () => launcherRunId, form, form.options,
  );
  const calls = [];
  const client = {
    async retrieveDraft(id) { calls.push(["draft", id]); return { data: draft }; },
    async status(id) {
      calls.push(["status", id]);
      throw new ControlRequestError(404, {
        code: "launcher_run_not_found", category: "launcher", component: "launcher",
        message: "Launcher run not found", ceiling: null,
      }, null);
    },
    async launchDraft(id, options) {
      calls.push(["launch", id, options.launcher_run_id]);
      return { data: control };
    },
  };

  const recovered = await recoverPersistedLaunch(client, attempt);

  assert.equal(recovered.review.draft_id, draft.draft_id);
  assert.equal(recovered.control.launcher.launcher_run_id, launcherRunId);
  assert.deepEqual(calls, [
    ["draft", draft.draft_id],
    ["status", launcherRunId],
    ["launch", draft.draft_id, launcherRunId],
  ]);
});

test("a polled later approval replaces the first gate instead of going silent", () => {
  const form = { target_identifier: "MDM2", options: {
    identifier_type: "gene", organism_id: 9606, epitope: null, objective: "binder",
    launcher_run_id: null, first_gate_auto_policy: null,
  } };
  const first = { ...control, approval_control: { plan_id: "planner_first" } };
  const later = { ...control, approval_control: { plan_id: "planner_later" } };
  let state = initialProjectLaunchState(form);
  state = projectLaunchReducer(state, { type: "control-succeeded", control: first });
  state = projectLaunchReducer(state, { type: "mutation-started", status: "checking" });
  state = projectLaunchReducer(state, { type: "control-succeeded", control: later });

  assert.equal(state.lastControl.approval_control.plan_id, "planner_later");
  assert.equal(state.status, "launched");
});

test("a pending Critic poll asks Launcher to continue and returns the second gate", async () => {
  const pending = {
    ...control,
    launcher: { ...control.launcher, status: "pending", boundary: "critic" },
    approval_control: null,
  };
  const later = {
    ...control,
    launcher: { ...control.launcher, status: "awaiting_approval", boundary: "approval" },
    approval_control: { plan_id: "planner_later" },
  };
  const calls = [];
  const client = {
    async status(id) { calls.push(["status", id]); return { data: pending }; },
    async continueRun(id) { calls.push(["continue", id]); return { data: later }; },
  };

  const refreshed = await refreshLauncherControl(client, launcherRunId);

  assert.equal(refreshed.approval_control.plan_id, "planner_later");
  assert.deepEqual(calls, [["status", launcherRunId], ["continue", launcherRunId]]);
});
