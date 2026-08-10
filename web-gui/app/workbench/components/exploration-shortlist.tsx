import type {
  EvidenceView,
  ExplorationShortlistEvidence,
} from "../domain";
import { explorationShortlistPresentation } from "../scientific-selectors";

export interface ExplorationShortlistProps {
  shortlist: ExplorationShortlistEvidence;
  evidence: EvidenceView[];
  headingId: string;
  passedHeadingId: string;
  onSelectEvidence: (eventId: string) => void;
}

function returnedValue(value: string | number | null): string {
  return value === null ? "Unavailable" : String(value);
}

export function ExplorationShortlist({
  shortlist,
  evidence,
  headingId,
  passedHeadingId,
  onSelectEvidence,
}: ExplorationShortlistProps) {
  const presentation = explorationShortlistPresentation(shortlist, evidence);

  return (
    <section className="exploration-shortlist" aria-labelledby={headingId}>
      <div className="passed-summary" aria-labelledby={passedHeadingId}>
        <span className="domain-kicker" id={passedHeadingId}>PASSED</span>
        <strong>{presentation.passedSummary}</strong>
      </div>

      <div className="shortlist-results">
        <header>
          <span className="domain-kicker">EXPLORATORY EVIDENCE</span>
          <h2 id={headingId}>Exploration shortlist</h2>
          <p>Shortlist membership is not a scientific pass.</p>
        </header>
        {presentation.shortlist.length === 0 ? (
          <p className="domain-empty">No shortlist items returned.</p>
        ) : (
          <div role="list" aria-label="Exploration shortlist candidates">
            {presentation.shortlist.map((item) => (
              <article
                className="shortlist-item"
                data-candidate-id={item.candidate_id}
                data-scientific-status={item.passed ? "passed" : "exploratory"}
                role="listitem"
                key={item.candidate_id}
              >
                <header>
                  <b>{item.candidate_id}</b>
                  <span className={item.passed ? "is-passed" : "is-exploratory"}>
                    passed: {String(item.passed)}
                  </span>
                </header>
                <dl className="domain-fields">
                  <div><dt>Desirability</dt><dd>{returnedValue(item.desirability)}</dd></div>
                  <div><dt>Pareto front</dt><dd>{String(item.pareto_front)}</dd></div>
                  <div><dt>Reason</dt><dd>{item.reason}</dd></div>
                  <div><dt>Top margin metric</dt><dd>{returnedValue(item.top_margin_metric)}</dd></div>
                </dl>
              </article>
            ))}
          </div>
        )}
      </div>

      <aside className="shortlist-limitations" aria-label="Exploration limitations">
        <h3>Calibration</h3>
        <dl className="domain-fields">
          <div><dt>Calibrated</dt><dd>{presentation.calibration.calibrated}</dd></div>
          <div><dt>Provisional</dt><dd>{presentation.calibration.provisional}</dd></div>
          <div><dt>Unavailable</dt><dd>{presentation.calibration.unavailable}</dd></div>
        </dl>
        <h3>Source Evidence</h3>
        <ul>
          {presentation.sourceEvents.map(({ eventId, evidence: source }) => (
            <li key={eventId}>
              {source ? (
                <button type="button" onClick={() => onSelectEvidence(eventId)}>
                  <code>{eventId}</code>{" "}
                  <span>{source.event_type ?? "event available"}</span>
                </button>
              ) : (
                <span><code>{eventId}</code>{" "}unavailable in this response</span>
              )}
            </li>
          ))}
        </ul>
        <h3>Unmapped metrics</h3>
        {presentation.unmappedMetrics.length === 0 ? (
          <p>None reported.</p>
        ) : (
          <ul>{presentation.unmappedMetrics.map((metric) => <li key={metric}>{metric}</li>)}</ul>
        )}
      </aside>
    </section>
  );
}
