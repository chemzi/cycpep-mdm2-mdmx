import type {
  BlockerView,
  ExecutionView,
  TaskView,
  TransactionView,
} from "../domain";
import { BlockerList, EmptyState } from "./shared-states";

export interface ExecutionTransactionDetailProps {
  task: TaskView;
  executions: ExecutionView[];
  transactions: TransactionView[];
  blockers: BlockerView[];
}

export interface CorrelatedAttempt {
  execution: ExecutionView;
  transactions: TransactionView[];
}

export function correlateTaskAttempts(
  taskId: string,
  executions: ExecutionView[],
  transactions: TransactionView[],
): CorrelatedAttempt[] {
  return executions
    .filter((execution) => execution.task_id === taskId)
    .map((execution) => ({
      execution,
      transactions: transactions.filter((transaction) =>
        transaction.task_id === taskId &&
        transaction.attempt_id === execution.attempt_id
      ),
    }));
}

function transactionBlockers(
  taskId: string,
  transactions: TransactionView[],
  blockers: BlockerView[],
): BlockerView[] {
  const transactionIds = new Set(
    transactions
      .map((transaction) => transaction.transaction_id)
      .filter((identity): identity is string => Boolean(identity)),
  );
  return blockers.filter((blocker) =>
    blocker.task_id === taskId ||
    (Boolean(blocker.transaction_id) && transactionIds.has(blocker.transaction_id as string))
  );
}

function StructuredFailure({
  error,
}: {
  error: NonNullable<ExecutionView["error"]>;
}) {
  return <section className="structured-failure" role="alert" aria-label="Structured execution failure">
    <h4>Failure</h4>
    <dl>
      <div><dt>Code</dt><dd>{error.code ?? "Unavailable"}</dd></div>
      <div><dt>Message</dt><dd>{error.message ?? "Unavailable"}</dd></div>
      <div><dt>Component</dt><dd>{error.component ?? "Unavailable"}</dd></div>
      <div><dt>Retryable</dt><dd>{error.retryable === undefined ? "Unavailable" : String(error.retryable)}</dd></div>
    </dl>
  </section>;
}

function TransactionRecord({ transaction }: { transaction: TransactionView }) {
  return <article className="transaction-record">
    <h4>Transaction {transaction.transaction_id ?? "Unavailable"}</h4>
    <dl>
      <div><dt>Status</dt><dd>{transaction.status ?? "Unavailable"}</dd></div>
      <div><dt>Attempt</dt><dd>{transaction.attempt_id ?? "Unavailable"}</dd></div>
      {transaction.created_at ? <div><dt>Created</dt><dd>{transaction.created_at}</dd></div> : null}
      {transaction.updated_at ? <div><dt>Updated</dt><dd>{transaction.updated_at}</dd></div> : null}
    </dl>
    {transaction.error ? <StructuredFailure error={transaction.error}/> : null}
  </article>;
}

export function ExecutionTransactionDetail({
  task,
  executions,
  transactions,
  blockers,
}: ExecutionTransactionDetailProps) {
  const taskId = task.task_id ?? "";
  const attempts = correlateTaskAttempts(taskId, executions, transactions);
  const relatedTransactions = attempts.flatMap((attempt) => attempt.transactions);
  const relatedBlockers = transactionBlockers(taskId, relatedTransactions, blockers);

  return <section className="execution-transaction-detail" aria-labelledby="execution-detail-title">
    <h3 id="execution-detail-title">Execution / transaction · {taskId || "Unavailable task"}</h3>
    {attempts.length === 0 ? <EmptyState
      title="No execution recorded"
      detail="The formal current-run response contains no execution for this task."
    /> : attempts.map(({ execution, transactions: matchingTransactions }, index) =>
      <article className="execution-attempt" key={`${execution.attempt_id ?? "attempt"}-${index}`}>
        <h4>Attempt {execution.attempt_id ?? String(execution.attempts)}</h4>
        <dl>
          <div><dt>Execution status</dt><dd>{execution.status ?? "Unavailable"}</dd></div>
          <div><dt>Attempt count</dt><dd>{execution.attempts}</dd></div>
          <div><dt>Worker</dt><dd>{execution.worker_id ?? "Unavailable"}</dd></div>
          <div><dt>Transaction visibility</dt><dd>{execution.transaction_visibility === "not_yet_recorded" ? "not yet recorded" : execution.transaction_visibility}</dd></div>
        </dl>
        {execution.transaction_visibility === "not_yet_recorded"
          ? <p className="transaction-not-recorded" role="status">No transaction record exists for this running attempt yet.</p>
          : null}
        {execution.error ? <StructuredFailure error={execution.error}/> : null}
        {matchingTransactions.length > 0
          ? matchingTransactions.map((transaction, transactionIndex) =>
              <TransactionRecord
                key={transaction.transaction_id ?? `${execution.attempt_id}-${transactionIndex}`}
                transaction={transaction}
              />)
          : null}
      </article>
    )}
    <BlockerList
      blockers={relatedBlockers}
      headingId="execution-recovery-blockers-title"
      title="Execution and recovery blockers"
    />
  </section>;
}
