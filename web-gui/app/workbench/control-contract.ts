import type {
  ApprovalBudgetProjection,
  ApprovalCeilings,
  ApprovalControlProjection,
  AutoApprovalCeilings,
  CalibrationStatus,
  ControlFailure,
  ControlFailureCategory,
  ControlFailureCode,
  EstimateStatus,
  FirstGateAutoApprovalPolicy,
  IdentifierType,
  ManualApprovalRequest,
  ProjectLaunchOptions,
  ProjectLaunchRequest,
  ResourceClass,
  TaskResourceProjection,
} from "./control-domain";

type JsonObject = Record<string, unknown>;

const LAUNCHER_ID = /^launcher_[0-9a-f]{32}$/;
const PLAN_ID = /^planner_[0-9a-f]{12}$/;
const TASK_ID = /^T[0-9]{3}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const IDENTIFIER_TYPES: IdentifierType[] = ["auto", "gene", "uniprot", "pdb"];
const RESOURCE_CLASSES: ResourceClass[] = ["cpu", "network_cpu", "gpu"];
const ESTIMATE_STATUSES: EstimateStatus[] = [
  "estimated", "benchmark_required", "unavailable", "not_applicable",
];
const CALIBRATION_STATUSES: CalibrationStatus[] = [
  "calibrated", "provisional", "pending", "unavailable", "not_applicable",
];
const FAILURE_CATEGORIES: ControlFailureCategory[] = [
  "binding", "stale_plan", "estimate", "ceiling", "review", "launcher",
];
const FAILURE_CODES: ControlFailureCode[] = [
  "control_binding_invalid", "control_binding_conflict", "approval_plan_stale",
  "approval_estimate_unavailable", "approval_ceiling_exceeded",
  "project_review_blocked", "launcher_run_not_found", "launcher_operation_failed",
];

export class ControlContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ControlContractError";
  }
}

function object(value: unknown, field: string): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ControlContractError(`${field} must be an object`);
  }
  return value as JsonObject;
}

function exact(value: JsonObject, keys: readonly string[], field: string): void {
  const unexpected = Object.keys(value).filter((key) => !keys.includes(key));
  const missing = keys.filter((key) => !(key in value));
  if (unexpected.length > 0) {
    throw new ControlContractError(`${field} has unexpected field ${unexpected[0]}`);
  }
  if (missing.length > 0) {
    throw new ControlContractError(`${field}.${missing[0]} is required`);
  }
}

function text(value: unknown, field: string): asserts value is string {
  if (typeof value !== "string" || value.length === 0) {
    throw new ControlContractError(`${field} must be a non-empty string`);
  }
}

function nullableText(value: unknown, field: string): asserts value is string | null {
  if (value !== null) text(value, field);
}

function finite(value: unknown, field: string): asserts value is number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new ControlContractError(`${field} must be a finite number`);
  }
}

function nonnegativeInteger(value: unknown, field: string): asserts value is number {
  finite(value, field);
  if (!Number.isInteger(value) || value < 0) {
    throw new ControlContractError(`${field} must be a non-negative integer`);
  }
}

function nullableNonnegativeInteger(value: unknown, field: string): void {
  if (value !== null) nonnegativeInteger(value, field);
}

function nullablePositive(value: unknown, field: string): void {
  if (value === null) return;
  finite(value, field);
  if (value <= 0) throw new ControlContractError(`${field} must be positive`);
}

function member<T extends string>(
  value: unknown,
  values: readonly T[],
  field: string,
): asserts value is T {
  if (typeof value !== "string" || !values.includes(value as T)) {
    throw new ControlContractError(`${field} is unsupported`);
  }
}

function matches(value: unknown, pattern: RegExp, field: string): asserts value is string {
  if (typeof value !== "string" || !pattern.test(value)) {
    throw new ControlContractError(`${field} is invalid`);
  }
}

export function assertLauncherRunId(
  value: unknown,
  field = "launcher_run_id",
): asserts value is string {
  matches(value, LAUNCHER_ID, field);
}

function stringArray(value: unknown, field: string, pattern?: RegExp): asserts value is string[] {
  if (
    !Array.isArray(value) || value.length === 0 ||
    value.some((item) => typeof item !== "string" || (pattern && !pattern.test(item))) ||
    new Set(value).size !== value.length
  ) {
    throw new ControlContractError(`${field} must be a non-empty unique string array`);
  }
}

