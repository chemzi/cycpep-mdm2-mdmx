"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { fetchResults, type ResultsDigest } from "./results-client";

export type ResultsRequestStatus =
  | "loading"
  | "refreshing"
  | "succeeded"
  | "failed"
  | "stale-after-error";

export interface UseResultsOptions {
  apiOrigin?: string;
  fetchImpl?: typeof fetch;
  autoRefreshIntervalMs?: number;
  initialAutoRefresh?: boolean;
}

export interface UseResultsResult {
  status: ResultsRequestStatus;
  data: ResultsDigest | null;
  error: string | null;
  refresh: () => Promise<void>;
  autoRefreshEnabled: boolean;
  setAutoRefreshEnabled: (enabled: boolean) => void;
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : "Results request failed";
}

export function useResults(options: UseResultsOptions = {}): UseResultsResult {
  const {
    autoRefreshIntervalMs = 10_000,
    initialAutoRefresh = true,
    apiOrigin,
    fetchImpl,
  } = options;
  const [status, setStatus] = useState<ResultsRequestStatus>("loading");
  const [data, setData] = useState<ResultsDigest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [autoRefreshEnabled, setAutoRefreshEnabled] = useState(initialAutoRefresh);
  const activeRequest = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    setStatus((current) => (current === "succeeded" ? "refreshing" : "loading"));
    try {
      const envelope = await fetchResults({
        apiOrigin,
        fetchImpl,
        signal: controller.signal,
      });
      if (!controller.signal.aborted) {
        setData(envelope.data);
        setError(null);
        setStatus("succeeded");
      }
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(errorMessage(cause));
        setStatus((current) => (current === "succeeded" ? "stale-after-error" : "failed"));
      }
    } finally {
      if (activeRequest.current === controller) activeRequest.current = null;
    }
  }, [apiOrigin, fetchImpl]);

  useEffect(() => {
    void refresh();
    return () => activeRequest.current?.abort();
  }, [refresh]);

  useEffect(() => {
    if (!autoRefreshEnabled || autoRefreshIntervalMs <= 0) return;
    const timer = window.setInterval(() => void refresh(), autoRefreshIntervalMs);
    return () => window.clearInterval(timer);
  }, [autoRefreshEnabled, autoRefreshIntervalMs, refresh]);

  return {
    status,
    data,
    error,
    refresh,
    autoRefreshEnabled,
    setAutoRefreshEnabled,
  };
}
