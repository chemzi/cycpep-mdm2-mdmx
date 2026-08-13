"use client";

import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";

import {
  ControlRequestError,
  ProjectControlClient,
  type ProjectControlClientOptions,
} from "./control-client";
import { assertLauncherRunId } from "./control-contract";
import type {
  ManualApprovalRequest,
  ProjectControlView,
  ProjectDraftProjection,
  ProjectLaunchOptions,
  ProjectLaunchRequest,
} from "./control-domain";

export const LAUNCH_ATTEMPT_STORAGE_KEY = "cycpep-project-launch-attempt-v1";

export type ProjectLaunchStatus =
  | "editing" | "resolving" | "review-ready" | "approving"
  | "launching" | "launched" | "checking" | "continuing" | "failed";

export interface ProjectLaunchState {
  status: ProjectLaunchStatus;
  form: ProjectLaunchRequest;
  review: ProjectDraftProjection | null;
  lastControl: ProjectControlView | null;
  error: string | null;
}

export type ProjectLaunchAction =
  | { type: "form-changed"; form: ProjectLaunchRequest }
  | { type: "mutation-started"; status: Exclude<ProjectLaunchStatus, "editing" | "failed"> }
  | { type: "draft-succeeded"; review: ProjectDraftProjection }
  | { type: "control-succeeded"; control: ProjectControlView }
  | { type: "mutation-failed"; error: string; control?: ProjectControlView | null };

export interface LaunchAttemptStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export interface PersistedLaunchAttempt {
  draft_id: string;
  project_id: string;
  draft_content_digest: string;
  launcher_run_id: string;
}

export function initialProjectLaunchState(form: ProjectLaunchRequest): ProjectLaunchState {
  return { status: "editing", form, review: null, lastControl: null, error: null };
}

export function projectLaunchReducer(
  state: ProjectLaunchState,
  action: ProjectLaunchAction,
): ProjectLaunchState {
  switch (action.type) {
    case "form-changed":
      return { ...state, status: "editing", form: action.form, error: null };
    case "mutation-started":
      return { ...state, status: action.status, error: null };
    case "draft-succeeded":
      return { ...state, status: "review-ready", review: action.review, error: null };
    case "control-succeeded":
      return { ...state, status: "launched", lastControl: action.control, error: null };
    case "mutation-failed":
      return {
        ...state,
        status: "failed",
        lastControl: action.control === undefined ? state.lastControl : action.control,
        error: action.error,
      };
  }
}

function reviewDigest(review: ProjectDraftProjection): string {
  const digest = review.review.content_digest;
  if (typeof digest !== "string" || !/^[0-9a-f]{64}$/.test(digest)) {
    throw new Error("Draft review has no valid content identity");
  }
  return digest;
}

