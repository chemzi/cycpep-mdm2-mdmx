import { isExplorationShortlistEvidence } from "../domain";
import type { WorkbenchReadModel } from "../domain";
import type { WorkbenchSelection } from "../selection";
import { ArtifactTraceInspector } from "./artifact-trace";
import { CandidateWorkspace } from "./candidate-workspace";
import { EvidenceProvenance } from "./evidence-provenance";
import { ExecutionTransactionDetail } from "./execution-transaction";
import { ExplorationShortlist } from "./exploration-shortlist";

export interface WorkbenchPrimaryProps {
  data: WorkbenchReadModel;
  selection: WorkbenchSelection;
  onSelectionChange: (selection: WorkbenchSelection) => void;
}

function OverviewWorkspace({ data }: { data: WorkbenchReadModel }) {
  const invalid = data.blockers.items.some((blocker) => blocker.code === "workflow_binding_invalid");
  return (
    <article className="overview-workspace">
      <header>
        <span>Project workspace</span>
        <h1>{invalid ? "Workflow binding invalid" : data.run ? "Current scientific run" : "No active run"}</h1>
      </header>
      <p className="overview-summary">
        {invalid
          ? "Current run unavailable. Trustworthy project-scoped Store records remain available."
          : data.run
            ? `${data.workflow?.workflow_id ?? "Workflow unavailable"} · ${data.run.run_id ?? "Run unavailable"}`
            : "Project-scoped candidates and Evidence remain available."}
      </p>
      <dl className="overview-register" aria-label="Returned project records">
        <div><dt>Project</dt><dd>{data.project.name ?? data.project.project_id}</dd></div>
        <div><dt>Targets</dt><dd>{data.project.targets.join(" / ") || "Unavailable"}</dd></div>
        <div><dt>Candidates returned</dt><dd>{data.candidates.returned}</dd></div>
        <div><dt>Evidence returned</dt><dd>{data.evidence.returned}</dd></div>
        <div><dt>Artifacts returned</dt><dd>{data.artifacts.returned}</dd></div>
      </dl>
    </article>
  );
}

export function WorkbenchPrimary({ data, selection, onSelectionChange }: WorkbenchPrimaryProps) {
  if (selection.kind === "task") {
    const task = data.tasks.items.find((item) => item.task_id === selection.identity);
    if (!task) return <OverviewWorkspace data={data} />;
    return (
      <article className="selected-task-workspace">
        <header><span>Selected task</span><h1>{selection.identity}</h1></header>
        <dl className="domain-fields">
          <div><dt>Status</dt><dd>{task.status ?? "Unavailable"}</dd></div>
          <div><dt>Action</dt><dd><code>{task.action.name}</code></dd></div>
          <div><dt>Executable</dt><dd>{String(task.action.executable)}</dd></div>
          <div><dt>Handler available</dt><dd>{String(task.action.handler_available)}</dd></div>
          <div><dt>Approval</dt><dd>{task.approval.state}</dd></div>
          <div><dt>Execution gate</dt><dd>{task.execution_gate.status ?? "Unavailable"}</dd></div>
        </dl>
        <p><strong>Depends on:</strong> {task.depends_on.join(", ") || "None"}</p>
        <ExecutionTransactionDetail
          task={task}
          executions={data.executions.items}
          transactions={data.transactions.items}
          blockers={data.blockers.items}
        />
      </article>
    );
  }

  if (selection.kind === "candidate") {
    return <CandidateWorkspace
      candidates={data.candidates}
      evidence={data.evidence.items}
      artifacts={data.artifacts.items}
      selectedCandidateId={selection.identity}
      onSelectCandidate={(identity) => onSelectionChange({ kind: "candidate", identity })}
    />;
  }

  if (selection.kind === "evidence") {
    const evidence = data.evidence.items.find((item) => item.event_id === selection.identity);
    if (!evidence) return <OverviewWorkspace data={data} />;
    return (
      <div className="selected-evidence-workspace">
        {isExplorationShortlistEvidence(evidence) ? <ExplorationShortlist
          shortlist={evidence}
          evidence={data.evidence.items}
          headingId="selected-shortlist-title"
          passedHeadingId="selected-shortlist-passed-title"
          onSelectEvidence={(identity) => onSelectionChange({ kind: "evidence", identity })}
        /> : null}
        <EvidenceProvenance
          evidence={data.evidence.items}
          selectedEvidenceId={selection.identity}
          onSelectEvidence={(identity) => onSelectionChange({ kind: "evidence", identity })}
        />
      </div>
    );
  }

  if (selection.kind === "artifact") {
    return <ArtifactTraceInspector
      artifacts={data.artifacts.items}
      protocols={data.protocols.items}
      selectedArtifactId={selection.identity}
      onSelectArtifact={(identity) => onSelectionChange({ kind: "artifact", identity })}
    />;
  }

  return <OverviewWorkspace data={data} />;
}
