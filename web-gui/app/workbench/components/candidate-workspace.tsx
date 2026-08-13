import type {
  ArtifactView,
  BoundedCollection,
  CandidateView,
  EvidenceView,
  MetricValue,
} from "../domain";
import { isExplorationShortlistEvidence } from "../domain";
import {
  candidateArtifacts,
  candidateById,
  candidateEvidence,
  isStructureBearingArtifact,
  metricEntries,
} from "../scientific-selectors";
import { ArtifactContentAvailability } from "./artifact-trace";
import { StructureViewer } from "./structure-viewer";

export interface CandidateWorkspaceProps {
  candidates: BoundedCollection<CandidateView>;
  evidence: BoundedCollection<EvidenceView>;
  artifacts: BoundedCollection<ArtifactView>;
  selectedCandidateId: string | null;
  onSelectCandidate: (candidateId: string) => void;
}

function relationLabel(candidate: CandidateView): string {
  return (candidate.associations?.status_owner?.run_relation ?? candidate.run_relation)
    .replaceAll("_", " ");
}

function displayMetric(value: MetricValue): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

export function CandidateWorkspace({
  candidates,
  evidence,
  artifacts,
  selectedCandidateId,
  onSelectCandidate,
}: CandidateWorkspaceProps) {
  const selected = candidateById(candidates.items, selectedCandidateId);
  const associatedEvidence = selected?.candidate_id
    ? candidateEvidence(selected.candidate_id, evidence.items)
    : [];
  const associatedArtifacts = selected?.candidate_id
    ? candidateArtifacts(selected.candidate_id, artifacts.items)
    : [];
  const summary = selected?.associations;
  const summaryStructure = summary?.structures[0];
  const structureArtifact: ArtifactView | null = summaryStructure
    ? {
      ...summaryStructure,
      trace: {
        candidate_id: selected?.candidate_id,
        artifact_id: summaryStructure.artifact_id,
      },
      run_relation: summary?.status_owner?.run_relation ?? selected?.run_relation ?? "unlinked",
    }
    : associatedArtifacts.find(isStructureBearingArtifact) ?? null;
  const shortlistRelationships = summary
    ? summary.shortlist.map((entry) => ({ entry, eventId: entry.event_id }))
    : selected?.candidate_id
    ? evidence.items.flatMap((item) => {
      if (!isExplorationShortlistEvidence(item)) return [];
      return item.shortlist
        .filter((entry) => entry.candidate_id === selected.candidate_id)
        .map((entry) => ({ entry, eventId: item.event_id }));
    })
    : [];
  const evidenceWindowLimited = evidence.truncated && !summary;
  const artifactWindowLimited = artifacts.truncated && !summary;
  const associationWindowLimited = evidenceWindowLimited || artifactWindowLimited;
  const metricsUnreadable = summary?.limitations.some(
    (item) => item.code === "candidate_metrics_malformed",
  ) ?? false;

  return (
    <section className="candidate-workspace" aria-labelledby="candidate-heading">
      <header className="domain-section-header">
        <div>
          <span className="domain-kicker">SCIENTIFIC CANDIDATES</span>
          <h2 id="candidate-heading">Candidate workspace</h2>
        </div>
        <span aria-label="Candidate collection coverage">
          {candidates.returned} / {candidates.total} returned
          {candidates.truncated ? " · truncated" : ""}
        </span>
      </header>

      {candidates.items.length === 0 ? (
        <p className="domain-empty">No candidates returned by the Store read model.</p>
      ) : (
        <div className="candidate-workspace-grid">
          <ul className="candidate-browser" aria-label="Candidates">
            {candidates.items.map((candidate) => {
              const candidateId = candidate.candidate_id;
              const selectedNow = candidateId === selected?.candidate_id;
              return <li key={candidateId ?? `${candidate.sequence}-${candidate.created_at}`}>
                <button
                  type="button"
                  aria-pressed={selectedNow}
                  className={selectedNow ? "is-selected" : undefined}
                  disabled={!candidateId}
                  onClick={() => candidateId && onSelectCandidate(candidateId)}
                >
                  <b>{candidateId ?? "Candidate identity unavailable"}</b>
                  <code>{candidate.sequence ?? "Sequence unavailable"}</code>
                  <span>{relationLabel(candidate)}</span>
                </button>
              </li>;
            })}
          </ul>

          <article className="candidate-detail" aria-live="polite">
            {!selected ? (
              <p className="domain-empty">Select a returned candidate to inspect it.</p>
            ) : (
              <>
                <header>
                  <div>
                    <span className="domain-kicker">CANDIDATE IDENTITY</span>
                    <h3>{selected.candidate_id ?? "Unavailable"}</h3>
                  </div>
                  <span>{relationLabel(selected)}</span>
                </header>
                <dl className="domain-fields">
                  <div><dt>Sequence</dt><dd><code>{selected.sequence ?? "Unavailable"}</code></dd></div>
                  <div><dt>Status</dt><dd>{selected.status ?? "Unavailable"}</dd></div>
                  <div><dt>Final status</dt><dd>{selected.final_status ?? "Unavailable"}</dd></div>
                  <div><dt>Source route</dt><dd>{selected.source_route ?? "Unavailable"}</dd></div>
                  {summary?.status_owner ? (
                    <div>
                      <dt>Status-owning run</dt>
                      <dd>
                        <code>{summary.status_owner.run_id}</code>
                        {` · ${summary.status_owner.run_relation.replaceAll("_", " ")}`}
                      </dd>
                    </div>
                  ) : null}
                </dl>

                <section
                  className="candidate-structure-stage"
                  aria-labelledby="candidate-structure-heading"
                >
                  <h4 id="candidate-structure-heading">Structure availability</h4>
                  {structureArtifact ? (
                    <>
                      <p>
                        Structure artifact recorded: <code>{structureArtifact.artifact_id ?? "Opaque identity unavailable"}</code>
                        {structureArtifact.artifact_type ? ` · ${structureArtifact.artifact_type}` : ""}
                      </p>
                      <ArtifactContentAvailability artifact={structureArtifact} />
                      <StructureViewer artifact={structureArtifact} />
                    </>
                  ) : associationWindowLimited ? (
                    <p className="domain-empty">
                      Structure association unavailable in the returned window. Additional associations may be omitted.
                    </p>
                  ) : (
                    <p className="domain-empty">No formal structure artifact is associated with this Candidate.</p>
                  )}
                </section>

                <section aria-labelledby="candidate-metrics-heading">
                  <h4 id="candidate-metrics-heading">Returned metrics</h4>
                  {metricEntries(selected).length === 0 ? (
                    <p className="domain-empty">
                      {metricsUnreadable
                        ? "Formal metrics are present but could not be read."
                        : "No formal metrics are recorded for this Candidate."}
                    </p>
                  ) : (
                    <dl className="domain-fields">
                      {metricEntries(selected).map(([name, value]) => (
                        <div key={name}><dt>{name}</dt><dd>{displayMetric(value)}</dd></div>
                      ))}
                    </dl>
                  )}
                </section>

                <section aria-labelledby="candidate-shortlist-heading">
                  <h4 id="candidate-shortlist-heading">Exploration shortlist relationship</h4>
                  {shortlistRelationships.length === 0 && evidenceWindowLimited ? (
                    <p className="domain-empty">
                      Shortlist association unavailable in the returned Evidence window. Additional associations may be omitted.
                    </p>
                  ) : shortlistRelationships.length === 0 ? (
                    <p className="domain-empty">No formal shortlist explicitly references this Candidate.</p>
                  ) : (
                    <ul className="candidate-shortlist-relationships">
                      {shortlistRelationships.map(({ entry, eventId }, index) => (
                        <li
                          key={`${eventId ?? "shortlist"}-${index}`}
                          data-scientific-status={entry.passed ? "passed" : "exploratory"}
                        >
                          <code>{eventId ?? "Opaque Evidence identity unavailable"}</code>
                          <span>passed: {String(entry.passed)}</span>
                          <span>{entry.reason}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </section>

                <section aria-labelledby="candidate-links-heading">
                  <h4 id="candidate-links-heading">Formal candidate associations</h4>
                  <p>
                    {summary
                      ? `${summary.evidence_total} Evidence · ${summary.artifact_total} artifacts · ${summary.complete ? "complete" : "limited"}`
                      : `${associatedEvidence.length} Evidence · ${associatedArtifacts.length} artifacts · returned window`}
                  </p>
                  {!summary && evidence.truncated ? (
                    <p>Evidence window: {evidence.returned} of {evidence.total} returned.</p>
                  ) : null}
                  {!summary && artifacts.truncated ? (
                    <p>Artifact window: {artifacts.returned} of {artifacts.total} returned.</p>
                  ) : null}
                  {associationWindowLimited ? (
                    <p>Additional associations may be omitted.</p>
                  ) : null}
                  {summary?.limitations.map((limitation) => (
                    <p key={limitation.code} data-association-limitation={limitation.code}>
                      {limitation.summary}
                    </p>
                  ))}
                  <ul>
                    {associatedEvidence.map((item, index) => (
                      <li key={item.event_id ?? `evidence-${index}`}>
                        Evidence: {item.event_id ?? "opaque identity unavailable"}
                      </li>
                    ))}
                    {associatedArtifacts.map((item, index) => (
                      <li key={item.artifact_id ?? `artifact-${index}`}>
                        Artifact: {item.artifact_id ?? "opaque identity unavailable"}
                      </li>
                    ))}
                    {summary?.artifact_ids
                      .filter((artifactId) => !associatedArtifacts.some(
                        (item) => item.artifact_id === artifactId,
                      ))
                      .map((artifactId) => (
                        <li key={artifactId}>Artifact: {artifactId}</li>
                      ))}
                  </ul>
                </section>
              </>
            )}
          </article>
        </div>
      )}
    </section>
  );
}
