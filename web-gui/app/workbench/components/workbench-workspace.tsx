import type { ReactNode } from "react";

import type { WorkbenchReadModel } from "../domain";
import type { WorkbenchRequestStatus } from "../request-lifecycle";
import { reconcileWorkbenchSelection } from "../selection";
import type { WorkbenchSelection } from "../selection";
import { WorkbenchFrame } from "./workbench-frame";
import { WorkbenchHistory } from "./workbench-history";
import { WorkbenchInspector } from "./workbench-inspector";
import { WorkbenchNavigator } from "./workbench-navigator";
import { WorkbenchPrimary } from "./workbench-primary";
import { WorkbenchTopBar } from "./workbench-top-bar";

export type WorkbenchAuxiliaryPanel = "inspector" | "history";

export interface WorkbenchWorkspaceProps {
  data: WorkbenchReadModel;
  requestStatus: WorkbenchRequestStatus;
  refreshError: string | null;
  autoRefreshEnabled: boolean;
  onRefresh: () => void;
  onAutoRefreshChange: (enabled: boolean) => void;
  results?: ReactNode;
  initialSelection?: WorkbenchSelection;
  initialCollapsedPanels?: WorkbenchAuxiliaryPanel[];
  selection?: WorkbenchSelection;
  collapsedPanels?: WorkbenchAuxiliaryPanel[];
  onSelectionChange?: (selection: WorkbenchSelection) => void;
  onPanelCollapsedChange?: (panel: WorkbenchAuxiliaryPanel, collapsed: boolean) => void;
}

const OVERVIEW: WorkbenchSelection = { kind: "overview", identity: null };

/**
 * Public presentation seam. It is intentionally hook-free so frozen read-model
 * fixtures can render the same workspace used by the browser.
 */
export function WorkbenchWorkspace({
  data,
  requestStatus,
  refreshError,
  autoRefreshEnabled,
  onRefresh,
  onAutoRefreshChange,
  results,
  initialSelection = OVERVIEW,
  initialCollapsedPanels = [],
  selection: controlledSelection,
  collapsedPanels: controlledCollapsedPanels,
  onSelectionChange = () => undefined,
  onPanelCollapsedChange = () => undefined,
}: WorkbenchWorkspaceProps) {
  const selection = reconcileWorkbenchSelection(controlledSelection ?? initialSelection, data);
  const collapsedPanels = controlledCollapsedPanels ?? initialCollapsedPanels;
  const inspectorCollapsed = collapsedPanels.includes("inspector");
  const historyCollapsed = collapsedPanels.includes("history");

  return <WorkbenchFrame
    inspectorCollapsed={inspectorCollapsed}
    historyCollapsed={historyCollapsed}
    topBar={<WorkbenchTopBar
      data={data}
      requestStatus={requestStatus}
      refreshError={refreshError}
      autoRefreshEnabled={autoRefreshEnabled}
      onRefresh={onRefresh}
      onAutoRefreshChange={onAutoRefreshChange}
    />}
    navigator={<WorkbenchNavigator
      data={data}
      selection={selection}
      onSelectionChange={onSelectionChange}
    />}
    primary={<section id="workbench-primary" className="primary-workspace" aria-label="Selected workspace">
      <WorkbenchPrimary data={data} selection={selection} onSelectionChange={onSelectionChange} />
      {results}
    </section>}
    inspector={<WorkbenchInspector
      data={data}
      selection={selection}
      collapsed={inspectorCollapsed}
      onCollapsedChange={(collapsed) => onPanelCollapsedChange("inspector", collapsed)}
      onSelectionChange={onSelectionChange}
    />}
    history={<WorkbenchHistory
      data={data}
      selection={selection}
      collapsed={historyCollapsed}
      onCollapsedChange={(collapsed) => onPanelCollapsedChange("history", collapsed)}
    />}
  />;
}
