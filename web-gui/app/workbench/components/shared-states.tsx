import type { BoundedCollection, BlockerView } from "../domain";

export function LoadingState({ label = "Loading workbench" }: { label?: string }) {
  return <p className="workbench-state loading" role="status">{label}</p>;
}

export function EmptyState({ title, detail }: { title: string; detail?: string }) {
  return <section className="workbench-state empty" aria-label={title}>
    <strong>{title}</strong>
    {detail ? <p>{detail}</p> : null}
  </section>;
}

export function FailureState({ message }: { message: string }) {
  return <section className="workbench-state failure" role="alert">
    <strong>Workbench unavailable</strong>
    <p>{message}</p>
  </section>;
}

export function StaleState({ message }: { message: string }) {
  return <p className="workbench-state stale" role="status">
    Showing the last successful response. Refresh failed: {message}
  </p>;
}

export function BlockerList({
  blockers,
  headingId,
  title = "Structured blockers",
  compact = false,
}: {
  blockers: BlockerView[];
  headingId: string;
  title?: string;
  compact?: boolean;
}) {
  if (blockers.length === 0) return null;
  return <section
    className={`workbench-blockers${compact ? " is-compact" : ""}`}
    aria-labelledby={headingId}
  >
    <h2 id={headingId}>{title}</h2>
    <ul>
      {blockers.map((blocker, index) => {
        const identity = blocker.transaction_id ?? blocker.task_id ?? blocker.run_id ?? blocker.workflow_id;
        return <li
          data-blocker-code={blocker.code}
          data-blocker-scope={blocker.scope}
          key={`${blocker.code}-${identity ?? index}`}
        >
          <code>{blocker.code}</code>
          <span>{blocker.scope}{identity ? ` · ${identity}` : ""}</span>
          <p>{blocker.summary}</p>
        </li>;
      })}
    </ul>
  </section>;
}

export function CollectionSummary({
  collection,
  label,
}: {
  collection: Pick<BoundedCollection<unknown>, "total" | "returned" | "truncated">;
  label: string;
}) {
  return <p className="collection-summary" aria-label={`${label} collection coverage`}>
    {collection.returned} returned / {collection.total} total
    {collection.truncated ? <strong> · truncated</strong> : null}
  </p>;
}
