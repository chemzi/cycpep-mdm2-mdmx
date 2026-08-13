export type IdentifierType = "auto" | "gene" | "uniprot" | "pdb";
export type ResourceClass = "cpu" | "network_cpu" | "gpu";
export type EstimateStatus =
  | "estimated"
  | "benchmark_required"
  | "unavailable"
  | "not_applicable";
export type CalibrationStatus =
  | "calibrated"
  | "provisional"
  | "pending"
  | "unavailable"
  | "not_applicable";

export interface ApprovalCeilings {
  max_gpu_job_slots: number | null;
  max_gpu_minutes: number | null;
  max_design_proposals: number | null;
  max_prediction_candidates: number | null;
}

export interface AutoApprovalCeilings {
  max_gpu_job_slots: number;
  max_gpu_minutes: number;
  max_design_proposals: number;
  max_prediction_candidates: number;
}

export interface FirstGateAutoApprovalPolicy {
  approver: string;
  justification: string;
  ceilings: AutoApprovalCeilings;
}

export interface ProjectLaunchOptions {
  identifier_type: IdentifierType;
  organism_id: number;
  epitope: string | null;
  objective: string;
  launcher_run_id: string | null;
  first_gate_auto_policy: FirstGateAutoApprovalPolicy | null;
}

export interface ProjectLaunchRequest {
  target_identifier: string;
  options: ProjectLaunchOptions;
}

export interface TaskResourceProjection {
  task_id: string;
  action: string;
  resource_class: ResourceClass;
  gpu_job_slots: number;
  proposal_count: number;
  candidate_limit: number;
  estimated_gpu_minutes: number | null;
  estimate_status: EstimateStatus;
  estimator_version: string | null;
  calibration_status: CalibrationStatus;
}

export interface ApprovalBudgetProjection {
  gpu_minutes: number | null;
  gpu_minutes_status: EstimateStatus;
  estimator_version: string | null;
  calibration_status: CalibrationStatus;
}

export interface ApprovalControlProjection {
  launcher_run_id: string;
  project_id: string;
  approved_content_binding: string;
  plan_id: string;
  plan_sha256: string;
  source_kind: string;
  required_task_ids: string[];
  tasks: TaskResourceProjection[];
  budget: ApprovalBudgetProjection;
}

export interface ManualApprovalRequest {
  launcher_run_id: string;
  project_id: string;
  approved_content_binding: string;
  plan_id: string;
  plan_sha256: string;
  required_task_ids: string[];
  approver: string;
  justification: string;
  ceilings: ApprovalCeilings;
}

export type ControlFailureCategory =
  | "binding"
  | "stale_plan"
  | "estimate"
  | "ceiling"
  | "review"
  | "launcher";

export type ControlFailureCode =
  | "control_binding_invalid"
  | "control_binding_conflict"
  | "approval_plan_stale"
  | "approval_estimate_unavailable"
  | "approval_ceiling_exceeded"
  | "project_review_blocked"
  | "launcher_run_not_found"
  | "launcher_operation_failed";

export interface ControlFailure {
  code: ControlFailureCode;
  category: ControlFailureCategory;
  component: string;
  message: string;
  ceiling: string | null;
}
