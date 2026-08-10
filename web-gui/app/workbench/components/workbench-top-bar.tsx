import type { WorkbenchReadModel } from "../domain";
import type { WorkbenchRequestStatus } from "../request-lifecycle";
import { BrandMark } from "./brand-mark";

export interface WorkbenchTopBarProps {
  data: WorkbenchReadModel;
  requestStatus: WorkbenchRequestStatus;
  refreshError: string | null;
  autoRefreshEnabled: boolean;
  onRefresh: () => void;
  onAutoRefreshChange: (enabled: boolean) => void;
}

function bindingLabel(data: WorkbenchReadModel): string {
  if (data.workflow && data.run) return data.run.status ?? data.workflow.status ?? "Status unavailable";
  if (data.blockers.items.some((blocker) => blocker.code === "workflow_binding_invalid")) {
    return "Current run unavailable";
  }
  return "No active run";
}

export function WorkbenchTopBar({
  data,
  requestStatus,
  refreshError,
  autoRefreshEnabled,
  onRefresh,
  onAutoRefreshChange,
}: WorkbenchTopBarProps) {
  const refreshing = requestStatus === "refreshing";
  const stale = requestStatus === "stale-after-error";

  return (
    <header className="workbench-top-bar" aria-label="Workbench context">
      <div className="workbench-product">
        <BrandMark className="workbench-product-mark" />
        <span>CycPep Workbench</span>
      </div>
      <div className="workbench-project-context">
        <strong>{data.project.name ?? data.project.project_id}</strong>
        <span>{data.project.targets.join(" / ") || "Targets unavailable"}</span>
      </div>
      <dl className="workbench-run-summary">
        <div><dt>Workflow</dt><dd>{data.workflow?.workflow_id ?? "Unavailable"}</dd></div>
        <div><dt>Run</dt><dd>{data.run?.run_id ?? "Unavailable"}</dd></div>
        <div><dt>Status</dt><dd>{bindingLabel(data)}</dd></div>
      </dl>
      <div className="workbench-attention">
        {data.blockers.returned > 0 ? <span>Needs attention · {data.blockers.returned}</span> : <span>No blockers returned</span>}
        {stale ? <span role="status">Data may be out of date · {refreshError}</span> : null}
      </div>
      <div className="workbench-refresh" aria-label="Workbench refresh settings">
        <label>
          <input
            type="checkbox"
            checked={autoRefreshEnabled}
            onChange={(event) => onAutoRefreshChange(event.target.checked)}
          />
          Auto refresh
        </label>
        <button type="button" onClick={onRefresh} disabled={refreshing}>
          {refreshing ? "Refreshing…" : "Refresh"}
        </button>
      </div>
    </header>
  );
}
