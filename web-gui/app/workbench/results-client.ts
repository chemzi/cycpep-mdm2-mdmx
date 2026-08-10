import type { ApiEnvelope } from "./domain";

export const RESULTS_ENDPOINT = "/api/v2/results";
export const RESULTS_SCHEMA_VERSION = "frontend.results.v1" as const;

type JsonObject = Record<string, unknown>;

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function assertString(value: unknown, field: string): asserts value is string {
  if (typeof value !== "string") {
    throw new ResultsContractError(`${field} must be a string`);
  }
}

function assertNonEmptyString(value: unknown, field: string): asserts value is string {
  if (typeof value !== "string" || value.length === 0) {
    throw new ResultsContractError(`${field} must be a non-empty string`);
  }
}

function assertNumber(value: unknown, field: string): asserts value is number {
  if (typeof value !== "number") {
    throw new ResultsContractError(`${field} must be a number`);
  }
}

function assertNullableNumber(value: unknown, field: string): void {
  if (value !== null && typeof value !== "number") {
    throw new ResultsContractError(`${field} must be a number or null`);
  }
}

function assertBoolean(value: unknown, field: string): asserts value is boolean {
  if (typeof value !== "boolean") {
    throw new ResultsContractError(`${field} must be a boolean`);
  }
}

function assertStringArray(value: unknown, field: string): asserts value is string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new ResultsContractError(`${field} must be an array of strings`);
  }
}

function assertNullableString(value: unknown, field: string): void {
  if (value !== null && typeof value !== "string") {
    throw new ResultsContractError(`${field} must be a string or null`);
  }
}

function assertOptionalString(value: unknown, field: string): void {
  if (value !== undefined) assertString(value, field);
}

export interface ThresholdEntryView {
  calibration_status: string;
  value: number | null;
  operator: string | null;
}

export interface PerTargetStat {
  target: string;
  evaluated: number;
  passed: number;
  pass_rate: number | null;
}

export interface PerTargetThresholdView extends ThresholdEntryView {
  target: string;
}

export interface ResultsLayerView {
  key: string;
  metric: string;
  direction: string;
  scope: string;
  evaluated: number;
  passed: number;
  pass_rate: number | null;
  threshold: ThresholdEntryView | null;
  per_target: PerTargetStat[];
  per_target_thresholds: PerTargetThresholdView[];
}

export interface ResultsFinalist {
  candidate_id: string;
  rank: number;
  sequence: string | null;
  status: string | null;
  source_route: string | null;
  hard_cleared: boolean;
  failed_layers: string[];
  desirability: number | null;
  pareto_front: boolean;
  top_margin_metric: string | null;
  targets: string[];
  metrics: JsonObject;
  battery_event_id: string | null;
  battery_timestamp: string | null;
}

export interface ResultsCounts {
  calibrated: number;
  provisional: number;
  unavailable: number;
}

export interface ResultsSummary {
  candidates_total: number;
  candidates_evaluated: number;
  candidates_pending_prediction: number;
  hard_cleared: number;
  hard_clearance_rate: number | null;
  n_shortlisted: number;
  n_pareto_front: number;
  layers_total: number;
  layers_evaluated: number;
  data_basis: "none" | "demo_fixture" | "real";
  counts: ResultsCounts;
  keys: string[];
  metrics_covered: string[];
}

export interface ResultsDigest {
  schema_version: typeof RESULTS_SCHEMA_VERSION;
  project: {
    project_id: string;
    name: string;
    targets: string[];
  };
  run: {
    run_id: string;
    workflow_id: string;
    plan_id: string;
    status: string;
  } | null;
  summary: ResultsSummary;
  layers: ResultsLayerView[];
  finalists: ResultsFinalist[];
  pending_candidates: Array<{ candidate_id: string; status?: string }>;
  thresholds: {
    counts: ResultsCounts;
    keys: string[];
    metrics_covered: string[];
  };
  conclusion: string;
  trace: {
    project_id?: string;
    workflow_id?: string | null;
    run_id?: string | null;
  };
}

