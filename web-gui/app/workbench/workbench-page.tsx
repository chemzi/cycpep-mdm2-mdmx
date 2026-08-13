"use client";

import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";

import type { WorkbenchReadModel } from "./domain";
import { FailureState, LoadingState } from "./components/shared-states";
import { ProjectLaunchSheet, type ProjectLaunchSubmission, type ProjectReviewProjection } from "./components/project-launch-sheet";
import { WorkbenchWorkspace } from "./components/workbench-workspace";
import type { ApprovalControlProjection, ManualApprovalRequest, ProjectDraftProjection, ProjectLaunchRequest } from "./control-domain";
import type { WorkbenchAuxiliaryPanel } from "./components/workbench-workspace";
import { useWorkbenchSelection } from "./selection";
import type { WorkbenchSelection } from "./selection";
import { useWorkbench } from "./use-workbench";
import { useProjectLaunchControl } from "./use-project-launch-control";

const AUTO_REFRESH_KEY = "cycpep-workbench-v2-auto-refresh";
const LAUNCH_SHEET_DISMISSED_KEY = "cycpep-launch-sheet-dismissed";
const LAUNCH_SHEET_CHANGE_EVENT = "cycpep-launch-sheet-change";
const ACTIVE_LAUNCHER_KEY = "cycpep-active-launcher-run-v1";

const INITIAL_LAUNCH_REQUEST: ProjectLaunchRequest = {
  target_identifier: "",
  options: {
    identifier_type: "auto",
    organism_id: 9606,
    epitope: null,
    objective: "binder",
    launcher_run_id: null,
    first_gate_auto_policy: null,
  },
};

function stringValue(value: unknown, fallback: string) {
  return typeof value === "string" && value ? value : fallback;
}

function stringList(value: unknown) {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function sheetReview(
  draft: ProjectDraftProjection | null,
  targetIdentifier: string,
): ProjectReviewProjection | null {
  if (!draft) return null;
  const target = draft.targets[0] ?? {};
  const structure = typeof target.structure === "object" && target.structure !== null
    ? target.structure as Record<string, unknown>
    : {};
  const blockers = stringList(draft.review.blocking_issues);
  const status = draft.review.status === "approved"
    ? "approved"
    : blockers.length === 0 ? "ready" : "review_required";
  return {
    draft_id: draft.draft_id,
    project_id: draft.project_id,
    name: draft.name,
    target_identifier: targetIdentifier.trim(),
    resolved_identity: stringValue(
      target.uniprot ?? target.gene_name ?? target.id,
      "Resolved target",
    ),
    structure_status: stringValue(structure.readiness ?? structure.status, "Unavailable"),
    review_status: status,
    blockers,
    uncertainties: stringList(target.uncertainties),
  };
}

function approvalRequest(
  approval: NonNullable<ReturnType<typeof useProjectLaunchControl>["lastControl"]>["approval_control"],
): ManualApprovalRequest | null {
  if (!approval) return null;
  return {
    launcher_run_id: approval.launcher_run_id,
    project_id: approval.project_id,
    approved_content_binding: approval.approved_content_binding,
    plan_id: approval.plan_id,
    plan_sha256: approval.plan_sha256,
    required_task_ids: approval.required_task_ids,
    approver: "",
    justification: "",
    ceilings: {
      max_gpu_job_slots: null,
      max_gpu_minutes: null,
      max_design_proposals: null,
      max_prediction_candidates: null,
    },
  };
}

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
  approvalControl,
  manualApprovalRequest,
  approvalPending,
  approvalError,
  onManualApprovalRequestChange,
  onApproveAndContinue,
}: {
  data: WorkbenchReadModel;
  requestStatus: ReturnType<typeof useWorkbench>["status"];
  refreshError: string | null;
  autoRefreshEnabled: boolean;
  onRefresh: () => void;
  onAutoRefreshChange: (enabled: boolean) => void;
  onNewProject: () => void;
  approvalControl: ApprovalControlProjection | null;
  manualApprovalRequest: ManualApprovalRequest | null;
  approvalPending: boolean;
  approvalError: string | null;
  onManualApprovalRequestChange: (request: ManualApprovalRequest) => void;
  onApproveAndContinue: (request: ManualApprovalRequest) => void;
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
      approvalControl={approvalControl}
      manualApprovalRequest={manualApprovalRequest}
      approvalPending={approvalPending}
      approvalError={approvalError}
      onManualApprovalRequestChange={onManualApprovalRequestChange}
      onApproveAndContinue={onApproveAndContinue}
    />;
}

