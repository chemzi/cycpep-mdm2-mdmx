import type { WorkbenchEnvelope } from "./domain";

export const WORKBENCH_ENDPOINT = "/api/v2/workbench";
const WORKBENCH_SCHEMA_VERSION = "frontend.workbench.v2";

type JsonObject = Record<string, unknown>;
type BoundedCollectionObject = JsonObject & {
  scope: string;
  total: number;
  returned: number;
  truncated: boolean;
  items: unknown[];
};

export interface FetchWorkbenchOptions {
  apiOrigin?: string;
  launcherRunId?: string;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
}

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function assertNonEmptyString(value: unknown, field: string): asserts value is string {
  if (typeof value !== "string" || value.length === 0) {
    throw new WorkbenchContractError(`${field} must be a non-empty string`);
  }
}

function assertString(value: unknown, field: string): asserts value is string {
  if (typeof value !== "string") {
    throw new WorkbenchContractError(`${field} must be a string`);
  }
}

function assertOptionalString(value: unknown, field: string): void {
  if (value !== undefined) assertString(value, field);
}

function assertNullableString(value: unknown, field: string): void {
  if (value !== undefined && value !== null) assertString(value, field);
}

function assertNumber(value: unknown, field: string): asserts value is number {
  if (typeof value !== "number") {
    throw new WorkbenchContractError(`${field} must be a number`);
  }
}

function assertOptionalNumber(value: unknown, field: string): void {
  if (value !== undefined) assertNumber(value, field);
}

function assertBoolean(value: unknown, field: string): asserts value is boolean {
  if (typeof value !== "boolean") {
    throw new WorkbenchContractError(`${field} must be a boolean`);
  }
}

function assertOptionalBoolean(value: unknown, field: string): void {
  if (value !== undefined) assertBoolean(value, field);
}

function assertStringArray(value: unknown, field: string): asserts value is string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new WorkbenchContractError(`${field} must be an array of strings`);
  }
}

function assertBoundedCollection(
  value: unknown,
  field: string,
): asserts value is BoundedCollectionObject {
  if (
    !isObject(value) ||
    typeof value.scope !== "string" ||
    typeof value.total !== "number" ||
    typeof value.returned !== "number" ||
    typeof value.truncated !== "boolean" ||
    !Array.isArray(value.items)
  ) {
    throw new WorkbenchContractError(`${field} must be a bounded collection`);
  }
}


const TRACE_STRING_FIELDS = [
  "project_id",
  "plan_id",
  "task_id",
  "attempt_id",
  "transaction_id",
  "candidate_id",
  "artifact_id",
  "parent_event_id",
] as const;

function assertTrace(value: unknown, field: string): void {
  if (!isObject(value)) {
    throw new WorkbenchContractError(`${field} must be an object`);
  }
  for (const name of TRACE_STRING_FIELDS) {
    assertOptionalString(value[name], `${field}.${name}`);
  }
  assertNullableString(value.workflow_id, `${field}.workflow_id`);
  assertNullableString(value.run_id, `${field}.run_id`);
}

function assertProtocol(value: unknown, field: string): void {
  if (!isObject(value)) {
    throw new WorkbenchContractError(`${field} must be an object`);
  }
  assertOptionalString(value.name, `${field}.name`);
  assertOptionalString(value.version, `${field}.version`);
  assertOptionalString(value.integrity_identity, `${field}.integrity_identity`);
}

function assertOptionalProtocol(value: unknown, field: string): void {
  if (value !== undefined) assertProtocol(value, field);
}

function assertStructuredError(value: unknown, field: string): void {
  if (!isObject(value)) {
    throw new WorkbenchContractError(`${field} must be an object`);
  }
  assertOptionalString(value.code, `${field}.code`);
  assertOptionalString(value.message, `${field}.message`);
  assertOptionalString(value.component, `${field}.component`);
  assertOptionalBoolean(value.retryable, `${field}.retryable`);
}

