"use client";

import { useState } from "react";

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
