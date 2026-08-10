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
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
}

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function assertString(value: unknown, field: string): asserts value is string {
  if (typeof value !== "string" || value.length === 0) {
    throw new WorkbenchContractError(`${field} must be a non-empty string`);
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
    !Array.isArray(value.unmapped_metrics)
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
  assertString(value.request_id, "request_id");
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
  assertString(data.project.project_id, "project.project_id");
  if (data.workflow !== null && !isObject(data.workflow)) {
    throw new WorkbenchContractError("workflow must be an object or null");
  }
  if (data.run !== null && !isObject(data.run)) {
    throw new WorkbenchContractError("run must be an object or null");
  }

  const collectionNames = [
    "tasks",
    "executions",
    "transactions",
    "candidates",
    "evidence",
    "artifacts",
    "protocols",
    "blockers",
  ] as const;
  for (const name of collectionNames) {
    assertBoundedCollection(data[name], name);
  }
  if (!isObject(data.trace)) {
    throw new WorkbenchContractError("trace must be an object");
  }
  const evidence = data.evidence;
  assertBoundedCollection(evidence, "evidence");
  evidence.items.forEach(assertShortlistEvidence);

  return value as unknown as WorkbenchEnvelope;
}

function workbenchUrl(apiOrigin: string | undefined): string {
  if (!apiOrigin) return WORKBENCH_ENDPOINT;
  return `${apiOrigin.replace(/\/$/, "")}${WORKBENCH_ENDPOINT}`;
}

export async function fetchWorkbench(
  options: FetchWorkbenchOptions = {},
): Promise<WorkbenchEnvelope> {
  const fetchImpl = options.fetchImpl ?? fetch;
  const response = await fetchImpl(workbenchUrl(options.apiOrigin), {
    signal: options.signal,
  });
  if (!response.ok) {
    throw new Error(`Workbench request failed with HTTP ${response.status}`);
  }
  return parseWorkbenchEnvelope(await response.json());
}