function assertOptionalStructuredError(value: unknown, field: string): void {
  if (value !== undefined && value !== null) assertStructuredError(value, field);
}

function assertRunRelation(value: unknown, field: string): void {
  if (!(["current_run", "historical_run", "unlinked"] as unknown[]).includes(value)) {
    throw new WorkbenchContractError(
      `${field} must be current_run, historical_run, or unlinked`,
    );
  }
}

function assertProject(value: JsonObject): void {
  assertNonEmptyString(value.project_id, "project.project_id");
  assertOptionalString(value.name, "project.name");
  assertStringArray(value.targets, "project.targets");
}

function assertTask(value: unknown, index: number): void {
  const field = `tasks.items[${index}]`;
  if (!isObject(value)) {
    throw new WorkbenchContractError(`${field} must be an object`);
  }
  for (const name of ["task_id", "agent", "kind", "disposition", "status"] as const) {
    assertOptionalString(value[name], `${field}.${name}`);
  }
  assertStringArray(value.depends_on, `${field}.depends_on`);

  if (!isObject(value.action)) {
    throw new WorkbenchContractError(`${field}.action must be an object`);
  }
  assertString(value.action.name, `${field}.action.name`);
  assertBoolean(value.action.executable, `${field}.action.executable`);
  assertBoolean(value.action.handler_available, `${field}.action.handler_available`);
  if (value.action.resource_class !== null) {
    assertString(value.action.resource_class, `${field}.action.resource_class`);
  }
  assertStringArray(value.action.output_roles, `${field}.action.output_roles`);

  if (!isObject(value.availability)) {
    throw new WorkbenchContractError(`${field}.availability must be an object`);
  }
  assertBoolean(value.availability.available, `${field}.availability.available`);
  assertStringArray(value.availability.reason_codes, `${field}.availability.reason_codes`);

  if (!isObject(value.approval)) {
    throw new WorkbenchContractError(`${field}.approval must be an object`);
  }
  assertBoolean(value.approval.required, `${field}.approval.required`);
  assertString(value.approval.state, `${field}.approval.state`);

  if (!isObject(value.execution_gate)) {
    throw new WorkbenchContractError(`${field}.execution_gate must be an object`);
  }
  assertOptionalString(value.execution_gate.status, `${field}.execution_gate.status`);
  assertOptionalProtocol(value.protocol, `${field}.protocol`);
}

function assertExecution(value: unknown, index: number): void {
  const field = `executions.items[${index}]`;
  if (!isObject(value)) {
    throw new WorkbenchContractError(`${field} must be an object`);
  }
  assertNonEmptyString(value.task_id, `${field}.task_id`);
  assertOptionalString(value.status, `${field}.status`);
  assertNumber(value.attempts, `${field}.attempts`);
  assertNullableString(value.attempt_id, `${field}.attempt_id`);
  assertNullableString(value.worker_id, `${field}.worker_id`);
  assertString(value.transaction_visibility, `${field}.transaction_visibility`);
  assertOptionalStructuredError(value.error, `${field}.error`);
}

function assertTransaction(value: unknown, index: number): void {
  const field = `transactions.items[${index}]`;
  if (!isObject(value)) {
    throw new WorkbenchContractError(`${field} must be an object`);
  }
  assertTrace(value, field);
  for (const name of ["transaction_id", "task_id", "attempt_id", "status"] as const) {
    assertNonEmptyString(value[name], `${field}.${name}`);
  }
  assertOptionalString(value.created_at, `${field}.created_at`);
  assertOptionalString(value.updated_at, `${field}.updated_at`);
  assertOptionalStructuredError(value.error, `${field}.error`);
}

function assertProvenanceRecord(value: JsonObject, field: string): void {
  assertTrace(value.trace, `${field}.trace`);
  assertRunRelation(value.run_relation, `${field}.run_relation`);
  assertOptionalProtocol(value.protocol, `${field}.protocol`);
}

