export const WORKBENCH_SCHEMA_VERSION = "frontend.workbench.v2" as const;

export type WorkbenchSchemaVersion = typeof WORKBENCH_SCHEMA_VERSION;
export type RunRelation = "current_run" | "historical_run" | "unlinked";

export interface ApiEnvelope<T> {
  request_id: string;
  data: T;
}

export interface BoundedCollection<T> {
  scope: string;
  total: number;
  returned: number;
  truncated: boolean;
  items: T[];
}

export interface TraceLink {
  project_id?: string;
  workflow_id?: string | null;
  run_id?: string | null;
  plan_id?: string;
  task_id?: string;
  attempt_id?: string;
  transaction_id?: string;
  candidate_id?: string;
  artifact_id?: string;
  parent_event_id?: string;
}

export interface ProtocolView {
  name?: string;
  version?: string;
  integrity_identity?: string;
}

export interface ProjectView {
  project_id: string;
  name?: string;
  targets: string[];
}

export interface WorkflowView {
  workflow_id?: string;
  plan_id?: string;
  status?: string;
}

export interface RunView {
  run_id?: string;
  workflow_id?: string;
  plan_id?: string;
  status?: string;
  created_at?: string;
  updated_at?: string;
}

export interface ActionView {
  name: string;
  executable: boolean;
  handler_available: boolean;
  resource_class: string | null;
  output_roles: string[];
}

export interface TaskView {
  task_id?: string;
  agent?: string;
  kind?: string;
  disposition?: string;
  depends_on: string[];
  status?: string;
  action: ActionView;
  availability: {
    available: boolean;
    reason_codes: string[];
  };
  approval: {
    required: boolean;
    state: string;
  };
  execution_gate: {
    status?: string;
  };
  protocol?: ProtocolView;
}

export interface StructuredError {
  code?: string;
  message?: string;
  component?: string;
  retryable?: boolean;
}

export interface ExecutionView {
  task_id: string;
  status?: string;
  attempts: number;
  attempt_id?: string | null;
  worker_id?: string | null;
  transaction_visibility: string;
  error?: StructuredError | null;
}

export interface TransactionView extends TraceLink {
  transaction_id?: string;
  status?: string;
  created_at?: string;
  updated_at?: string;
  error?: StructuredError;
}

export type MetricValue =
  | string
  | number
  | boolean
  | null
  | MetricValue[]
  | { [key: string]: MetricValue };

export interface CandidateAssociationLimitation {
  code: string;
  summary: string;
}

export interface CandidateStatusOwner {
  run_id: string;
  run_relation: RunRelation;
}

export interface CandidateStructureAssociation {
  artifact_id: string;
  artifact_type: string;
  role?: string;
  content_link?: string;
}

export interface CandidateShortlistAssociation {
  event_id?: string;
  candidate_id?: string;
  passed: boolean;
  reason: string;
  desirability?: number | null;
  pareto_front?: boolean;
  top_margin_metric?: string | null;
}

export interface CandidateAssociations {
  evidence_total: number;
  artifact_total: number;
  artifact_ids: string[];
  complete: boolean;
  limitations: CandidateAssociationLimitation[];
  status_owner?: CandidateStatusOwner;
  structures: CandidateStructureAssociation[];
  shortlist: CandidateShortlistAssociation[];
}

export interface CandidateView {
  candidate_id?: string;
  sequence?: string;
  source_route?: string;
  status?: string;
  final_status?: string;
  created_at?: string;
  updated_at?: string;
  metrics?: Record<string, MetricValue>;
  trace: TraceLink;
  run_relation: RunRelation;
  protocol?: ProtocolView;
  associations?: CandidateAssociations;
}

export interface ShortlistItem {
  candidate_id: string;
  passed: boolean;
  desirability: number | null;
  pareto_front: boolean;
  reason: string;
  top_margin_metric: string | null;
}

export interface ExplorationCalibration {
  calibrated: number;
  provisional: number;
  unavailable: number;
}

export interface EvidenceBase {
  event_id?: string;
  timestamp?: string;
  agent?: string;
  event_type?: string;
  phase?: string;
  round?: number;
  code?: string;
  component?: string;
  retryable?: boolean;
  targets?: string[];
  blocks?: boolean;
  message?: string;
  trace: TraceLink;
  run_relation: RunRelation;
  protocol?: ProtocolView;
}

export interface ExplorationShortlistEvidence extends EvidenceBase {
  event_type: "exploration_shortlist";
  k?: number;
  n_evaluated: number;
  n_passed: number;
  shortlist: ShortlistItem[];
  calibration: ExplorationCalibration;
  source_event_ids: string[];
  unmapped_metrics: string[];
}

export type EvidenceView = EvidenceBase | ExplorationShortlistEvidence;

export interface ArtifactView {
  artifact_id?: string;
  artifact_type?: string;
  role?: string;
  size_bytes?: number;
  sha256?: string;
  schema_version?: string;
  created_at?: string;
  producer_task_id?: string;
  input_artifact_ids?: string[];
  content_link?: string;
  trace: TraceLink;
  run_relation: RunRelation;
  protocol?: ProtocolView;
}

export interface BlockerView {
  code: string;
  scope: string;
  summary: string;
  workflow_id?: string;
  run_id?: string;
  task_id?: string;
  transaction_id?: string;
}

export interface WorkbenchReadModel {
  schema_version: WorkbenchSchemaVersion;
  project: ProjectView;
  workflow: WorkflowView | null;
  run: RunView | null;
  tasks: BoundedCollection<TaskView>;
  executions: BoundedCollection<ExecutionView>;
  transactions: BoundedCollection<TransactionView>;
  candidates: BoundedCollection<CandidateView>;
  evidence: BoundedCollection<EvidenceView>;
  artifacts: BoundedCollection<ArtifactView>;
  protocols: BoundedCollection<ProtocolView>;
  trace: TraceLink;
  blockers: BoundedCollection<BlockerView>;
}

export type WorkbenchEnvelope = ApiEnvelope<WorkbenchReadModel>;

export function isExplorationShortlistEvidence(
  evidence: EvidenceView,
): evidence is ExplorationShortlistEvidence {
  return evidence.event_type === "exploration_shortlist";
}
