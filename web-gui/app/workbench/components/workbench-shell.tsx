import type { ReactNode } from "react";

import type { WorkbenchReadModel } from "../domain";
import type { WorkbenchRequestStatus } from "../request-lifecycle";
import { BlockerList, EmptyState, StaleState } from "./shared-states";

export interface WorkbenchShellProps {
  data: WorkbenchReadModel;
  requestStatus: WorkbenchRequestStatus;
  refreshError?: string | null;
  autoRefreshEnabled: boolean;
  onRefresh: () => void;
  onAutoRefreshChange: (enabled: boolean) => void;
  children?: ReactNode;
}

function bindingState(data: WorkbenchReadModel): "available" | "invalid" | "none" {
  if (data.workflow && data.run) return "available";
  if (data.blockers.items.some((item) => item.code === "workflow_binding_invalid")) {
    return "invalid";
  }
  return "none";
}

export function WorkbenchShell({
  data,
  requestStatus,
  refreshError,
  autoRefreshEnabled,
  onRefresh,
  onAutoRefreshChange,
  children,
}: WorkbenchShellProps) {
  const binding = bindingState(data);
  const refreshing = requestStatus === "refreshing";

  return <main className="workbench workbench-v2">
    <header className="workbench-header">
      <div className="wordmark">
        <span className="mark" aria-hidden="true"><i/><i/><i/></span>
        <div><b>CycPep Workbench</b><small>READ-ONLY SCIENTIFIC OBSERVABILITY</small></div>
      </div>
      <div className="project-context">
        <span>PROJECT</span>
        <b>{data.project.name ?? data.project.project_id}</b>
        <small>{data.project.project_id} · {data.project.targets.join(" · ") || "No targets recorded"}</small>
      </div>
      <div className="header-actions" aria-label="Workbench refresh settings">
        <span className="connection-pill online"><i/>{requestStatus}</span>
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

    {requestStatus === "stale-after-error" && refreshError
      ? <StaleState message={refreshError}/>
      : null}

    <section className="run-context" aria-labelledby="run-context-title">
      <h1 id="run-context-title">Workflow / run</h1>
      {binding === "available" ? <dl>
        <div><dt>Workflow</dt><dd>{data.workflow?.workflow_id ?? "Unavailable"}</dd></div>
        <div><dt>Plan</dt><dd>{data.run?.plan_id ?? data.workflow?.plan_id ?? "Unavailable"}</dd></div>
        <div><dt>Run</dt><dd>{data.run?.run_id ?? "Unavailable"}</dd></div>
        <div><dt>Overall status</dt><dd>{data.run?.status ?? "Unavailable"}</dd></div>
      </dl> : <EmptyState
        title={binding === "invalid" ? "Workflow binding invalid" : "No current run"}
        detail={binding === "invalid"
          ? "Workflow and run details are unavailable. Project-scoped Store data remains visible below."
          : "No current workflow run is recorded. Project-scoped Store data remains visible below."}
      />}
    </section>

    <BlockerList blockers={data.blockers.items} headingId="workbench-blockers-title"/>
    <section className="workbench-v2-content" aria-label="Project-scoped workbench data">
      {children}
    </section>
  </main>;
}