function assertCandidate(value: unknown, index: number): void {
  const field = `candidates.items[${index}]`;
  if (!isObject(value)) {
    throw new WorkbenchContractError(`${field} must be an object`);
  }
  for (const name of [
    "candidate_id", "sequence", "source_route", "status", "final_status",
    "created_at", "updated_at",
  ] as const) {
    assertOptionalString(value[name], `${field}.${name}`);
  }
  if (value.metrics !== undefined && !isObject(value.metrics)) {
    throw new WorkbenchContractError(`${field}.metrics must be an object`);
  }
  assertProvenanceRecord(value, field);
}

function assertEvidence(value: unknown, index: number): void {
  const field = `evidence.items[${index}]`;
  if (!isObject(value)) {
    throw new WorkbenchContractError(`${field} must be an object`);
  }
  for (const name of [
    "event_id", "timestamp", "agent", "event_type", "phase", "code",
    "component", "message",
  ] as const) {
    assertOptionalString(value[name], `${field}.${name}`);
  }
  assertOptionalNumber(value.round, `${field}.round`);
  assertOptionalBoolean(value.retryable, `${field}.retryable`);
  assertOptionalBoolean(value.blocks, `${field}.blocks`);
  if (value.targets !== undefined) assertStringArray(value.targets, `${field}.targets`);
  assertProvenanceRecord(value, field);
  assertShortlistEvidence(value, index);
}

function assertArtifact(value: unknown, index: number): void {
  const field = `artifacts.items[${index}]`;
  if (!isObject(value)) {
    throw new WorkbenchContractError(`${field} must be an object`);
  }
  for (const name of [
    "artifact_id", "artifact_type", "role", "sha256", "schema_version",
    "created_at", "producer_task_id", "content_link",
  ] as const) {
    assertOptionalString(value[name], `${field}.${name}`);
  }
  assertOptionalNumber(value.size_bytes, `${field}.size_bytes`);
  if (value.input_artifact_ids !== undefined) {
    assertStringArray(value.input_artifact_ids, `${field}.input_artifact_ids`);
  }
  assertProvenanceRecord(value, field);
}

function assertBlocker(value: unknown, index: number): void {
  const field = `blockers.items[${index}]`;
  if (!isObject(value)) {
    throw new WorkbenchContractError(`${field} must be an object`);
  }
  assertString(value.code, `${field}.code`);
  assertString(value.scope, `${field}.scope`);
  assertString(value.summary, `${field}.summary`);
  for (const name of ["workflow_id", "run_id", "task_id", "transaction_id"] as const) {
    assertOptionalString(value[name], `${field}.${name}`);
  }
}

function assertShortlistEvidence(value: unknown, index: number): void {
  if (!isObject(value) || value.event_type !== "exploration_shortlist") return;
  const calibration = value.calibration;
  const shortlist = value.shortlist;
  if (
    typeof value.n_evaluated !== "number" ||
    typeof value.n_passed !== "number" ||
    !Array.isArray(shortlist) ||
    !isObject(calibration) ||
    typeof calibration.calibrated !== "number" ||
    typeof calibration.provisional !== "number" ||
    typeof calibration.unavailable !== "number" ||
    !Array.isArray(value.source_event_ids) ||
    value.source_event_ids.some((item) => typeof item !== "string") ||
    !Array.isArray(value.unmapped_metrics) ||
    value.unmapped_metrics.some((item) => typeof item !== "string")
  ) {
    throw new WorkbenchContractError(
      `evidence.items[${index}] does not satisfy exploration_shortlist`,
    );
  }
  shortlist.forEach((item, itemIndex) => {
    if (
      !isObject(item) ||
      typeof item.candidate_id !== "string" ||
      typeof item.passed !== "boolean" ||
      !(typeof item.desirability === "number" || item.desirability === null) ||
      typeof item.pareto_front !== "boolean" ||
      typeof item.reason !== "string" ||
      !(typeof item.top_margin_metric === "string" || item.top_margin_metric === null)
    ) {
      throw new WorkbenchContractError(
        `evidence.items[${index}].shortlist[${itemIndex}] is malformed`,
      );
    }
  });
  assertOptionalNumber(value.k, `evidence.items[${index}].k`);
}

