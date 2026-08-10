import type { ReactNode } from "react";

export interface WorkbenchFrameProps {
  topBar: ReactNode;
  navigator: ReactNode;
  primary: ReactNode;
  inspector: ReactNode;
  history: ReactNode;
  inspectorCollapsed: boolean;
  historyCollapsed: boolean;
}

export function WorkbenchFrame({
  topBar,
  navigator,
  primary,
  inspector,
  history,
  inspectorCollapsed,
  historyCollapsed,
}: WorkbenchFrameProps) {
  return (
    <main
      className="workbench-frame"
      data-inspector-collapsed={inspectorCollapsed || undefined}
      data-history-collapsed={historyCollapsed || undefined}
    >
      <a className="skip-link" href="#workbench-primary">Skip to selected workspace</a>
      {topBar}
      {navigator}
      {primary}
      {inspector}
      {history}
    </main>
  );
}
