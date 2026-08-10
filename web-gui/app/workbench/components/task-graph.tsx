"use client";

import { useMemo } from "react";

import type {
  BoundedCollection,
  BlockerView,
  ExecutionView,
  TaskView,
  TransactionView,
} from "../domain";
import { CollectionSummary, EmptyState } from "./shared-states";
import { ExecutionTransactionDetail } from "./execution-transaction";
import { useBoundedSelection } from "../selection";

export interface TaskGraphProps {
  tasks: BoundedCollection<TaskView>;
  executions: BoundedCollection<ExecutionView>;
  transactions: BoundedCollection<TransactionView>;
  blockers: BlockerView[];
}

function TaskCard({
  task,
  blockers,
  selected,
  onSelect,
}: {
  task: TaskView;
  blockers: BlockerView[];
  selected: boolean;
  onSelect: () => void;
}) {
  const identity = task.task_id ?? "Unavailable task identity";
  return <article className={`task-card${selected ? " selected" : ""}`}>
    <button type="button" aria-pressed={selected} onClick={onSelect}>
      Inspect {identity}
    </button>
    <h3>{identity}</h3>
    <p>{task.kind ?? "Unspecified kind"} · {task.disposition ?? "Unspecified disposition"}</p>
    <dl>
      <div><dt>Status</dt><dd>{task.status ?? "Unavailable"}</dd></div>
      <div><dt>Action</dt><dd><code>{task.action.name}</code></dd></div>
      <div><dt>Executable</dt><dd>{String(task.action.executable)}</dd></div>
      <div><dt>Handler available</dt><dd>{String(task.action.handler_available)}</dd></div>
      <div><dt>Resource class</dt><dd>{task.action.resource_class ?? "Unavailable"}</dd></div>
      <div><dt>Approval required</dt><dd>{String(task.approval.required)}</dd></div>
      <div><dt>Approval state</dt><dd>{task.approval.state}</dd></div>
      <div><dt>Execution gate</dt><dd>{task.execution_gate.status ?? "Unavailable"}</dd></div>
    </dl>
    <p className="task-dependencies">
      <strong>Depends on:</strong> {task.depends_on.length > 0 ? task.depends_on.join(", ") : "None"}
    </p>
    <p className="task-outputs">
      <strong>Output roles:</strong> {task.action.output_roles.length > 0 ? task.action.output_roles.join(", ") : "None"}
    </p>
    {!task.availability.available || task.availability.reason_codes.length > 0 ? <section aria-label={`${identity} availability`}>
      <strong>Action unavailable</strong>
      <ul>{task.availability.reason_codes.map((reason) => <li key={reason}><code>{reason}</code></li>)}</ul>
    </section> : <p>Action available</p>}
    {blockers.length > 0 ? <section aria-label={`${identity} blockers`}>
      <strong>Task blockers</strong>
      <ul>{blockers.map((blocker, index) => <li key={`${blocker.code}-${index}`}>
        <code>{blocker.code}</code> · {blocker.summary}
      </li>)}</ul>
    </section> : null}
  </article>;
}

export function TaskGraph({ tasks, executions, transactions, blockers }: TaskGraphProps) {
  const taskIds = useMemo(
    () => tasks.items.map((task) => task.task_id).filter((identity): identity is string => Boolean(identity)),
    [tasks.items],
  );
  const [selectedTaskId, setSelectedTaskId] = useBoundedSelection(taskIds);

  if (tasks.items.length === 0) {
    return <section className="task-graph" aria-labelledby="task-graph-title">
      <h2 id="task-graph-title">Task / Action graph</h2>
      <CollectionSummary collection={tasks} label="Tasks"/>
      <EmptyState
        title="No trustworthy current-run task graph"
        detail="No task graph is available for the current workflow/run binding."
      />
    </section>;
  }

  const selectedTask = tasks.items.find((task) => task.task_id === selectedTaskId) ?? tasks.items[0];

  return <section className="task-graph" aria-labelledby="task-graph-title">
    <h2 id="task-graph-title">Task / Action graph</h2>
    <CollectionSummary collection={tasks} label="Tasks"/>
    <p className="graph-note">Dependencies are shown as returned; tasks are not mapped to a fixed Agent pipeline.</p>
    <div className="task-graph-items">
      {tasks.items.map((task, index) => <TaskCard
        key={task.task_id ?? `task-${index}`}
        task={task}
        blockers={blockers.filter((blocker) => blocker.task_id === task.task_id)}
        selected={task === selectedTask}
        onSelect={() => setSelectedTaskId(task.task_id ?? "")}
      />)}
    </div>
    <ExecutionTransactionDetail
      task={selectedTask}
      executions={executions.items}
      transactions={transactions.items}
      blockers={blockers}
    />
  </section>;
}
