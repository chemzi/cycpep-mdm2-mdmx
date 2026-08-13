"use client";

import { useCallback, useEffect, useReducer, useRef, useState } from "react";

import { fetchWorkbench, type FetchWorkbenchOptions } from "./client";
import {
  initialWorkbenchRequestState,
  beginWorkbenchRequest,
  beginWorkbenchScopeChange,
  workbenchRequestReducer,
  type RefreshSource,
  type WorkbenchRequestState,
} from "./request-lifecycle";

export interface UseWorkbenchOptions extends Omit<FetchWorkbenchOptions, "signal"> {
  autoRefreshIntervalMs?: number;
  initialAutoRefresh?: boolean;
  launcherRunId?: string;
}

export interface UseWorkbenchResult extends WorkbenchRequestState {
  autoRefreshEnabled: boolean;
  refresh: () => Promise<void>;
  setAutoRefreshEnabled: (enabled: boolean) => void;
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : "Workbench request failed";
}

export function useWorkbench(options: UseWorkbenchOptions = {}): UseWorkbenchResult {
  const {
    autoRefreshIntervalMs = 10_000,
    initialAutoRefresh = true,
    apiOrigin,
    fetchImpl,
    launcherRunId,
  } = options;
  const [state, dispatch] = useReducer(
    workbenchRequestReducer,
    undefined,
    initialWorkbenchRequestState,
  );
  const [autoRefreshEnabled, setAutoRefreshEnabled] = useState(initialAutoRefresh);
  const activeRequest = useRef<AbortController | null>(null);
  const activeLauncherRunId = useRef(launcherRunId);

  const refreshFrom = useCallback(async (source: RefreshSource) => {
    const controller = beginWorkbenchRequest(activeRequest.current, source);
    if (!controller) return;
    activeRequest.current = controller;
    dispatch({ type: "started" });
    try {
      const data = await fetchWorkbench({
        apiOrigin,
        fetchImpl,
        launcherRunId,
        signal: controller.signal,
      });
      if (!controller.signal.aborted) dispatch({ type: "succeeded", data });
    } catch (cause) {
      if (!controller.signal.aborted) {
        dispatch({ type: "failed", error: errorMessage(cause) });
      }
    } finally {
      if (activeRequest.current === controller) activeRequest.current = null;
    }
  }, [apiOrigin, fetchImpl, launcherRunId]);

  const refresh = useCallback(
    () => refreshFrom("manual"),
    [refreshFrom],
  );

  useEffect(() => {
    const previousLauncherRunId = activeLauncherRunId.current;
    const transition = beginWorkbenchScopeChange(
      activeRequest.current,
      previousLauncherRunId,
      launcherRunId,
    );
    if (transition.changed) {
      activeRequest.current = null;
      activeLauncherRunId.current = launcherRunId;
    }
    void refresh();
    return () => activeRequest.current?.abort();
  }, [launcherRunId, refresh]);

  useEffect(() => {
    if (!autoRefreshEnabled || autoRefreshIntervalMs <= 0) return;
    const timer = window.setInterval(
      () => void refreshFrom("automatic"),
      autoRefreshIntervalMs,
    );
    return () => window.clearInterval(timer);
  }, [autoRefreshEnabled, autoRefreshIntervalMs, refreshFrom]);

  return {
    ...state,
    autoRefreshEnabled,
    refresh,
    setAutoRefreshEnabled,
  };
}