export function WorkbenchPage() {
  const [activeLauncherRunId, setActiveLauncherRunId] = useState<string | undefined>(() => {
    if (typeof window === "undefined") return undefined;
    return window.sessionStorage.getItem(ACTIVE_LAUNCHER_KEY) ?? undefined;
  });
  const launchSheetOpen = useSyncExternalStore(subscribeLaunchSheet, launchSheetSnapshot, () => false);
  const [initialAutoRefresh] = useState(() => {
    if (typeof window === "undefined") return true;
    const stored = window.localStorage.getItem(AUTO_REFRESH_KEY);
    return stored === null ? true : stored === "true";
  });
  const workbench = useWorkbench({
    autoRefreshIntervalMs: 10_000,
    initialAutoRefresh,
    launcherRunId: activeLauncherRunId,
  });
  const control = useProjectLaunchControl({ initialForm: INITIAL_LAUNCH_REQUEST });
  const review = useMemo(
    () => sheetReview(control.review, control.form.target_identifier),
    [control.form.target_identifier, control.review],
  );
  const [manualDraft, setManualDraft] = useState<{
    planId: string;
    request: ManualApprovalRequest;
  } | null>(null);
  const approvalControl = control.lastControl?.approval_control ?? null;
  const manualRequest = manualDraft && manualDraft.planId === approvalControl?.plan_id
    ? manualDraft.request
    : approvalRequest(approvalControl);
  const refreshControlStatus = control.refreshStatus;

  useEffect(() => {
    if (!activeLauncherRunId) return;
    const timer = window.setInterval(
      () => void refreshControlStatus(activeLauncherRunId),
      10_000,
    );
    return () => window.clearInterval(timer);
  }, [activeLauncherRunId, refreshControlStatus]);
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

  async function launchProject(submission: ProjectLaunchSubmission) {
    control.setForm(submission.request);
    const result = await control.launch(submission.request.options);
    const launcherRunId = result?.launcher?.launcher_run_id;
    if (!launcherRunId) return;
    window.sessionStorage.setItem(ACTIVE_LAUNCHER_KEY, launcherRunId);
    setActiveLauncherRunId(launcherRunId);
    closeLaunchSheet();
  }

  async function refreshAll() {
    await Promise.all([
      workbench.refresh(),
      activeLauncherRunId ? control.refreshStatus(activeLauncherRunId) : Promise.resolve(null),
    ]);
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
        onRefresh={() => void refreshAll()}
        onAutoRefreshChange={setAutoRefresh}
        approvalControl={approvalControl}
        manualApprovalRequest={manualRequest}
        approvalPending={control.mutationInFlight}
        approvalError={control.error}
        onManualApprovalRequestChange={(request) => setManualDraft({ planId: request.plan_id, request })}
        onApproveAndContinue={(request) => void control.approveAndContinue(request).then(() => refreshAll())}
      />;

  return <>
    {content}
    {launchSheetOpen ? <ProjectLaunchSheet
      onClose={closeLaunchSheet}
      review={review}
      mutation={control.status === "resolving" ? "resolve" : control.status === "approving" ? "approve" : control.status === "launching" ? "launch" : null}
      error={control.error}
      launcherRunId={activeLauncherRunId ?? null}
      initialRequest={control.form}
      onResolveDraft={(request) => void control.createDraft(request)}
      onApproveDraft={() => void control.approveDraft()}
      onCreateAndLaunch={(submission) => void launchProject(submission)}
    /> : null}
  </>;
}