function parseAutoCeilings(value: unknown, field: string): AutoApprovalCeilings {
  const result = object(value, field);
  exact(result, [
    "max_gpu_job_slots", "max_gpu_minutes", "max_design_proposals",
    "max_prediction_candidates",
  ], field);
  nonnegativeInteger(result.max_gpu_job_slots, `${field}.max_gpu_job_slots`);
  finite(result.max_gpu_minutes, `${field}.max_gpu_minutes`);
  if (result.max_gpu_minutes <= 0) {
    throw new ControlContractError(`${field}.max_gpu_minutes must be positive`);
  }
  nonnegativeInteger(result.max_design_proposals, `${field}.max_design_proposals`);
  nonnegativeInteger(result.max_prediction_candidates, `${field}.max_prediction_candidates`);
  return result as unknown as AutoApprovalCeilings;
}

function parseAutoPolicy(value: unknown, field: string): FirstGateAutoApprovalPolicy {
  const result = object(value, field);
  exact(result, ["approver", "justification", "ceilings"], field);
  text(result.approver, `${field}.approver`);
  text(result.justification, `${field}.justification`);
  parseAutoCeilings(result.ceilings, `${field}.ceilings`);
  return result as unknown as FirstGateAutoApprovalPolicy;
}

function parseLaunchOptions(value: unknown, field: string): ProjectLaunchOptions {
  const result = object(value, field);
  exact(result, [
    "identifier_type", "organism_id", "epitope", "objective", "launcher_run_id",
    "first_gate_auto_policy",
  ], field);
  member(result.identifier_type, IDENTIFIER_TYPES, `${field}.identifier_type`);
  nonnegativeInteger(result.organism_id, `${field}.organism_id`);
  if (result.organism_id === 0) {
    throw new ControlContractError(`${field}.organism_id must be positive`);
  }
  nullableText(result.epitope, `${field}.epitope`);
  text(result.objective, `${field}.objective`);
  if (result.launcher_run_id !== null) {
    assertLauncherRunId(result.launcher_run_id, `${field}.launcher_run_id`);
  }
  if (result.first_gate_auto_policy !== null) {
    parseAutoPolicy(result.first_gate_auto_policy, `${field}.first_gate_auto_policy`);
  }
  return result as unknown as ProjectLaunchOptions;
}

export function parseProjectLaunchRequest(value: unknown): ProjectLaunchRequest {
  const result = object(value, "launch_request");
  exact(result, ["target_identifier", "options"], "launch_request");
  text(result.target_identifier, "launch_request.target_identifier");
  parseLaunchOptions(result.options, "launch_request.options");
  return result as unknown as ProjectLaunchRequest;
}

function parseResource(value: unknown, field: string): TaskResourceProjection {
  const result = object(value, field);
  exact(result, [
    "task_id", "action", "resource_class", "gpu_job_slots", "proposal_count",
    "candidate_limit", "estimated_gpu_minutes", "estimate_status", "estimator_version",
    "calibration_status",
  ], field);
  matches(result.task_id, TASK_ID, `${field}.task_id`);
  text(result.action, `${field}.action`);
  member(result.resource_class, RESOURCE_CLASSES, `${field}.resource_class`);
  nonnegativeInteger(result.gpu_job_slots, `${field}.gpu_job_slots`);
  nonnegativeInteger(result.proposal_count, `${field}.proposal_count`);
  nonnegativeInteger(result.candidate_limit, `${field}.candidate_limit`);
  member(result.estimate_status, ESTIMATE_STATUSES, `${field}.estimate_status`);
  nullableText(result.estimator_version, `${field}.estimator_version`);
  member(result.calibration_status, CALIBRATION_STATUSES, `${field}.calibration_status`);
  validateEstimate(
    result.estimated_gpu_minutes,
    result.estimate_status,
    `${field}.estimated_gpu_minutes`,
  );
  return result as unknown as TaskResourceProjection;
}

function validateEstimate(value: unknown, status: EstimateStatus, field: string): void {
  if (status !== "estimated") {
    if (value !== null) {
      throw new ControlContractError(`${field} must be null when estimate is unavailable`);
    }
    return;
  }
  finite(value, field);
  if (value < 0) throw new ControlContractError(`${field} must be non-negative`);
}

