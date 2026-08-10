import type { WorkbenchEnvelope } from "./domain";

export type WorkbenchRequestStatus =
  | "initial-loading"
  | "ready"
  | "refreshing"
  | "stale-after-error"
  | "failed-before-data";

export interface WorkbenchRequestState {
  status: WorkbenchRequestStatus;
  data: WorkbenchEnvelope | null;
  error: string | null;
}

export type WorkbenchRequestAction =
  | { type: "started" }
  | { type: "succeeded"; data: WorkbenchEnvelope }
  | { type: "failed"; error: string };

export type RefreshSource = "manual" | "automatic";

export function beginWorkbenchRequest(
  active: AbortController | null,
  source: RefreshSource,
): AbortController | null {
  if (source === "automatic" && active) return null;
  active?.abort();
  return new AbortController();
}

export function initialWorkbenchRequestState(): WorkbenchRequestState {
  return { status: "initial-loading", data: null, error: null };
}

export function workbenchRequestReducer(
  state: WorkbenchRequestState,
  action: WorkbenchRequestAction,
): WorkbenchRequestState {
  switch (action.type) {
    case "started":
      return state.data
        ? { status: "refreshing", data: state.data, error: null }
        : initialWorkbenchRequestState();
    case "succeeded":
      return { status: "ready", data: action.data, error: null };
    case "failed":
      return state.data
        ? { status: "stale-after-error", data: state.data, error: action.error }
        : { status: "failed-before-data", data: null, error: action.error };
  }
}
