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

          {!selected ? (
            <p className="domain-empty">Select an Evidence record to inspect it.</p>
          ) : (
            <article className="evidence-detail" aria-live="polite">
              <header><h3>{selected.event_type ?? "unknown_event"}</h3><code>{selected.event_id ?? "Unavailable"}</code></header>
              <dl className="domain-fields">
                <div><dt>Timestamp</dt><dd>{selected.timestamp ?? "Unavailable"}</dd></div>
                <div><dt>Agent</dt><dd>{selected.agent ?? "Unavailable"}</dd></div>
                <div><dt>Round</dt><dd>{selected.round ?? "Unavailable"}</dd></div>
                <div><dt>Targets</dt><dd>{returnedList(selected.targets)}</dd></div>
                <div><dt>Run relationship</dt><dd>{selected.run_relation.replaceAll("_", " ")}</dd></div>
                <div><dt>Message</dt><dd>{selected.message ?? "No message returned"}</dd></div>
              </dl>
              <h4>Protocol</h4>
              <ProtocolDetail protocol={selected.protocol} />
              <h4>Trace</h4>
              <TraceDetail trace={selected.trace} />
            </article>
          )}
        </div>
      )}
    </section>
  );
}
