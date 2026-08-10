import type {
  ArtifactView,
  BoundedCollection,
  BlockerView,
  CandidateView,
  EvidenceView,
  TaskView,
  TraceLink,
  TransactionView,
  WorkbenchReadModel,
} from "../domain";
import type { WorkbenchSelection } from "../selection";
import { candidateArtifacts, candidateEvidence } from "../scientific-selectors";
import { ProtocolDetail, TraceDetail } from "./artifact-trace";
import { selectTaskLifecycle } from "./execution-transaction";

export interface WorkbenchInspectorProps {
  data: WorkbenchReadModel;
  selection: WorkbenchSelection;
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
  onSelectionChange: (selection: WorkbenchSelection) => void;
}

interface ReturnedCollectionCoverage {
  label: string;
  collection: Pick<BoundedCollection<unknown>, "total" | "returned" | "truncated">;
}

function PartialReturnedContext({ collections }: { collections: ReturnedCollectionCoverage[] }) {
  const omitted = collections.filter(({ collection }) => collection.truncated);
  if (omitted.length === 0) return null;
  return (
    <section className="partial-returned-context" aria-labelledby="partial-returned-context-title">
      <h3 id="partial-returned-context-title">Partial returned context</h3>
      <ul>
        {omitted.map(({ label, collection }) => (
          <li key={label}>{label} {collection.returned} / {collection.total} returned · omitted</li>
        ))}
      </ul>
    </section>
  );
}

function supportingCollections(
  data: WorkbenchReadModel,
  selection: WorkbenchSelection,
): ReturnedCollectionCoverage[] {
  if (selection.kind === "task") return [
    { label: "Executions", collection: data.executions },
    { label: "Transactions", collection: data.transactions },
    { label: "Blockers", collection: data.blockers },
  ];
  if (selection.kind === "candidate") return [
    { label: "Evidence", collection: data.evidence },
    { label: "Artifacts", collection: data.artifacts },
  ];
  if (selection.kind === "evidence") return [
    { label: "Evidence", collection: data.evidence },
    { label: "Protocols", collection: data.protocols },
  ];
  if (selection.kind === "artifact") return [
    { label: "Artifacts", collection: data.artifacts },
    { label: "Protocols", collection: data.protocols },
  ];
  return [];
}

function BlockerDetail({ blockers }: { blockers: BlockerView[] }) {
  if (blockers.length === 0) return <p>No blockers returned for this context.</p>;
  return (
    <section aria-labelledby="inspector-blockers-title">
      <h3 id="inspector-blockers-title">Needs attention</h3>
      <ul className="inspector-blockers">
        {blockers.map((blocker, index) => (
          <li key={`${blocker.code}-${blocker.task_id ?? blocker.transaction_id ?? index}`}>
            <code>{blocker.code}</code>
            <span>{blocker.scope}</span>
            <p>{blocker.summary}</p>
            <dl className="blocker-identities" aria-label={`${blocker.code} formal identifiers`}>
              {blocker.workflow_id ? <div><dt>workflow_id</dt><dd><code>{blocker.workflow_id}</code></dd></div> : null}
              {blocker.run_id ? <div><dt>run_id</dt><dd><code>{blocker.run_id}</code></dd></div> : null}
              {blocker.task_id ? <div><dt>task_id</dt><dd><code>{blocker.task_id}</code></dd></div> : null}
              {blocker.transaction_id ? <div><dt>transaction_id</dt><dd><code>{blocker.transaction_id}</code></dd></div> : null}
            </dl>
          </li>
        ))}
      </ul>
    </section>
  );
}

function TaskInspector({ task, transactions }: { task: TaskView; transactions: TransactionView[] }) {
  const identity = task.task_id ?? "Unavailable";
  return (
    <>
      <IdentityDetail title="task" identity={identity} protocol={task.protocol} />
      <section aria-labelledby="task-metadata-title">
        <h3 id="task-metadata-title">Task metadata</h3>
        <dl className="domain-fields">
          <div><dt>Agent</dt><dd>{task.agent ?? "Unavailable"}</dd></div>
          <div><dt>Kind</dt><dd>{task.kind ?? "Unavailable"}</dd></div>
          <div><dt>Disposition</dt><dd>{task.disposition ?? "Unavailable"}</dd></div>
          <div><dt>Status</dt><dd>{task.status ?? "Unavailable"}</dd></div>
          <div><dt>Action</dt><dd><code>{task.action.name}</code></dd></div>
          <div><dt>Approval</dt><dd>{task.approval.state}</dd></div>
        </dl>
      </section>
      <section aria-labelledby="task-transactions-title">
        <h3 id="task-transactions-title">Returned transactions</h3>
        {transactions.length === 0 ? <p>No transactions returned for this task.</p> : transactions.map((transaction, index) => (
          <article className="inspector-transaction" key={transaction.transaction_id ?? `${identity}-${index}`}>
            <strong><code>{transaction.transaction_id ?? "Opaque transaction identity unavailable"}</code></strong>
            <span>{transaction.status ?? "Status unavailable"}</span>
            <TraceDetail trace={transaction} />
          </article>
        ))}
      </section>
    </>
  );
}

