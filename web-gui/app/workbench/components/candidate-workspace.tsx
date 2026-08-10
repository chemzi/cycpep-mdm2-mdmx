import type {
  ArtifactView,
  BoundedCollection,
  CandidateView,
  EvidenceView,
  MetricValue,
} from "../domain";
import {
  candidateArtifacts,
  candidateById,
  candidateEvidence,
  metricEntries,
} from "../scientific-selectors";

export interface CandidateWorkspaceProps {
  candidates: BoundedCollection<CandidateView>;
  evidence: EvidenceView[];
  artifacts: ArtifactView[];
  selectedCandidateId: string | null;
  onSelectCandidate: (candidateId: string) => void;
}

function relationLabel(candidate: CandidateView): string {
  return candidate.run_relation.replaceAll("_", " ");
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
    ? candidateEvidence(selected.candidate_id, evidence)
    : [];
  const associatedArtifacts = selected?.candidate_id
    ? candidateArtifacts(selected.candidate_id, artifacts)
    : [];

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
                </dl>

                <section aria-labelledby="candidate-metrics-heading">
                  <h4 id="candidate-metrics-heading">Returned metrics</h4>
                  {metricEntries(selected).length === 0 ? (
                    <p className="domain-empty">No metrics returned.</p>
                  ) : (
                    <dl className="domain-fields">
                      {metricEntries(selected).map(([name, value]) => (
                        <div key={name}><dt>{name}</dt><dd>{displayMetric(value)}</dd></div>
                      ))}
                    </dl>
                  )}
                </section>

                <section aria-labelledby="candidate-links-heading">
                  <h4 id="candidate-links-heading">Formal candidate associations</h4>
                  <p>
                    {associatedEvidence.length} Evidence · {associatedArtifacts.length} artifacts
                  </p>
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
