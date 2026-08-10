import type { TransactionView, WorkbenchReadModel } from "../domain";
import type { WorkbenchSelection } from "../selection";

export interface WorkbenchHistoryProps {
  data: WorkbenchReadModel;
  selection: WorkbenchSelection;
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
}

interface TimestampedRecord {
  identity: string;
  label: string;
  timestamp: string;
  detail: string;
}

function timestampedRecords(data: WorkbenchReadModel): TimestampedRecord[] {
  const evidence = data.evidence.items.flatMap((item, index) => item.timestamp ? [{
    identity: item.event_id ?? `Evidence ${index + 1}`,
    label: item.event_type ?? "Evidence",
    timestamp: item.timestamp,
    detail: item.run_relation.replaceAll("_", " "),
  }] : []);
  const transactions = data.transactions.items.flatMap((item, index) => {
    const timestamp = item.updated_at ?? item.created_at;
    return timestamp ? [{
      identity: item.transaction_id ?? `Transaction ${index + 1}`,
      label: "Transaction",
      timestamp,
      detail: `${item.attempt_id ?? "Attempt unavailable"} · ${item.status ?? "Status unavailable"}`,
    }] : [];
  });
  return [...evidence, ...transactions].sort((left, right) => left.timestamp.localeCompare(right.timestamp));
}

function taskTransactions(data: WorkbenchReadModel, selection: WorkbenchSelection): TransactionView[] {
  if (selection.kind !== "task") return data.transactions.items;
  return data.transactions.items.filter((transaction) => transaction.task_id === selection.identity);
}

export function WorkbenchHistory({ data, selection, collapsed, onCollapsedChange }: WorkbenchHistoryProps) {
  const timestamped = timestampedRecords(data);
  const executions = selection.kind === "task"
    ? data.executions.items.filter((execution) => execution.task_id === selection.identity)
    : data.executions.items;
  const untimedTransactions = taskTransactions(data, selection).filter(
    (transaction) => !transaction.created_at && !transaction.updated_at,
  );

  return (
    <section className={`workbench-history${collapsed ? " is-collapsed" : ""}`} aria-label="Workbench history">
      {collapsed ? (
        <button type="button" aria-label="Restore history" aria-expanded="false" onClick={() => onCollapsedChange(false)}>
          History · {selection.identity ?? "Overview"}
        </button>
      ) : (
        <>
          <header>
            <div><span>Formal returned records</span><h2>Evidence and lifecycle history</h2></div>
            <button type="button" aria-label="Collapse history" aria-expanded="true" onClick={() => onCollapsedChange(true)}>Collapse history</button>
          </header>
          <div className="history-lanes">
            <ol className="timestamped-history" aria-label="Timestamped records">
              {timestamped.map((record) => <li key={`${record.label}-${record.identity}-${record.timestamp}`}>
                <time dateTime={record.timestamp}>{record.timestamp}</time>
                <strong>{record.label}</strong>
                <code>{record.identity}</code>
                <span>{record.detail}</span>
              </li>)}
            </ol>
            <section className="untimed-history" aria-labelledby="untimed-history-title">
              <h3 id="untimed-history-title">Untimed records</h3>
              {executions.map((execution, index) => <article key={`${execution.task_id}-${execution.attempt_id ?? index}`}>
                <strong>Attempt {execution.attempt_id ?? execution.attempts}</strong>
                <span>{execution.task_id} · {execution.status ?? "Status unavailable"}</span>
                <span>{execution.transaction_visibility === "not_yet_recorded" ? "not yet recorded" : execution.transaction_visibility}</span>
              </article>)}
              {untimedTransactions.map((transaction, index) => <article key={transaction.transaction_id ?? `${transaction.attempt_id}-${index}`}>
                <strong>Transaction {transaction.transaction_id ?? "Unavailable"}</strong>
                <span>{transaction.attempt_id ?? "Attempt unavailable"} · {transaction.status ?? "Status unavailable"}</span>
              </article>)}
              {executions.length === 0 && untimedTransactions.length === 0 ? <p>No untimed attempt records returned.</p> : null}
            </section>
          </div>
        </>
      )}
    </section>
  );
}
