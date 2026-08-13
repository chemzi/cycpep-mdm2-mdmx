import { WORKBENCH_SCHEMA_VERSION } from "./domain";
import type { BoundedCollection, WorkbenchReadModel } from "./domain";

function emptyCollection<T>(scope: string): BoundedCollection<T> {
  return { scope, total: 0, returned: 0, truncated: false, items: [] };
}

export function createEmptyMonitoringModel(target: string): WorkbenchReadModel {
  const projectId = `project-${target.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-") || "new"}`;
  return {
    schema_version: WORKBENCH_SCHEMA_VERSION,
    project: { project_id: projectId, name: `${target} cyclic peptide campaign`, targets: [target] },
    workflow: null,
    run: null,
    tasks: emptyCollection("current_run"),
    executions: emptyCollection("current_run"),
    transactions: emptyCollection("current_run"),
    candidates: emptyCollection("project"),
    evidence: emptyCollection("project"),
    artifacts: emptyCollection("project"),
    protocols: emptyCollection("project"),
    trace: { project_id: projectId },
    blockers: emptyCollection("workbench"),
  };
}