export type ResultsEnvelope = ApiEnvelope<ResultsDigest>;

function assertThreshold(value: unknown, field: string): void {
  if (!isObject(value)) {
    throw new ResultsContractError(`${field} must be an object`);
  }
  assertString(value.calibration_status, `${field}.calibration_status`);
  assertNullableNumber(value.value, `${field}.value`);
  assertNullableString(value.operator, `${field}.operator`);
}

function assertThresholdOptional(value: unknown, field: string): void {
  if (value !== null && value !== undefined) assertThreshold(value, field);
}

function assertPerTargetStat(value: unknown, index: number): void {
  const field = `layers.per_target[${index}]`;
  if (!isObject(value)) {
    throw new ResultsContractError(`${field} must be an object`);
  }
  assertNonEmptyString(value.target, `${field}.target`);
  assertNumber(value.evaluated, `${field}.evaluated`);
  assertNumber(value.passed, `${field}.passed`);
  assertNullableNumber(value.pass_rate, `${field}.pass_rate`);
}

function assertPerTargetThreshold(value: unknown, index: number): void {
  const field = `layers.per_target_thresholds[${index}]`;
  if (!isObject(value)) {
    throw new ResultsContractError(`${field} must be an object`);
  }
  assertNonEmptyString(value.target, `${field}.target`);
  assertThreshold(value, field);
}

function assertLayer(value: unknown, index: number): void {
  const field = `layers[${index}]`;
  if (!isObject(value)) {
    throw new ResultsContractError(`${field} must be an object`);
  }
  assertNonEmptyString(value.key, `${field}.key`);
  assertString(value.metric, `${field}.metric`);
  assertString(value.direction, `${field}.direction`);
  assertString(value.scope, `${field}.scope`);
  assertNumber(value.evaluated, `${field}.evaluated`);
  assertNumber(value.passed, `${field}.passed`);
  assertNullableNumber(value.pass_rate, `${field}.pass_rate`);
  assertThresholdOptional(value.threshold, `${field}.threshold`);
  if (!Array.isArray(value.per_target)) {
    throw new ResultsContractError(`${field}.per_target must be an array`);
  }
  value.per_target.forEach(assertPerTargetStat);
  if (!Array.isArray(value.per_target_thresholds)) {
    throw new ResultsContractError(`${field}.per_target_thresholds must be an array`);
  }
  value.per_target_thresholds.forEach(assertPerTargetThreshold);
}

function assertFinalist(value: unknown, index: number): void {
  const field = `finalists[${index}]`;
  if (!isObject(value)) {
    throw new ResultsContractError(`${field} must be an object`);
  }
  assertNonEmptyString(value.candidate_id, `${field}.candidate_id`);
  assertNumber(value.rank, `${field}.rank`);
  assertNullableString(value.sequence, `${field}.sequence`);
  assertNullableString(value.status, `${field}.status`);
  assertNullableString(value.source_route, `${field}.source_route`);
  assertBoolean(value.hard_cleared, `${field}.hard_cleared`);
  assertStringArray(value.failed_layers, `${field}.failed_layers`);
  assertNullableNumber(value.desirability, `${field}.desirability`);
  assertBoolean(value.pareto_front, `${field}.pareto_front`);
  assertNullableString(value.top_margin_metric, `${field}.top_margin_metric`);
  assertStringArray(value.targets, `${field}.targets`);
  if (!isObject(value.metrics)) {
    throw new ResultsContractError(`${field}.metrics must be an object`);
  }
  assertNullableString(value.battery_event_id, `${field}.battery_event_id`);
  assertNullableString(value.battery_timestamp, `${field}.battery_timestamp`);
}

