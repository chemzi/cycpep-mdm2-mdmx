import type { BoundedCollection, WorkbenchReadModel } from "../domain";
import type { WorkbenchSelection } from "../selection";

export interface WorkbenchNavigatorProps {
  data: WorkbenchReadModel;
  selection: WorkbenchSelection;
  onSelectionChange: (selection: WorkbenchSelection) => void;
}

function collectionLabel(
  label: string,
  collection: Pick<BoundedCollection<unknown>, "returned" | "total" | "truncated">,
): string {
  if (collection.truncated) {
    return `${label} ${collection.returned} / ${collection.total} · omitted`;
  }
  return `${label} ${collection.returned}`;
}

export function WorkbenchNavigator({
  data,
  selection,
  onSelectionChange,
}: WorkbenchNavigatorProps) {
  const selectedKind = selection.kind === "overview" ? "task" : selection.kind;
  const tabs = [
    { kind: "task" as const, label: collectionLabel("Tasks", data.tasks), first: data.tasks.items[0]?.task_id },
    { kind: "candidate" as const, label: collectionLabel("Candidates", data.candidates), first: data.candidates.items[0]?.candidate_id },
    { kind: "evidence" as const, label: collectionLabel("Evidence", data.evidence), first: data.evidence.items[0]?.event_id },
  ];

  return (
    <nav className="workbench-navigator" aria-label="Workbench navigator">
      <div role="group" aria-label="Workbench collections">
        {tabs.map((tab) => (
          <button
            key={tab.kind}
            type="button"
            aria-pressed={selectedKind === tab.kind}
            disabled={!tab.first}
            onClick={() => tab.first && onSelectionChange({ kind: tab.kind, identity: tab.first })}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {selectedKind === "task" ? (
        <ul className="navigator-subjects" aria-label="Returned tasks">
          {data.tasks.items.map((task, index) => {
            const identity = task.task_id;
            if (!identity) return null;
            const selected = selection.kind === "task" && selection.identity === identity;
            const blocked = !task.availability.available || data.blockers.items.some((item) => item.task_id === identity);
            return <li key={identity ?? `task-${index}`}>
              <button type="button" aria-pressed={selected} onClick={() => onSelectionChange({ kind: "task", identity })}>
                <strong>{identity}</strong>
                <span>{task.action.name}</span>
                <small>{blocked ? "Needs attention" : task.status ?? "Status unavailable"}</small>
              </button>
            </li>;
          })}
        </ul>
      ) : null}

      {selectedKind === "candidate" ? (
        <ul className="navigator-subjects" aria-label="Returned candidates">
          {data.candidates.items.map((candidate, index) => {
            const identity = candidate.candidate_id;
            if (!identity) return null;
            const selected = selection.kind === "candidate" && selection.identity === identity;
            return <li key={identity ?? `candidate-${index}`}>
              <button type="button" aria-pressed={selected} onClick={() => onSelectionChange({ kind: "candidate", identity })}>
                <strong>{identity}</strong>
                <span>{candidate.sequence ?? "Sequence unavailable"}</span>
                <small>{candidate.run_relation.replaceAll("_", " ")}</small>
              </button>
            </li>;
          })}
        </ul>
      ) : null}

      {selectedKind === "evidence" ? (
        <ul className="navigator-subjects" aria-label="Returned Evidence">
          {data.evidence.items.map((evidence, index) => {
            const identity = evidence.event_id;
            if (!identity) return null;
            const selected = selection.kind === "evidence" && selection.identity === identity;
            return <li key={identity ?? `evidence-${index}`}>
              <button type="button" aria-pressed={selected} onClick={() => onSelectionChange({ kind: "evidence", identity })}>
                <strong>{evidence.event_type ?? "Evidence"}</strong>
                <span>{identity}</span>
                <small>{evidence.timestamp ?? "Time unavailable"}</small>
              </button>
            </li>;
          })}
        </ul>
      ) : null}
    </nav>
  );
}