function readAttempt(storage: LaunchAttemptStorage): PersistedLaunchAttempt | null {
  try {
    const raw = storage.getItem(LAUNCH_ATTEMPT_STORAGE_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<PersistedLaunchAttempt>;
    if (
      typeof value.draft_id !== "string" || typeof value.project_id !== "string" ||
      typeof value.draft_content_digest !== "string" ||
      typeof value.launcher_run_id !== "string"
    ) return null;
    assertLauncherRunId(value.launcher_run_id);
    return value as PersistedLaunchAttempt;
  } catch {
    return null;
  }
}

export function prepareLaunchAttempt(
  storage: LaunchAttemptStorage,
  review: ProjectDraftProjection,
  launcherIdFactory: () => string = generateLauncherRunId,
): PersistedLaunchAttempt {
  const identity = {
    draft_id: review.draft_id,
    project_id: review.project_id,
    draft_content_digest: reviewDigest(review),
  };
  const existing = readAttempt(storage);
  if (
    existing && existing.draft_id === identity.draft_id &&
    existing.project_id === identity.project_id &&
    existing.draft_content_digest === identity.draft_content_digest
  ) return existing;
  const launcherRunId = launcherIdFactory();
  assertLauncherRunId(launcherRunId);
  const attempt = { ...identity, launcher_run_id: launcherRunId };
  storage.setItem(LAUNCH_ATTEMPT_STORAGE_KEY, JSON.stringify(attempt));
  return attempt;
}

export function generateLauncherRunId(): string {
  return `launcher_${crypto.randomUUID().replaceAll("-", "")}`;
}

export interface UseProjectLaunchControlOptions extends ProjectControlClientOptions {
  initialForm: ProjectLaunchRequest;
  storage?: LaunchAttemptStorage;
  launcherIdFactory?: () => string;
}

export interface UseProjectLaunchControlResult extends ProjectLaunchState {
  mutationInFlight: boolean;
  setForm(form: ProjectLaunchRequest): void;
  createDraft(form?: ProjectLaunchRequest): Promise<ProjectDraftProjection | null>;
  retrieveDraft(draftId: string): Promise<ProjectDraftProjection | null>;
  approveDraft(justification?: string): Promise<ProjectDraftProjection | null>;
  launch(options: ProjectLaunchOptions): Promise<ProjectControlView | null>;
  refreshStatus(launcherRunId: string): Promise<ProjectControlView | null>;
  approveAndContinue(request: ManualApprovalRequest): Promise<ProjectControlView | null>;
}

export function useProjectLaunchControl(
  options: UseProjectLaunchControlOptions,
): UseProjectLaunchControlResult {
  const {
    initialForm, storage, launcherIdFactory, apiOrigin, fetchImpl,
  } = options;
  const client = useMemo(
    () => new ProjectControlClient({ apiOrigin, fetchImpl }),
    [apiOrigin, fetchImpl],
  );
  const [state, dispatch] = useReducer(
    projectLaunchReducer,
    initialForm,
    initialProjectLaunchState,
  );
  const activeMutation = useRef<AbortController | null>(null);
  const stateRef = useRef(state);
  useEffect(() => { stateRef.current = state; }, [state]);

  const run = useCallback(async <T,>(
    status: Exclude<ProjectLaunchStatus, "editing" | "failed">,
    operation: (signal: AbortSignal) => Promise<T>,
    success: (value: T) => void,
    recover?: (error: ControlRequestError) => T | null,
  ): Promise<T | null> => {
    if (activeMutation.current) return null;
    const controller = new AbortController();
    activeMutation.current = controller;
    dispatch({ type: "mutation-started", status });
    try {
      const value = await operation(controller.signal);
      success(value);
      return value;
    } catch (cause) {
      const control = cause instanceof ControlRequestError ? cause.control : undefined;
      dispatch({
        type: "mutation-failed",
        error: cause instanceof Error ? cause.message : "Control request failed",
        control,
      });
      return cause instanceof ControlRequestError && recover ? recover(cause) : null;
    } finally {
      if (activeMutation.current === controller) activeMutation.current = null;
    }
  }, []);

  const createDraft = useCallback((form?: ProjectLaunchRequest) => {
    const request = form ?? stateRef.current.form;
    if (form) dispatch({ type: "form-changed", form });
    return run(
      "resolving",
      (signal) => client.createDraft(request, signal).then((value) => value.data),
      (review) => dispatch({ type: "draft-succeeded", review }),
    );
  }, [client, run]);

  const retrieveDraft = useCallback((draftId: string) => run(
    "resolving",
    (signal) => client.retrieveDraft(draftId, signal).then((value) => value.data),
    (review) => dispatch({ type: "draft-succeeded", review }),
  ), [client, run]);

  const approveDraft = useCallback((justification?: string) => {
    const review = stateRef.current.review;
    if (!review) return Promise.resolve(null);
    return run(
      "approving",
      (signal) => client.approveDraft(review.draft_id, justification, signal)
        .then((value) => value.data),
      (approved) => dispatch({ type: "draft-succeeded", review: approved }),
    );
  }, [client, run]);

  const launch = useCallback((launchOptions: ProjectLaunchOptions) => {
    const review = stateRef.current.review;
    const session = storage ?? window.sessionStorage;
    if (!review) return Promise.resolve(null);
    const attempt = prepareLaunchAttempt(session, review, launcherIdFactory);
    const boundOptions = { ...launchOptions, launcher_run_id: attempt.launcher_run_id };
    return run(
      "launching",
      (signal) => client.launchDraft(review.draft_id, boundOptions, signal)
        .then((value) => value.data),
      (control) => dispatch({ type: "control-succeeded", control }),
      (error) => error.control,
    );
  }, [client, launcherIdFactory, run, storage]);

  const refreshStatus = useCallback((launcherRunId: string) => run(
    "checking",
    (signal) => client.status(launcherRunId, signal).then((value) => value.data),
    (control) => dispatch({ type: "control-succeeded", control }),
    (error) => error.control,
  ), [client, run]);

  const approveAndContinue = useCallback((request: ManualApprovalRequest) => run(
    "continuing",
    (signal) => client.approveAndContinue(request, signal).then((value) => value.data),
    (control) => dispatch({ type: "control-succeeded", control }),
    (error) => error.control,
  ), [client, run]);

  return {
    ...state,
    mutationInFlight: [
      "resolving", "approving", "launching", "checking", "continuing",
    ].includes(state.status),
    setForm: (form) => dispatch({ type: "form-changed", form }),
    createDraft,
    retrieveDraft,
    approveDraft,
    launch,
    refreshStatus,
    approveAndContinue,
  };
}
