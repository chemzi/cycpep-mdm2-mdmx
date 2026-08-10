"use client";

import { useMemo, useState } from "react";

import { isExplorationShortlistEvidence } from "./domain";
import { ArtifactTraceInspector } from "./components/artifact-trace";
import { CandidateWorkspace } from "./components/candidate-workspace";
import { EvidenceProvenance } from "./components/evidence-provenance";
import { ExplorationShortlist } from "./components/exploration-shortlist";
import { CollectionSummary, FailureState, LoadingState } from "./components/shared-states";
import { TaskGraph } from "./components/task-graph";
import { ResultsSummary } from "./components/results-summary";
import { WorkbenchShell } from "./components/workbench-shell";
import { useResults } from "./use-results";
import { useWorkbench } from "./use-workbench";
import { useBoundedSelection } from "./selection";

const AUTO_REFRESH_KEY = "cycpep-workbench-v2-auto-refresh";

export function WorkbenchPage() {
  const [initialAutoRefresh] = useState(() => {
    if (typeof window === "undefined") return true;
    const stored = window.localStorage.getItem(AUTO_REFRESH_KEY);
    return stored === null ? true : stored === "true";
  });
  const workbench = useWorkbench({
    autoRefreshIntervalMs: 10_000,
    initialAutoRefresh,
  });
  const results = useResults({
    autoRefreshIntervalMs: 10_000,
    initialAutoRefresh,
  });
  const model = workbench.data?.data ?? null;

  function setAutoRefresh(enabled: boolean) {
    window.localStorage.setItem(AUTO_REFRESH_KEY, String(enabled));
    workbench.setAutoRefreshEnabled(enabled);
  }

  const candidateIds = useMemo(
    () => model?.candidates.items
      .map((candidate) => candidate.candidate_id)
      .filter((identity): identity is string => Boolean(identity)) ?? [],
    [model?.candidates.items],
  );
  const evidenceIds = useMemo(
    () => model?.evidence.items
      .map((evidence) => evidence.event_id)
      .filter((identity): identity is string => Boolean(identity)) ?? [],
    [model?.evidence.items],
  );
  const artifactIds = useMemo(
    () => model?.artifacts.items
      .map((artifact) => artifact.artifact_id)
      .filter((identity): identity is string => Boolean(identity)) ?? [],
    [model?.artifacts.items],
  );
  const [selectedCandidateId, setSelectedCandidateId] = useBoundedSelection(candidateIds);
  const [selectedEvidenceId, setSelectedEvidenceId] = useBoundedSelection(evidenceIds);
  const [selectedArtifactId, setSelectedArtifactId] = useBoundedSelection(artifactIds);

  if (!model && workbench.status === "failed-before-data") {
    return <main className="initial-state"><FailureState message={workbench.error ?? "Workbench request failed"}/></main>;
  }
  if (!model) {
    return <main className="initial-state"><LoadingState label="Loading Frontend V2 workbench"/></main>;
  }

  const shortlists = model.evidence.items.filter(isExplorationShortlistEvidence);
  const collections = [
    ["Tasks", model.tasks],
    ["Executions", model.executions],
    ["Transactions", model.transactions],
    ["Candidates", model.candidates],
    ["Evidence", model.evidence],
    ["Artifacts", model.artifacts],
    ["Protocols", model.protocols],
    ["Blockers", model.blockers],
  ] as const;

  return <WorkbenchShell
    data={model}
    requestStatus={workbench.status}
    refreshError={workbench.error}
    autoRefreshEnabled={workbench.autoRefreshEnabled}
    onRefresh={() => void workbench.refresh()}
    onAutoRefreshChange={setAutoRefresh}
  >
    <section className="collection-coverage" aria-labelledby="coverage-heading">
      <div className="domain-section-header">
        <div><span className="domain-kicker">BOUNDED RESPONSE</span><h2 id="coverage-heading">Collection coverage</h2></div>
      </div>
      <div>{collections.map(([label, collection]) => <CollectionSummary key={label} label={label} collection={collection}/>)}</div>
    </section>

    <div className="primary-workspace-grid">
      <TaskGraph
        tasks={model.tasks}
        executions={model.executions}
        transactions={model.transactions}
        blockers={model.blockers.items}
      />
      <CandidateWorkspace
        candidates={model.candidates}
        evidence={model.evidence.items}
        artifacts={model.artifacts.items}
        selectedCandidateId={selectedCandidateId}
        onSelectCandidate={setSelectedCandidateId}
      />
    </div>

    <section className="results-section" aria-labelledby="results-digest-heading">
      <ResultsSummary
        digest={results.data}
        error={results.error}
        refreshing={results.status === "refreshing"}
      />
    </section>

    <section className="scientific-results" aria-labelledby="scientific-results-heading">
      <div className="domain-section-header">
        <div><span className="domain-kicker">SCIENTIFIC EVIDENCE</span><h2 id="scientific-results-heading">Exploration results</h2></div>
      </div>
      {shortlists.length === 0
        ? <p className="domain-empty">No exploration_shortlist Evidence returned.</p>
        : shortlists.map((shortlist, index) => <ExplorationShortlist
            key={shortlist.event_id ?? `shortlist-${index}`}
            shortlist={shortlist}
            evidence={model.evidence.items}
            headingId={`exploration-shortlist-${index}-title`}
            passedHeadingId={`exploration-shortlist-${index}-passed-title`}
            onSelectEvidence={setSelectedEvidenceId}
          />)}
    </section>

    <div className="provenance-grid">
      <EvidenceProvenance
        evidence={model.evidence.items}
        selectedEvidenceId={selectedEvidenceId}
        onSelectEvidence={setSelectedEvidenceId}
      />
      <ArtifactTraceInspector
        artifacts={model.artifacts.items}
        protocols={model.protocols.items}
        selectedArtifactId={selectedArtifactId}
        onSelectArtifact={setSelectedArtifactId}
      />
    </div>
  </WorkbenchShell>;
}
