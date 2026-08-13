import {
  parseApprovalControlProjection,
  parseControlFailure,
  parseManualApprovalRequest,
  parseProjectLaunchRequest,
  assertLauncherRunId,
} from "./control-contract";
import type {
  ControlFailure,
  LauncherControlStatus,
  ManualApprovalRequest,
  ProjectControlView,
  ProjectDraftProjection,
  ProjectLaunchOptions,
  ProjectLaunchRequest,
} from "./control-domain";
import type { ApiEnvelope } from "./domain";

type JsonObject = Record<string, unknown>;

export const CONTROL_ENDPOINTS = {
  createDraft: "/api/v2/control/project-drafts",
  draft: (draftId: string) => `/api/v2/control/project-drafts/${encodeId(draftId, "draft_id")}`,
  approveDraft: (draftId: string) => `${CONTROL_ENDPOINTS.draft(draftId)}/approve`,
  launchDraft: (draftId: string) => `${CONTROL_ENDPOINTS.draft(draftId)}/launch`,
  run: (launcherRunId: string) => {
    assertLauncherRunId(launcherRunId);
    return `/api/v2/control/launcher-runs/${launcherRunId}`;
  },
  approval: (launcherRunId: string) => `${CONTROL_ENDPOINTS.run(launcherRunId)}/approval`,
} as const;

export class ControlClientContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ControlClientContractError";
  }
}

export class ControlRequestError extends Error {
  constructor(
    public readonly status: number,
    public readonly failure: ControlFailure,
    public readonly control: ProjectControlView | null,
  ) {
    super(failure.message);
    this.name = "ControlRequestError";
  }
}

export interface ProjectControlClientOptions {
  apiOrigin?: string;
  fetchImpl?: typeof fetch;
}

function object(value: unknown, field: string): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ControlClientContractError(`${field} must be an object`);
  }
  return value as JsonObject;
}

function exact(value: JsonObject, keys: readonly string[], field: string): void {
  const unexpected = Object.keys(value).find((key) => !keys.includes(key));
  const missing = keys.find((key) => !(key in value));
  if (unexpected) {
    throw new ControlClientContractError(`${field} has unexpected field ${unexpected}`);
  }
  if (missing) throw new ControlClientContractError(`${field}.${missing} is required`);
}

function text(value: unknown, field: string): asserts value is string {
  if (typeof value !== "string" || value.length === 0) {
    throw new ControlClientContractError(`${field} must be a non-empty string`);
  }
}

function nullableText(value: unknown, field: string): asserts value is string | null {
  if (value !== null) text(value, field);
}

function stringArray(value: unknown, field: string): asserts value is string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new ControlClientContractError(`${field} must be a string array`);
  }
}

function encodeId(value: string, field: string): string {
  if (!/^drf_[A-Za-z0-9]+$/.test(value)) {
    throw new ControlClientContractError(`${field} is invalid`);
  }
  return encodeURIComponent(value);
}

function parseEnvelope(value: unknown): { request_id: string; data: unknown } {
  const envelope = object(value, "response");
  exact(envelope, ["request_id", "data"], "response");
  text(envelope.request_id, "request_id");
  return envelope as { request_id: string; data: unknown };
}

export function parseDraftEnvelope(value: unknown): ApiEnvelope<ProjectDraftProjection> {
  const envelope = parseEnvelope(value);
  const draft = object(envelope.data, "data");
  exact(draft, [
    "draft_id", "project_id", "name", "objective", "targets", "review", "bootstrap",
  ], "data");
  for (const field of ["draft_id", "project_id", "name", "objective"] as const) {
    text(draft[field], `data.${field}`);
  }
  encodeId(draft.draft_id as string, "data.draft_id");
  if (!Array.isArray(draft.targets) || draft.targets.some((target) => {
    try { object(target, "data.targets[]"); return false; } catch { return true; }
  })) {
    throw new ControlClientContractError("data.targets must be an object array");
  }
  object(draft.review, "data.review");
  object(draft.bootstrap, "data.bootstrap");
  return envelope as ApiEnvelope<ProjectDraftProjection>;
}

function parseTrace(value: unknown): Record<string, string | null> {
  const trace = object(value, "launcher.formal_trace");
  exact(trace, [
    "workflow_id", "run_id", "plan_id", "task_id", "attempt_id", "transaction_id",
  ], "launcher.formal_trace");
  for (const [key, item] of Object.entries(trace)) {
    nullableText(item, `launcher.formal_trace.${key}`);
  }
  return trace as Record<string, string | null>;
}

