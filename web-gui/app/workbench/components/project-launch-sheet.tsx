"use client";

import { useEffect, useRef, useState } from "react";
import type {
  FirstGateAutoApprovalPolicy,
  IdentifierType,
  ProjectLaunchRequest,
} from "../control-domain";

export type LaunchApprovalMode = "manual" | "automatic";
export type LaunchMutation = "resolve" | "approve" | "launch";

export interface ProjectReviewProjection {
  draft_id: string;
  project_id: string;
  name: string;
  target_identifier: string;
  resolved_identity: string;
  structure_status: string;
  review_status: "review_required" | "ready" | "approved";
  blockers: string[];
  uncertainties: string[];
}

export interface ProjectLaunchSubmission {
  request: ProjectLaunchRequest;
  review: ProjectReviewProjection;
  approval_mode: LaunchApprovalMode;
  manual_approver: string;
  manual_justification: string;
}

export interface ProjectLaunchSheetProps {
  onClose: () => void;
  review?: ProjectReviewProjection | null;
  mutation?: LaunchMutation | null;
  error?: string | null;
  launcherRunId?: string | null;
  initialRequest?: ProjectLaunchRequest;
  onResolveDraft?: (request: ProjectLaunchRequest) => void;
  onApproveDraft?: (review: ProjectReviewProjection) => void;
  onCreateAndLaunch?: (submission: ProjectLaunchSubmission) => void;
  /** Temporary compatibility seam while the page adopts the typed callback. */
  onLaunch?: (target: string) => void;
}

type CeilingInputs = {
  max_gpu_minutes: string;
  max_gpu_job_slots: string;
  max_design_proposals: string;
  max_prediction_candidates: string;
};

const IDENTIFIER_TYPES: ReadonlyArray<[IdentifierType, string]> = [
  ["auto", "Auto detect"], ["gene", "Gene symbol"],
  ["uniprot", "UniProt ID"], ["pdb", "PDB ID"],
];

const EMPTY_CEILINGS: CeilingInputs = {
  max_gpu_minutes: "",
  max_gpu_job_slots: "",
  max_design_proposals: "",
  max_prediction_candidates: "",
};

function validNonnegative(value: string) {
  return value !== "" && Number.isFinite(Number(value)) && Number(value) >= 0;
}

export function isLaunchRequestReady(
  target: string,
  mode: LaunchApprovalMode,
  ceilings: CeilingInputs,
  reviewApproved = false,
  approver = "",
  justification = "",
): boolean {
  if (!target.trim() || !reviewApproved || !approver.trim() || !justification.trim()) return false;
  if (!validNonnegative(ceilings.max_gpu_job_slots)
    || !validNonnegative(ceilings.max_design_proposals)
    || !validNonnegative(ceilings.max_prediction_candidates)) return false;
  if (!(Number(ceilings.max_gpu_minutes) > 0)) return false;
  return mode === "manual" || mode === "automatic";
}

function useDialogFocus(onClose: () => void) {
  const targetInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    targetInput.current?.focus();
    function handleKeys(event: KeyboardEvent) {
      if (event.key === "Escape") return onClose();
      if (event.key !== "Tab") return;
      const controls = Array.from(document.querySelectorAll<HTMLElement>(
        ".launch-overlay button:not(:disabled), .launch-overlay input:not(:disabled), .launch-overlay select:not(:disabled), .launch-overlay textarea:not(:disabled)",
      ));
      const first = controls[0]; const last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
    window.addEventListener("keydown", handleKeys);
    return () => { window.removeEventListener("keydown", handleKeys); previous?.focus(); };
  }, [onClose]);
  return targetInput;
}