export class WorkbenchContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "WorkbenchContractError";
  }
}

export function parseWorkbenchEnvelope(value: unknown): WorkbenchEnvelope {
  if (!isObject(value)) {
    throw new WorkbenchContractError("Workbench response must be an object");
  }
  assertNonEmptyString(value.request_id, "request_id");
  if (!isObject(value.data)) {
    throw new WorkbenchContractError("data must be an object");
  }

  const data = value.data;
  if (data.schema_version !== WORKBENCH_SCHEMA_VERSION) {
    throw new WorkbenchContractError(
      `Expected ${WORKBENCH_SCHEMA_VERSION}`,
    );
  }
  if (!isObject(data.project)) {
    throw new WorkbenchContractError("project must be an object");
  }
  assertProject(data.project);
  if (data.workflow !== null && !isObject(data.workflow)) {
    throw new WorkbenchContractError("workflow must be an object or null");
  }
  if (data.run !== null && !isObject(data.run)) {
    throw new WorkbenchContractError("run must be an object or null");
  }

  const tasks = data.tasks;
  const executions = data.executions;
  const transactions = data.transactions;
  const candidates = data.candidates;
  const evidence = data.evidence;
  const artifacts = data.artifacts;
  const protocols = data.protocols;
  const blockers = data.blockers;
  assertBoundedCollection(tasks, "tasks");
  assertBoundedCollection(executions, "executions");
  assertBoundedCollection(transactions, "transactions");
  assertBoundedCollection(candidates, "candidates");
  assertBoundedCollection(evidence, "evidence");
  assertBoundedCollection(artifacts, "artifacts");
  assertBoundedCollection(protocols, "protocols");
  assertBoundedCollection(blockers, "blockers");
  if (!isObject(data.trace)) {
    throw new WorkbenchContractError("trace must be an object");
  }
  assertTrace(data.trace, "trace");
  tasks.items.forEach(assertTask);
  executions.items.forEach(assertExecution);
  transactions.items.forEach(assertTransaction);
  candidates.items.forEach(assertCandidate);
  evidence.items.forEach(assertEvidence);
  artifacts.items.forEach(assertArtifact);
  protocols.items.forEach((protocol, index) =>
    assertProtocol(protocol, `protocols.items[${index}]`)
  );
  blockers.items.forEach(assertBlocker);

  return value as unknown as WorkbenchEnvelope;
}

export function buildWorkbenchUrl(
  apiOrigin: string | undefined,
  launcherRunId?: string,
): string {
  const endpoint = apiOrigin
    ? `${apiOrigin.replace(/\/$/, "")}${WORKBENCH_ENDPOINT}`
    : WORKBENCH_ENDPOINT;
  if (launcherRunId === undefined) return endpoint;
  assertLauncherRunId(launcherRunId);
  return `${endpoint}?launcher_run_id=${encodeURIComponent(launcherRunId)}`;
}

const LAUNCHER_RUN_ID = /^launcher_[0-9a-f]{32}$/;

function assertLauncherRunId(value: string): void {
  if (!LAUNCHER_RUN_ID.test(value)) {
    throw new WorkbenchContractError("launcherRunId is invalid");
  }
}

export async function fetchWorkbench(
  options: FetchWorkbenchOptions = {},
): Promise<WorkbenchEnvelope> {
  const fetchImpl = options.fetchImpl ?? fetch;
  const response = await fetchImpl(
    buildWorkbenchUrl(options.apiOrigin, options.launcherRunId),
    {
    signal: options.signal,
    },
  );
  if (!response.ok) {
    throw new Error(`Workbench request failed with HTTP ${response.status}`);
  }
  return parseWorkbenchEnvelope(await response.json());
}