function parseLauncher(value: unknown): LauncherControlStatus {
  const launcher = object(value, "launcher");
  exact(launcher, [
    "schema_version", "status", "launcher_run_id", "project_id", "approved_content_binding",
    "boundary", "prediction_invocation_id", "prediction_run_id", "formal_trace",
    "evidence_ids", "artifact_ids", "required_task_ids", "task_status_counts",
    "last_known_formal_status", "error",
  ], "launcher");
  if (!Number.isInteger(launcher.schema_version)) {
    throw new ControlClientContractError("launcher.schema_version must be an integer");
  }
  text(launcher.status, "launcher.status");
  for (const field of [
    "launcher_run_id", "project_id", "approved_content_binding", "boundary",
    "prediction_invocation_id", "prediction_run_id", "last_known_formal_status",
  ] as const) nullableText(launcher[field], `launcher.${field}`);
  if (launcher.launcher_run_id !== null) assertLauncherRunId(launcher.launcher_run_id);
  parseTrace(launcher.formal_trace);
  stringArray(launcher.evidence_ids, "launcher.evidence_ids");
  stringArray(launcher.artifact_ids, "launcher.artifact_ids");
  stringArray(launcher.required_task_ids, "launcher.required_task_ids");
  const counts = object(launcher.task_status_counts, "launcher.task_status_counts");
  if (Object.values(counts).some((count) => !Number.isInteger(count) || Number(count) < 0)) {
    throw new ControlClientContractError("launcher.task_status_counts is invalid");
  }
  if (launcher.error !== null) {
    const error = object(launcher.error, "launcher.error");
    exact(error, ["code", "component", "message"], "launcher.error");
    for (const field of ["code", "component", "message"] as const) {
      text(error[field], `launcher.error.${field}`);
    }
  }
  return launcher as unknown as LauncherControlStatus;
}

export function parseControlView(value: unknown): ProjectControlView {
  const control = object(value, "control");
  exact(control, ["launcher", "approval_control", "control_failure"], "control");
  if (control.launcher !== null) parseLauncher(control.launcher);
  if (control.approval_control !== null) {
    parseApprovalControlProjection(control.approval_control);
  }
  if (control.control_failure !== null) parseControlFailure(control.control_failure);
  return control as unknown as ProjectControlView;
}

export function parseControlViewEnvelope(value: unknown): ApiEnvelope<ProjectControlView> {
  const envelope = parseEnvelope(value);
  parseControlView(envelope.data);
  return envelope as ApiEnvelope<ProjectControlView>;
}

function parseErrorEnvelope(value: unknown, status: number): ControlRequestError {
  const envelope = object(value, "response");
  exact(envelope, ["request_id", "error"], "response");
  text(envelope.request_id, "request_id");
  const error = object(envelope.error, "error");
  const controlValue = error.control;
  const safeFailure = { ...error };
  delete safeFailure.control;
  let failure: ControlFailure;
  if ("category" in safeFailure && "component" in safeFailure && "ceiling" in safeFailure) {
    failure = parseControlFailure(safeFailure);
  } else {
    exact(safeFailure, ["code", "message"], "error");
    text(safeFailure.code, "error.code");
    text(safeFailure.message, "error.message");
    failure = {
      code: "launcher_operation_failed",
      category: "launcher",
      component: "adapter",
      message: safeFailure.message as string,
      ceiling: null,
    };
  }
  const control = controlValue === undefined ? null : parseControlView(controlValue);
  return new ControlRequestError(status, failure, control);
}

function endpoint(apiOrigin: string | undefined, path: string): string {
  return apiOrigin ? `${apiOrigin.replace(/\/$/, "")}${path}` : path;
}

export class ProjectControlClient {
  private readonly apiOrigin?: string;
  private readonly fetchImpl: typeof fetch;

  constructor(options: ProjectControlClientOptions = {}) {
    this.apiOrigin = options.apiOrigin;
    const selectedFetch = options.fetchImpl ?? fetch;
    this.fetchImpl = (input, init) => selectedFetch(input, init);
  }

  createDraft(request: ProjectLaunchRequest, signal?: AbortSignal) {
    parseProjectLaunchRequest(request);
    return this.request(CONTROL_ENDPOINTS.createDraft, "POST", request, parseDraftEnvelope, signal);
  }

  retrieveDraft(draftId: string, signal?: AbortSignal) {
    return this.request(
      CONTROL_ENDPOINTS.draft(draftId), "GET", undefined, parseDraftEnvelope, signal,
    );
  }

  approveDraft(draftId: string, justification?: string, signal?: AbortSignal) {
    return this.request(
      CONTROL_ENDPOINTS.approveDraft(draftId),
      "POST",
      { draft_id: draftId, justification: justification ?? null },
      parseDraftEnvelope,
      signal,
    );
  }

  launchDraft(draftId: string, options: ProjectLaunchOptions, signal?: AbortSignal) {
    parseProjectLaunchRequest({ target_identifier: "binding-check", options });
    return this.request(
      CONTROL_ENDPOINTS.launchDraft(draftId),
      "POST",
      { draft_id: draftId, options },
      parseControlViewEnvelope,
      signal,
    );
  }

  status(launcherRunId: string, signal?: AbortSignal) {
    return this.request(
      CONTROL_ENDPOINTS.run(launcherRunId), "GET", undefined,
      parseControlViewEnvelope, signal,
    );
  }

  approveAndContinue(request: ManualApprovalRequest, signal?: AbortSignal) {
    parseManualApprovalRequest(request);
    return this.request(
      CONTROL_ENDPOINTS.approval(request.launcher_run_id), "POST", request,
      parseControlViewEnvelope, signal,
    );
  }

  private async request<T>(
    path: string,
    method: "GET" | "POST",
    body: unknown,
    parser: (value: unknown) => ApiEnvelope<T>,
    signal?: AbortSignal,
  ): Promise<ApiEnvelope<T>> {
    const response = await this.fetchImpl(endpoint(this.apiOrigin, path), {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
    const value: unknown = await response.json();
    if (!response.ok) throw parseErrorEnvelope(value, response.status);
    return parser(value);
  }
}