function assertCounts(value: unknown, field: string): void {
  if (!isObject(value)) {
    throw new ResultsContractError(`${field} must be an object`);
  }
  assertNumber(value.calibrated, `${field}.calibrated`);
  assertNumber(value.provisional, `${field}.provisional`);
  assertNumber(value.unavailable, `${field}.unavailable`);
}

export class ResultsContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ResultsContractError";
  }
}

export function parseResultsEnvelope(value: unknown): ResultsEnvelope {
  if (!isObject(value)) {
    throw new ResultsContractError("Results response must be an object");
  }
  assertNonEmptyString(value.request_id, "request_id");
  if (!isObject(value.data)) {
    throw new ResultsContractError("data must be an object");
  }

  const data = value.data;
  if (data.schema_version !== RESULTS_SCHEMA_VERSION) {
    throw new ResultsContractError(
      `Expected ${RESULTS_SCHEMA_VERSION}`,
    );
  }
  if (!isObject(data.project)) {
    throw new ResultsContractError("project must be an object");
  }
  assertNonEmptyString(data.project.project_id, "project.project_id");
  assertString(data.project.name, "project.name");
  assertStringArray(data.project.targets, "project.targets");

  if (data.run !== null && !isObject(data.run)) {
    throw new ResultsContractError("run must be an object or null");
  }

  if (!isObject(data.summary)) {
    throw new ResultsContractError("summary must be an object");
  }
  for (const field of [
    "candidates_total",
    "candidates_evaluated",
    "candidates_pending_prediction",
    "hard_cleared",
    "n_shortlisted",
    "n_pareto_front",
    "layers_total",
    "layers_evaluated",
  ] as const) {
    assertNumber(data.summary[field], `summary.${field}`);
  }
  assertNullableNumber(data.summary.hard_clearance_rate, "summary.hard_clearance_rate");
  if (!["none", "demo_fixture", "real"].includes(data.summary.data_basis as string)) {
    throw new ResultsContractError("summary.data_basis must be none, demo_fixture, or real");
  }
  assertCounts(data.summary.counts, "summary.counts");
  assertStringArray(data.summary.keys, "summary.keys");
  assertStringArray(data.summary.metrics_covered, "summary.metrics_covered");

  if (!Array.isArray(data.layers)) {
    throw new ResultsContractError("layers must be an array");
  }
  data.layers.forEach(assertLayer);
  if (!Array.isArray(data.finalists)) {
    throw new ResultsContractError("finalists must be an array");
  }
  data.finalists.forEach(assertFinalist);
  if (!Array.isArray(data.pending_candidates)) {
    throw new ResultsContractError("pending_candidates must be an array");
  }

  if (!isObject(data.thresholds)) {
    throw new ResultsContractError("thresholds must be an object");
  }
  assertCounts(data.thresholds.counts, "thresholds.counts");
  assertStringArray(data.thresholds.keys, "thresholds.keys");
  assertStringArray(data.thresholds.metrics_covered, "thresholds.metrics_covered");

  assertNonEmptyString(data.conclusion, "conclusion");
  if (!isObject(data.trace)) {
    throw new ResultsContractError("trace must be an object");
  }

  return value as unknown as ResultsEnvelope;
}

function resultsUrl(apiOrigin: string | undefined): string {
  if (!apiOrigin) return RESULTS_ENDPOINT;
  return `${apiOrigin.replace(/\/$/, "")}${RESULTS_ENDPOINT}`;
}

export interface FetchResultsOptions {
  apiOrigin?: string;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
}

export async function fetchResults(
  options: FetchResultsOptions = {},
): Promise<ResultsEnvelope> {
  const fetchImpl = options.fetchImpl ?? fetch;
  const response = await fetchImpl(resultsUrl(options.apiOrigin), {
    signal: options.signal,
  });
  if (!response.ok) {
    throw new Error(`Results request failed with HTTP ${response.status}`);
  }
  return parseResultsEnvelope(await response.json());
}