function parseBudget(value: unknown, field: string): ApprovalBudgetProjection {
  const result = object(value, field);
  exact(result, [
    "gpu_minutes", "gpu_minutes_status", "estimator_version", "calibration_status",
  ], field);
  member(result.gpu_minutes_status, ESTIMATE_STATUSES, `${field}.gpu_minutes_status`);
  nullableText(result.estimator_version, `${field}.estimator_version`);
  member(result.calibration_status, CALIBRATION_STATUSES, `${field}.calibration_status`);
  validateEstimate(result.gpu_minutes, result.gpu_minutes_status, `${field}.gpu_minutes`);
  return result as unknown as ApprovalBudgetProjection;
}

export function parseApprovalControlProjection(value: unknown): ApprovalControlProjection {
  const result = object(value, "approval_control");
  exact(result, [
    "launcher_run_id", "project_id", "approved_content_binding", "plan_id", "plan_sha256",
    "source_kind", "required_task_ids", "tasks", "budget",
  ], "approval_control");
  assertLauncherRunId(result.launcher_run_id);
  text(result.project_id, "project_id");
  matches(result.approved_content_binding, SHA256, "approved_content_binding");
  matches(result.plan_id, PLAN_ID, "plan_id");
  matches(result.plan_sha256, SHA256, "plan_sha256");
  text(result.source_kind, "source_kind");
  stringArray(result.required_task_ids, "required_task_ids", TASK_ID);
  const requiredTaskIds = result.required_task_ids;
  if (!Array.isArray(result.tasks)) {
    throw new ControlContractError("tasks must be an array");
  }
  result.tasks.forEach((task, index) => parseResource(task, `tasks[${index}]`));
  const taskIds = result.tasks.map((task) => (task as JsonObject).task_id);
  if (
    taskIds.length !== requiredTaskIds.length ||
    taskIds.some((taskId, index) => taskId !== requiredTaskIds[index])
  ) {
    throw new ControlContractError("required_task_ids must exactly match tasks");
  }
  parseBudget(result.budget, "budget");
  return result as unknown as ApprovalControlProjection;
}

function parseManualCeilings(value: unknown, field: string): ApprovalCeilings {
  const result = object(value, field);
  exact(result, [
    "max_gpu_job_slots", "max_gpu_minutes", "max_design_proposals",
    "max_prediction_candidates",
  ], field);
  nullableNonnegativeInteger(result.max_gpu_job_slots, `${field}.max_gpu_job_slots`);
  nullablePositive(result.max_gpu_minutes, `${field}.max_gpu_minutes`);
  nullableNonnegativeInteger(result.max_design_proposals, `${field}.max_design_proposals`);
  nullableNonnegativeInteger(
    result.max_prediction_candidates,
    `${field}.max_prediction_candidates`,
  );
  return result as unknown as ApprovalCeilings;
}

export function parseManualApprovalRequest(value: unknown): ManualApprovalRequest {
  const result = object(value, "manual_approval_request");
  exact(result, [
    "launcher_run_id", "project_id", "approved_content_binding", "plan_id", "plan_sha256",
    "required_task_ids", "approver", "justification", "ceilings",
  ], "manual_approval_request");
  assertLauncherRunId(result.launcher_run_id);
  text(result.project_id, "project_id");
  matches(result.approved_content_binding, SHA256, "approved_content_binding");
  matches(result.plan_id, PLAN_ID, "plan_id");
  matches(result.plan_sha256, SHA256, "plan_sha256");
  stringArray(result.required_task_ids, "required_task_ids", TASK_ID);
  text(result.approver, "approver");
  text(result.justification, "justification");
  parseManualCeilings(result.ceilings, "ceilings");
  return result as unknown as ManualApprovalRequest;
}

export function parseControlFailure(value: unknown): ControlFailure {
  const result = object(value, "control_failure");
  exact(result, ["code", "category", "component", "message", "ceiling"], "control_failure");
  member(result.code, FAILURE_CODES, "code");
  member(result.category, FAILURE_CATEGORIES, "category");
  text(result.component, "component");
  text(result.message, "message");
  nullableText(result.ceiling, "ceiling");
  return result as unknown as ControlFailure;
}
