import type { EvidenceView } from "../domain";
import { candidateEvidence } from "../scientific-selectors";
import { ProtocolDetail, TraceDetail } from "./artifact-trace";

export interface EvidenceProvenanceProps {
  evidence: EvidenceView[];
  selectedEvidenceId: string | null;
  onSelectEvidence: (eventId: string) => void;
  candidateId?: string | null;
}

function returnedList(values?: string[]): string {
  return values?.length ? values.join(", ") : "None returned";
}

export function EvidenceRecordDetail({
  evidence,
}: {
  evidence: EvidenceView | null;
}) {
  if (!evidence) {
    return <p className="domain-empty">Select an Evidence record to inspect it.</p>;
  }
  return (
    <article className="evidence-detail" aria-live="polite">
      <header>
        <h3>{evidence.event_type ?? "unknown_event"}</h3>
        <code>{evidence.event_id ?? "Unavailable"}</code>
      </header>
      <dl className="domain-fields">
        <div><dt>Timestamp</dt><dd>{evidence.timestamp ?? "Unavailable"}</dd></div>
        <div><dt>Agent</dt><dd>{evidence.agent ?? "Unavailable"}</dd></div>
        <div><dt>Round</dt><dd>{evidence.round ?? "Unavailable"}</dd></div>
        <div><dt>Targets</dt><dd>{returnedList(evidence.targets)}</dd></div>
        <div><dt>Run relationship</dt><dd>{evidence.run_relation.replaceAll("_", " ")}</dd></div>
        <div><dt>Message</dt><dd>{evidence.message ?? "No message returned"}</dd></div>
      </dl>
      <h4>Protocol</h4>
      <ProtocolDetail protocol={evidence.protocol} />
      <h4>Trace</h4>
      <TraceDetail trace={evidence.trace} />
    </article>
  );
}

export function EvidenceProvenance({
  evidence,
  selectedEvidenceId,
  onSelectEvidence,
  candidateId,
}: EvidenceProvenanceProps) {
  const visibleEvidence = candidateId
    ? candidateEvidence(candidateId, evidence)
    : evidence;
  const selected =
    visibleEvidence.find((item) => item.event_id === selectedEvidenceId) ?? null;

  return (
    <section className="evidence-provenance" aria-labelledby="evidence-heading">
      <header className="domain-section-header">
        <div>
          <span className="domain-kicker">STRUCTURED PROVENANCE</span>
          <h2 id="evidence-heading">Evidence timeline</h2>
        </div>
        {candidateId ? <span>Candidate trace: {candidateId}</span> : <span>Project scope</span>}
      </header>

      {visibleEvidence.length === 0 ? (
        <p className="domain-empty">No formally linked Evidence returned.</p>
      ) : (
        <div className="evidence-inspector-grid">
          <ol className="evidence-timeline" aria-label="Evidence records">
            {visibleEvidence.map((item, index) => (
              <li key={item.event_id ?? `evidence-${index}`}>
                <button
                  type="button"
                  disabled={!item.event_id}
                  aria-pressed={item.event_id === selected?.event_id}
                  onClick={() => item.event_id && onSelectEvidence(item.event_id)}
                >
                  <b>{item.event_type ?? "unknown_event"}</b>
                  <time>{item.timestamp ?? "Timestamp unavailable"}</time>
                  <span>{item.agent ?? "Agent unavailable"} · {item.run_relation.replaceAll("_", " ")}</span>
                </button>
              </li>
            ))}
          </ol>

          <EvidenceRecordDetail evidence={selected} />
        </div>
      )}
    </section>
  );
}
