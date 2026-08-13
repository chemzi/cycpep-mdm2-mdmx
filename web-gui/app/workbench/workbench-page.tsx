"use client";

import { useCallback, useState, useSyncExternalStore } from "react";

import type { WorkbenchReadModel } from "./domain";
import { FailureState, LoadingState } from "./components/shared-states";
import { ProjectLaunchSheet } from "./components/project-launch-sheet";
import { WorkbenchWorkspace } from "./components/workbench-workspace";
import type { WorkbenchAuxiliaryPanel } from "./components/workbench-workspace";
import { useWorkbenchSelection } from "./selection";
import type { WorkbenchSelection } from "./selection";
import { useWorkbench } from "./use-workbench";

const AUTO_REFRESH_KEY = "cycpep-workbench-v2-auto-refresh";
const LAUNCH_SHEET_DISMISSED_KEY = "cycpep-launch-sheet-dismissed";
const LAUNCH_SHEET_CHANGE_EVENT = "cycpep-launch-sheet-change";

function subscribeLaunchSheet(callback: () => void) {
  window.addEventListener(LAUNCH_SHEET_CHANGE_EVENT, callback);
  return () => window.removeEventListener(LAUNCH_SHEET_CHANGE_EVENT, callback);
}

function launchSheetSnapshot() {
  return window.sessionStorage.getItem(LAUNCH_SHEET_DISMISSED_KEY) !== "true";
}

function initialSelection(data: WorkbenchReadModel): WorkbenchSelection {
  const task = data.tasks.items.find((item) => item.task_id)?.task_id;
  if (task) return { kind: "task", identity: task };
  const candidate = data.candidates.items.find((item) => item.candidate_id)?.candidate_id;
  if (candidate) return { kind: "candidate", identity: candidate };
  const evidence = data.evidence.items.find((item) => item.event_id)?.event_id;
  if (evidence) return { kind: "evidence", identity: evidence };
  return { kind: "overview", identity: null };
}

function LoadedWorkbench({
  data,
  requestStatus,
  refreshError,
  autoRefreshEnabled,
  onRefresh,
  onAutoRefreshChange,
  onNewProject,
}: {
  data: WorkbenchReadModel;
  requestStatus: ReturnType<typeof useWorkbench>["status"];
  refreshError: string | null;
  autoRefreshEnabled: boolean;
  onRefresh: () => void;
  onAutoRefreshChange: (enabled: boolean) => void;
  onNewProject: () => void;
}) {
  const [selection, setSelection] = useWorkbenchSelection(data, initialSelection(data));
  const [collapsedPanels, setCollapsedPanels] = useState<WorkbenchAuxiliaryPanel[]>([]);

  function setPanelCollapsed(panel: WorkbenchAuxiliaryPanel, collapsed: boolean) {
    setCollapsedPanels((current) => collapsed
      ? current.includes(panel) ? current : [...current, panel]
      : current.filter((item) => item !== panel));
  }

  return <WorkbenchWorkspace
      data={data}
      requestStatus={requestStatus}
      refreshError={refreshError}
      autoRefreshEnabled={autoRefreshEnabled}
      onNewProject={onNewProject}
      onRefresh={onRefresh}
      onAutoRefreshChange={onAutoRefreshChange}
      selection={selection}
      collapsedPanels={collapsedPanels}
      onSelectionChange={setSelection}
      onPanelCollapsedChange={setPanelCollapsed}
    />;
}

export function WorkbenchPage() {
  const launchSheetOpen = useSyncExternalStore(subscribeLaunchSheet, launchSheetSnapshot, () => false);
  const [initialAutoRefresh] = useState(() => {
    if (typeof window === "undefined") return true;
    const stored = window.localStorage.getItem(AUTO_REFRESH_KEY);
    return stored === null ? true : stored === "true";
  });
  const workbench = useWorkbench({
    autoRefreshIntervalMs: 10_000,
    initialAutoRefresh,
  });
  const model = workbench.data?.data ?? null;

  const setLaunchSheet = useCallback((open: boolean) => {
    if (open) window.sessionStorage.removeItem(LAUNCH_SHEET_DISMISSED_KEY);
    else window.sessionStorage.setItem(LAUNCH_SHEET_DISMISSED_KEY, "true");
    window.dispatchEvent(new Event(LAUNCH_SHEET_CHANGE_EVENT));
  }, []);
  const openLaunchSheet = useCallback(() => setLaunchSheet(true), [setLaunchSheet]);
  const closeLaunchSheet = useCallback(() => setLaunchSheet(false), [setLaunchSheet]);

  function setAutoRefresh(enabled: boolean) {
    window.localStorage.setItem(AUTO_REFRESH_KEY, String(enabled));
    workbench.setAutoRefreshEnabled(enabled);
  }

  const content = !model
    ? <main className="initial-state">
        {workbench.status === "failed-before-data"
          ? <FailureState message={workbench.error ?? "Workbench request failed"} />
          : <LoadingState label="Loading Frontend V2 workbench" />}
      </main>
    : <LoadedWorkbench
        data={model}
        requestStatus={workbench.status}
        refreshError={workbench.error}
        autoRefreshEnabled={workbench.autoRefreshEnabled}
        onNewProject={openLaunchSheet}
        onRefresh={() => void workbench.refresh()}
        onAutoRefreshChange={setAutoRefresh}
      />;

  return <>
    {content}
    {launchSheetOpen ? <ProjectLaunchSheet onClose={closeLaunchSheet} /> : null}
  </>;
}