function TargetStage({ request, pending, inputRef, onChange, onResolve }: {
  request: ProjectLaunchRequest;
  pending: boolean;
  inputRef: React.RefObject<HTMLInputElement | null>;
  onChange: (request: ProjectLaunchRequest) => void;
  onResolve: () => void;
}) {
  const options = request.options;
  return <section className="launch-ledger-stage is-active" aria-labelledby="launch-target-heading">
    <header><span className="launch-stage-marker" aria-hidden="true">01</span><div><p>Target</p><h2 id="launch-target-heading">Biological identity</h2></div></header>
    <label className="launch-field launch-target-field"><span>Target identifier</span><input ref={inputRef} value={request.target_identifier} disabled={pending} onChange={(event) => onChange({ ...request, target_identifier: event.target.value })} placeholder="e.g. MDM2, Q00987 or 1YCR" autoComplete="off" /></label>
    <div className="launch-inline-fields">
      <label className="launch-field"><span>Identifier type</span><select value={options.identifier_type} disabled={pending} onChange={(event) => onChange({ ...request, options: { ...options, identifier_type: event.target.value as IdentifierType } })}>{IDENTIFIER_TYPES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label className="launch-field"><span>Organism ID</span><input type="number" min="1" value={options.organism_id} disabled={pending} onChange={(event) => onChange({ ...request, options: { ...options, organism_id: Number(event.target.value) } })} /></label>
    </div>
    <label className="launch-field launch-objective-field"><span>Objective</span><input value={options.objective} disabled={pending} onChange={(event) => onChange({ ...request, options: { ...options, objective: event.target.value } })} /></label>
    <button className="launch-stage-action" type="button" disabled={pending || !request.target_identifier.trim() || options.organism_id < 1 || !options.objective.trim()} onClick={onResolve}>{pending ? "Resolving…" : "Resolve target"}<span aria-hidden="true">→</span></button>
  </section>;
}

function ReviewStage({ review, targetIdentifier, pending, onApprove }: {
  review: ProjectReviewProjection | null;
  targetIdentifier: string;
  pending: boolean;
  onApprove: () => void;
}) {
  const current = review?.target_identifier === targetIdentifier.trim();
  return <section className="launch-ledger-stage" aria-labelledby="launch-review-heading">
    <header><span className="launch-stage-marker" aria-hidden="true">02</span><div><p>Project</p><h2 id="launch-review-heading">Review before launch</h2></div></header>
    <dl className="launch-review-register">
      <div><dt>Project</dt><dd>{current ? review?.name : "Awaiting target resolution"}</dd></div>
      <div><dt>Resolved identity</dt><dd>{current ? review?.resolved_identity : "Unavailable"}</dd></div>
      <div><dt>Structure readiness</dt><dd>{current ? review?.structure_status : "Unavailable"}</dd></div>
      <div><dt>Formal review</dt><dd>{current ? review?.review_status : "Not started"}</dd></div>
    </dl>
    {current && review?.uncertainties.length ? <p className="launch-pending-copy">Uncertainties: {review.uncertainties.join(" · ")}</p> : null}
    {current && review?.blockers.length ? <p className="launch-pending-copy" role="alert">Review blockers: {review.blockers.join(" · ")}</p> : null}
    {current && review?.review_status === "ready" ? <button className="launch-stage-action" type="button" disabled={pending} onClick={onApprove}>{pending ? "Approving…" : "Approve project"}<span aria-hidden="true">→</span></button> : null}
  </section>;
}

function ComputeStage({ mode, ceilings, approver, justification, pending, onModeChange, onCeilingChange, onApproverChange, onJustificationChange }: {
  mode: LaunchApprovalMode;
  ceilings: CeilingInputs;
  approver: string;
  justification: string;
  pending: boolean;
  onModeChange: (mode: LaunchApprovalMode) => void;
  onCeilingChange: (key: keyof CeilingInputs, value: string) => void;
  onApproverChange: (value: string) => void;
  onJustificationChange: (value: string) => void;
}) {
  const fields: Array<[keyof CeilingInputs, string, string]> = [
    ["max_gpu_minutes", "GPU minutes", "0.1"], ["max_gpu_job_slots", "GPU slots", "1"],
    ["max_design_proposals", "Design proposals", "1"], ["max_prediction_candidates", "Candidates", "1"],
  ];
  return <section className="launch-ledger-stage" aria-labelledby="launch-compute-heading">
    <header><span className="launch-stage-marker" aria-hidden="true">03</span><div><p>Compute</p><h2 id="launch-compute-heading">First GPU gate</h2></div></header>
    <fieldset className="launch-approval-options"><legend>Approval mode</legend>
      <label data-selected={mode === "manual" || undefined}><input type="radio" name="approval-mode" checked={mode === "manual"} disabled={pending} onChange={() => onModeChange("manual")} /><span><strong>Review manually</strong><small>Pause before heavy Prediction.</small></span></label>
      <label data-selected={mode === "automatic" || undefined}><input type="radio" name="approval-mode" checked={mode === "automatic"} disabled={pending} onChange={() => onModeChange("automatic")} /><span><strong>Auto-approve first GPU gate</strong><small>Only within these ceilings.</small></span></label>
    </fieldset>
    <label className="launch-field"><span>Approver</span><input value={approver} disabled={pending} onChange={(event) => onApproverChange(event.target.value)} /></label>
    <label className="launch-field launch-justification-field"><span>Justification</span><textarea rows={2} value={justification} disabled={pending} onChange={(event) => onJustificationChange(event.target.value)} /></label>
    <div className="launch-ceilings">{fields.map(([key, label, step]) => <label className="launch-field" key={key}><span>{label}</span><input type="number" value={ceilings[key]} placeholder="Set limit" min={key === "max_gpu_minutes" ? "0.1" : "0"} step={step} disabled={pending} onChange={(event) => onCeilingChange(key, event.target.value)} /></label>)}</div>
    <p className="launch-field-note">This gate follows Initial Design and precedes heavy Prediction. Later plans still require review.</p>
  </section>;
}

function autoPolicy(mode: LaunchApprovalMode, approver: string, justification: string, ceilings: CeilingInputs): FirstGateAutoApprovalPolicy | null {
  if (mode !== "automatic") return null;
  return {
    approver: approver.trim(), justification: justification.trim(), ceilings: {
      max_gpu_job_slots: Number(ceilings.max_gpu_job_slots),
      max_gpu_minutes: Number(ceilings.max_gpu_minutes),
      max_design_proposals: Number(ceilings.max_design_proposals),
      max_prediction_candidates: Number(ceilings.max_prediction_candidates),
    },
  };
}

export function ProjectLaunchSheet({ onClose, review = null, mutation = null, error = null, launcherRunId = null, initialRequest, onResolveDraft = () => undefined, onApproveDraft = () => undefined, onCreateAndLaunch, onLaunch }: ProjectLaunchSheetProps) {
  const targetInput = useDialogFocus(onClose);
  const [request, setRequest] = useState<ProjectLaunchRequest>(() => initialRequest ?? ({ target_identifier: "", options: { identifier_type: "auto", organism_id: 9606, epitope: null, objective: "binder", launcher_run_id: launcherRunId, first_gate_auto_policy: null } }));
  const [mode, setMode] = useState<LaunchApprovalMode>("manual");
  const [ceilings, setCeilings] = useState<CeilingInputs>(EMPTY_CEILINGS);
  const [approver, setApprover] = useState("");
  const [justification, setJustification] = useState("");
  const pending = mutation !== null;
  const reviewCurrent = review?.target_identifier === request.target_identifier.trim();
  const approved = reviewCurrent && review?.review_status === "approved";
  const ready = isLaunchRequestReady(request.target_identifier, mode, ceilings, approved, approver, justification);
  const resolvedRequest = { ...request, options: { ...request.options, launcher_run_id: launcherRunId, first_gate_auto_policy: autoPolicy(mode, approver, justification, ceilings) } };
  function updateCeiling(key: keyof CeilingInputs, value: string) { setCeilings((current) => ({ ...current, [key]: value })); }

  return <section className="launch-overlay" role="dialog" aria-modal="true" aria-labelledby="launch-title"><div className="launch-sheet">
    <header className="launch-sheet-header"><div className="launch-sheet-kicker" aria-label="Project ignition"><span aria-hidden="true" />CycPep / project ignition</div><div className="launch-sheet-dismiss"><button type="button" className="launch-text-button" onClick={onClose}>View existing tasks</button><button type="button" className="launch-close-button" onClick={onClose} aria-label="Close new project"><span aria-hidden="true">×</span></button></div></header>
    <form className="launch-sheet-body" onSubmit={(event) => { event.preventDefault(); if (!ready || !review) return; const submission = { request: resolvedRequest, review, approval_mode: mode, manual_approver: approver.trim(), manual_justification: justification.trim() } satisfies ProjectLaunchSubmission; onCreateAndLaunch?.(submission); onLaunch?.(request.target_identifier.trim()); }}>
      <div className="launch-introduction"><p className="launch-eyebrow">New cyclic-peptide campaign</p><h1 id="launch-title">Start with a target.</h1><p>Resolve one biological identity, approve the formal project review, then hand the campaign to the workflow.</p></div>
      <div className="launch-ledger" aria-label="Target to compute launch ledger"><TargetStage request={request} pending={mutation === "resolve"} inputRef={targetInput} onChange={setRequest} onResolve={() => onResolveDraft({ ...request, options: { ...request.options, launcher_run_id: launcherRunId, first_gate_auto_policy: null } })} /><ReviewStage review={review} targetIdentifier={request.target_identifier} pending={mutation === "approve"} onApprove={() => reviewCurrent && review && onApproveDraft(review)} /><ComputeStage mode={mode} ceilings={ceilings} approver={approver} justification={justification} pending={pending} onModeChange={setMode} onCeilingChange={updateCeiling} onApproverChange={setApprover} onJustificationChange={setJustification} /></div>
      {error ? <p className="launch-control-error" role="alert">{error}</p> : null}
      <footer className="launch-sheet-footer"><div className="launch-interface-status" role="status"><span data-ready={ready || undefined} aria-hidden="true" />{approved ? (ready ? "Approved project and compute ceilings are ready." : "Complete operator identity and compute ceilings.") : "Resolve and approve the project before launch."}</div><button className="launch-primary-action" type="submit" disabled={!ready || pending}>{mutation === "launch" ? "Launching…" : "Create and launch"}<span aria-hidden="true">→</span></button></footer>
    </form>
  </div></section>;
}