function IdentityDetail({
  title,
  identity,
  trace,
  protocol,
}: {
  title: string;
  identity: string;
  trace?: TraceLink;
  protocol?: TaskView["protocol"];
}) {
  return (
    <>
      <header><span>Selected {title}</span><h2>{identity}</h2></header>
      {protocol ? <><h3>Protocol</h3><ProtocolDetail protocol={protocol} /></> : null}
      {trace ? <><h3>Trace linkage</h3><TraceDetail trace={trace} /></> : null}
    </>
  );
}

function CandidateInspector({
  candidate,
  evidence,
  artifacts,
  onSelectionChange,
}: {
  candidate: CandidateView;
  evidence: EvidenceView[];
  artifacts: ArtifactView[];
  onSelectionChange: (selection: WorkbenchSelection) => void;
}) {
  const candidateId = candidate.candidate_id ?? "Unavailable";
  const relatedEvidence = candidate.candidate_id ? candidateEvidence(candidate.candidate_id, evidence) : [];
  const relatedArtifacts = candidate.candidate_id ? candidateArtifacts(candidate.candidate_id, artifacts) : [];
  return (
    <>
      <IdentityDetail title="candidate" identity={candidateId} trace={candidate.trace} protocol={candidate.protocol} />
      <section aria-labelledby="candidate-provenance-title">
        <h3 id="candidate-provenance-title">Formal provenance</h3>
        <ul>
          {relatedEvidence.map((item) => <li key={item.event_id}>
            <button type="button" onClick={() => item.event_id && onSelectionChange({ kind: "evidence", identity: item.event_id })}>
              Evidence: {item.event_id ?? "Unavailable"}
            </button>
          </li>)}
          {relatedArtifacts.map((item) => <li key={item.artifact_id}>
            <button type="button" onClick={() => item.artifact_id && onSelectionChange({ kind: "artifact", identity: item.artifact_id })}>
              Artifact: {item.artifact_id ?? "Unavailable"}
            </button>
            <span>{item.content_link ? ` · ${item.content_link}` : " · Content unavailable"}</span>
          </li>)}
        </ul>
      </section>
    </>
  );
}

export function WorkbenchInspector({
  data,
  selection,
  collapsed,
  onCollapsedChange,
  onSelectionChange,
}: WorkbenchInspectorProps) {
  let content;
  let blockers = data.blockers.items;

  if (selection.kind === "task") {
    const task = data.tasks.items.find((item) => item.task_id === selection.identity);
    const lifecycle = selectTaskLifecycle(selection.identity, data.transactions.items, data.blockers.items);
    blockers = lifecycle.blockers;
    content = task ? <TaskInspector task={task} transactions={lifecycle.transactions} /> : null;
  } else if (selection.kind === "candidate") {
    const candidate = data.candidates.items.find((item) => item.candidate_id === selection.identity);
    content = candidate ? <CandidateInspector
      candidate={candidate}
      evidence={data.evidence.items}
      artifacts={data.artifacts.items}
      onSelectionChange={onSelectionChange}
    /> : null;
  } else if (selection.kind === "evidence") {
    const evidence = data.evidence.items.find((item) => item.event_id === selection.identity);
    content = evidence ? <IdentityDetail title="Evidence" identity={selection.identity} trace={evidence.trace} protocol={evidence.protocol} /> : null;
  } else if (selection.kind === "artifact") {
    const artifact = data.artifacts.items.find((item) => item.artifact_id === selection.identity);
    content = artifact ? <>
      <IdentityDetail title="artifact" identity={selection.identity} trace={artifact.trace} protocol={artifact.protocol} />
      <p>{artifact.content_link ? `Content link: ${artifact.content_link}` : "Content unavailable: no formal content_link returned"}</p>
    </> : null;
  } else {
    content = <IdentityDetail title="project" identity={data.project.project_id} trace={data.trace} />;
  }

  return (
    <aside className={`workbench-inspector${collapsed ? " is-collapsed" : ""}`} aria-label="Workbench inspector">
      {collapsed ? (
        <button type="button" aria-label="Restore inspector" aria-expanded="false" onClick={() => onCollapsedChange(false)}>
          Inspector · {selection.identity ?? "Overview"} · {blockers.length > 0 ? "Needs attention" : "No blocker"}
        </button>
      ) : (
        <>
          <button type="button" aria-label="Collapse inspector" aria-expanded="true" onClick={() => onCollapsedChange(true)}>Collapse inspector</button>
          {content}
          <PartialReturnedContext collections={supportingCollections(data, selection)} />
          <BlockerDetail blockers={blockers} />
        </>
      )}
    </aside>
  );
}
