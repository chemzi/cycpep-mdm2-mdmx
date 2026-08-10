"use client";

import { useState } from "react";

import type { WorkbenchReadModel } from "./domain";

export type WorkbenchSelection =
  | { kind: "overview"; identity: null }
  | { kind: "task" | "candidate" | "evidence" | "artifact"; identity: string };

const OVERVIEW_SELECTION: WorkbenchSelection = {
  kind: "overview",
  identity: null,
};

function returnedIdentities(
  kind: Exclude<WorkbenchSelection["kind"], "overview">,
  data: WorkbenchReadModel,
): string[] {
  switch (kind) {
    case "task":
      return data.tasks.items
        .map((task) => task.task_id)
        .filter((identity): identity is string => Boolean(identity));
    case "candidate":
      return data.candidates.items
        .map((candidate) => candidate.candidate_id)
        .filter((identity): identity is string => Boolean(identity));
    case "evidence":
      return data.evidence.items
        .map((evidence) => evidence.event_id)
        .filter((identity): identity is string => Boolean(identity));
    case "artifact":
      return data.artifacts.items
        .map((artifact) => artifact.artifact_id)
        .filter((identity): identity is string => Boolean(identity));
  }
}

/**
 * Keeps only an opaque returned identity in UI state. Domain detail is always
 * resolved from the latest read model by the component that renders it.
 */
export function reconcileWorkbenchSelection(
  preferred: WorkbenchSelection,
  data: WorkbenchReadModel,
): WorkbenchSelection {
  if (preferred.kind === "overview") return OVERVIEW_SELECTION;

  const identities = returnedIdentities(preferred.kind, data);
  if (identities.includes(preferred.identity)) return preferred;
  if (identities[0]) return { kind: preferred.kind, identity: identities[0] };
  return OVERVIEW_SELECTION;
}

export function useWorkbenchSelection(
  data: WorkbenchReadModel,
  initialSelection: WorkbenchSelection = OVERVIEW_SELECTION,
): [WorkbenchSelection, (selection: WorkbenchSelection) => void] {
  const revision = JSON.stringify({
    tasks: returnedIdentities("task", data),
    candidates: returnedIdentities("candidate", data),
    evidence: returnedIdentities("evidence", data),
    artifacts: returnedIdentities("artifact", data),
  });
  const [stored, setStored] = useState(() => ({
    revision,
    selection: reconcileWorkbenchSelection(initialSelection, data),
  }));

  if (stored.revision !== revision) {
    const reconciled = {
      revision,
      selection: reconcileWorkbenchSelection(stored.selection, data),
    };
    setStored(reconciled);
    return [
      reconciled.selection,
      (selection) => setStored({ revision, selection }),
    ];
  }

  return [stored.selection, (selection) => setStored({ revision, selection })];
}

export function reconcileSelection(
  preferred: string | null,
  identities: string[],
): string | null {
  if (preferred && identities.includes(preferred)) return preferred;
  return identities[0] ?? null;
}

export function useBoundedSelection(
  identities: string[],
): [string | null, (identity: string) => void] {
  const revision = JSON.stringify(identities);
  const [selection, setSelection] = useState<{
    revision: string;
    identity: string | null;
  }>(() => ({
    revision,
    identity: identities[0] ?? null,
  }));

  if (selection.revision !== revision) {
    const reconciled = {
      revision,
      identity: reconcileSelection(selection.identity, identities),
    };
    setSelection(reconciled);
    return [reconciled.identity, (identity) => setSelection({ revision, identity })];
  }

  return [selection.identity, (identity) => setSelection({ revision, identity })];
}
