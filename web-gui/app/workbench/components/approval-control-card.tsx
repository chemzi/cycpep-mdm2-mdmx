import type {
  ApprovalControlProjection,
  ApprovalCeilings,
  ManualApprovalRequest,
} from "../control-domain";

type CeilingKey = keyof ApprovalCeilings;

export interface ApprovalControlCardProps {
  approval: ApprovalControlProjection;
  request: ManualApprovalRequest;
  autoApprovalEligible: boolean;
  autoApprovalSelected?: boolean;
  pending?: boolean;
  error?: string | null;
  onRequestChange?: (request: ManualApprovalRequest) => void;
  onAutoApprovalChange?: (enabled: boolean) => void;
  onApprove?: (request: ManualApprovalRequest) => void;
}

const CEILINGS: ReadonlyArray<[CeilingKey, string, string]> = [
  ["max_gpu_job_slots", "GPU slots", "1"],
  ["max_gpu_minutes", "GPU minutes", "0.1"],
  ["max_design_proposals", "Design proposals", "1"],
  ["max_prediction_candidates", "Prediction candidates", "1"],
];

function estimateLabel(minutes: number | null, status: string) {
  if (minutes === null || status !== "estimated") return "Pending benchmark";
  return `${minutes.toLocaleString(undefined, { maximumFractionDigits: 2 })} GPU-min`;
}

function updateCeiling(
  request: ManualApprovalRequest,
  key: CeilingKey,
  value: string,
): ManualApprovalRequest {
  const parsed = value === "" ? null : Number(value);
  return {
    ...request,
    ceilings: {
      ...request.ceilings,
      [key]: parsed !== null && Number.isFinite(parsed) ? parsed : null,
    },
  };
}

function requestReady(
  request: ManualApprovalRequest,
  approval: ApprovalControlProjection,
) {
  const estimate = approval.budget.gpu_minutes;
  const ceiling = request.ceilings.max_gpu_minutes;
  return Boolean(
    request.approver.trim()
    && request.justification.trim()
    && approval.budget.gpu_minutes_status === "estimated"
    && estimate !== null
    && ceiling !== null
    && ceiling >= estimate,
  );
}

export function ApprovalControlCard({
  approval,
  request,
  autoApprovalEligible,
  autoApprovalSelected = false,
  pending = false,
  error = null,
  onRequestChange = () => undefined,
  onAutoApprovalChange = () => undefined,
  onApprove = () => undefined,
}: ApprovalControlCardProps) {
  const estimateAvailable = approval.budget.gpu_minutes_status === "estimated"
    && approval.budget.gpu_minutes !== null;
  const ready = requestReady(request, approval);

  return <article className="approval-control-card" aria-labelledby="approval-control-title">
    <header className="approval-control-header">
      <div>
        <p className="approval-control-kicker">Exact plan approval</p>
        <h1 id="approval-control-title">Release the current compute plan</h1>
      </div>
      <span className="approval-control-state" role="status" aria-live="assertive">
        Awaiting approval · action required
      </span>
    </header>

    <div className="approval-control-binding" aria-label="Immutable plan binding">
      <div><span>Plan</span><code>{approval.plan_id}</code></div>
      <div><span>Digest</span><code>{approval.plan_sha256}</code></div>
      <div><span>Required scope</span><strong>{approval.required_task_ids.join(" · ")}</strong></div>
    </div>

    <div className="approval-control-body">
      <section className="approval-control-resources" aria-labelledby="approval-resources-title">
        <header>
          <div><p>Planner resource request</p><h2 id="approval-resources-title">Required tasks</h2></div>
          <span>{approval.tasks.length} task{approval.tasks.length === 1 ? "" : "s"}</span>
        </header>
        <div className="approval-resource-table" role="table" aria-label="Required task resources">
          <div className="approval-resource-row is-heading" role="row">
            <span role="columnheader">Task / action</span><span role="columnheader">Slots</span>
            <span role="columnheader">Proposals</span><span role="columnheader">Candidates</span>
            <span role="columnheader">Estimate</span>
          </div>
          {approval.tasks.map((task) => <div className="approval-resource-row" role="row" key={task.task_id}>
            <span role="cell"><code>{task.task_id}</code><small>{task.action}</small></span>
            <span role="cell">{task.gpu_job_slots}</span>
            <span role="cell">{task.proposal_count}</span>
            <span role="cell">{task.candidate_limit}</span>
            <span role="cell" data-estimate-status={task.estimate_status}>
              <strong>{estimateLabel(task.estimated_gpu_minutes, task.estimate_status)}</strong>
              <small>{task.estimate_status} · {task.calibration_status}</small>
            </span>
          </div>)}
        </div>
        <dl className="approval-budget-register">
          <div><dt>Total GPU budget</dt><dd>{estimateLabel(approval.budget.gpu_minutes, approval.budget.gpu_minutes_status)}</dd></div>
          <div><dt>Estimator</dt><dd>{approval.budget.estimator_version ?? "Unavailable"}</dd></div>
          <div><dt>Calibration</dt><dd>{approval.budget.calibration_status}</dd></div>
        </dl>
      </section>

      <form className="approval-control-form" onSubmit={(event) => {
        event.preventDefault();
        if (ready && !pending) onApprove(request);
      }}>
        <header><p>Operator authorization</p><h2>Manual ceilings</h2></header>
        <label className="approval-control-field">
          <span>Approver</span>
          <input value={request.approver} disabled={pending} onChange={(event) => onRequestChange({ ...request, approver: event.target.value })} />
        </label>
        <label className="approval-control-field">
          <span>Justification</span>
          <textarea value={request.justification} disabled={pending} rows={2} onChange={(event) => onRequestChange({ ...request, justification: event.target.value })} />
        </label>
        <div className="approval-control-ceilings">
          {CEILINGS.map(([key, label, step]) => <label className="approval-control-field" key={key}>
            <span>{label}</span>
            <input type="number" min="0" step={step} value={request.ceilings[key] ?? ""} disabled={pending} onChange={(event) => onRequestChange(updateCeiling(request, key, event.target.value))} />
          </label>)}
        </div>
        {autoApprovalEligible ? <label className="approval-auto-option" data-disabled={!estimateAvailable || undefined}>
          <input type="checkbox" checked={autoApprovalSelected} disabled={!estimateAvailable || pending} onChange={(event) => onAutoApprovalChange(event.target.checked)} />
          <span><strong>Auto-approve first GPU gate</strong><small>{estimateAvailable ? "Apply these ceilings only to this first gate." : "Available after a finite GPU estimate is returned."}</small></span>
        </label> : null}
        {error ? <p className="approval-control-error" role="alert">{error}</p> : null}
        <button className="approval-control-action" type="submit" disabled={!ready || pending}>
          {pending ? "Recording approval…" : "Approve and continue"}<span aria-hidden="true">→</span>
        </button>
      </form>
    </div>
  </article>;
}
