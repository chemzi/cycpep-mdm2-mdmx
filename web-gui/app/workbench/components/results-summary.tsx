import type {
  ResultsDigest,
  ResultsFinalist,
  ResultsLayerView,
} from "../results-client";

export interface ResultsSummaryProps {
  digest: ResultsDigest | null;
  error?: string | null;
  refreshing?: boolean;
}

function percent(value: number | null): string {
  return value === null ? "n/a" : `${(value * 100).toFixed(0)}%`;
}

function valueOrUnavailable(value: string | null | undefined): string {
  return value ? String(value) : "Unavailable";
}

function OverviewCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "pass" | "warn" | "muted";
}) {
  return (
    <div className={`results-card is-${tone ?? "muted"}`}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function FinalistsTable({ finalists }: { finalists: ResultsFinalist[] }) {
  if (finalists.length === 0) {
    return (
      <div className="results-panel">
        <header><span className="domain-kicker">HARD CLEARANCE</span><h3>Finalists</h3></header>
        <p className="domain-empty">No evaluated candidates yet.</p>
      </div>
    );
  }
  return (
    <div className="results-panel">
      <header>
        <span className="domain-kicker">HARD CLEARANCE</span>
        <h3>Finalists</h3>
        <p>Ranked by hard clearance first, then exploration desirability.</p>
      </header>
      <table className="results-table">
        <thead>
          <tr>
            <th scope="col">Rank</th>
            <th scope="col">Candidate</th>
            <th scope="col">Hard cleared</th>
            <th scope="col">Desirability</th>
            <th scope="col">Failed layers</th>
            <th scope="col">Pareto</th>
          </tr>
        </thead>
        <tbody>
          {finalists.map((finalist) => (
            <tr key={finalist.candidate_id}>
              <td>{finalist.rank ?? "?"}</td>
              <td>
                <b>{finalist.candidate_id}</b>
                <small>{finalist.sequence ?? "sequence unavailable"}</small>
              </td>
              <td><span className={finalist.hard_cleared ? "is-passed" : "is-exploratory"}>
                {String(finalist.hard_cleared)}
              </span></td>
              <td>{finalist.desirability === null ? "n/a" : finalist.desirability.toFixed(3)}</td>
              <td>{finalist.failed_layers.length === 0 ? "?" : finalist.failed_layers.join(", ")}</td>
              <td>{String(finalist.pareto_front)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function thresholdLabel(threshold: ResultsLayerView["threshold"]): string {
  if (!threshold) return "Unavailable";
  if (threshold.value === null || threshold.operator === null) {
    return `${threshold.calibration_status} ? unset`;
  }
  return `${threshold.operator} ${threshold.value} (${threshold.calibration_status})`;
}

function LayerStatsTable({ layers }: { layers: ResultsLayerView[] }) {
  return (
    <div className="results-panel">
      <header>
        <span className="domain-kicker">LAYER STATISTICS</span>
        <h3>Battery layers</h3>
        <p>Evaluated rows with finite values; pass honors per-target battery results.</p>
      </header>
      <table className="results-table">
        <thead>
          <tr>
            <th scope="col">Layer</th>
            <th scope="col">Metric</th>
            <th scope="col">Direction</th>
            <th scope="col">Evaluated</th>
            <th scope="col">Passed</th>
            <th scope="col">Pass rate</th>
            <th scope="col">Threshold</th>
          </tr>
        </thead>
        <tbody>
          {layers.map((layer) => (
            <tr key={layer.key}>
              <td><code>{layer.key}</code></td>
              <td>{layer.metric}</td>
              <td>{layer.direction}</td>
              <td>{layer.evaluated}</td>
              <td>{layer.passed}</td>
              <td>{percent(layer.pass_rate)}</td>
              <td>{thresholdLabel(layer.threshold)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ResultsSummary({ digest, error = null, refreshing = false }: ResultsSummaryProps) {
  if (!digest) {
    return (
      <section className="results-digest" aria-labelledby="results-digest-heading">
        <div className="domain-section-header">
          <div>
            <span className="domain-kicker">RESULTS READ MODEL</span>
            <h2 id="results-digest-heading">Results digest</h2>
          </div>
        </div>
        <p className="workbench-state failure" role="alert">
          Results digest unavailable. {error ?? "The results read model did not respond."}
        </p>
      </section>
    );
  }
  const summary = digest.summary;
  const dataBasisLabel =
    summary.data_basis === "demo_fixture"
      ? "Demo fixture (synthetic)"
      : summary.data_basis === "real"
        ? "Real run data"
        : "No data yet";
  return (
    <section className="results-digest" aria-labelledby="results-digest-heading">
      <div className="domain-section-header">
        <div>
          <span className="domain-kicker">RESULTS READ MODEL</span>
          <h2 id="results-digest-heading">Results digest</h2>
        </div>
        <div className="results-header-meta">
          <span className={`data-basis is-${summary.data_basis}`}>{dataBasisLabel}</span>
          {refreshing ? <span className="results-refreshing">refreshing?</span> : null}
        </div>
      </div>

      <dl className="results-overview" role="list" aria-label="Results overview">
        <OverviewCard label="Candidates total" value={String(summary.candidates_total)} />
        <OverviewCard label="Evaluated" value={String(summary.candidates_evaluated)} />
        <OverviewCard label="Pending prediction" value={String(summary.candidates_pending_prediction)} />
        <OverviewCard label="Hard cleared" value={String(summary.hard_cleared)} tone="pass" />
        <OverviewCard
          label="Hard clearance rate"
          value={percent(summary.hard_clearance_rate)}
          tone={summary.hard_clearance_rate === null || summary.hard_clearance_rate > 0 ? "pass" : "warn"}
        />
        <OverviewCard label="Shortlisted" value={String(summary.n_shortlisted)} />
        <OverviewCard label="Pareto front" value={String(summary.n_pareto_front)} />
        <OverviewCard label="Layers evaluated" value={`${summary.layers_evaluated} / ${summary.layers_total}`} />
      </dl>

      <div className="results-thresholds" aria-label="Threshold calibration counts">
        <span className="domain-kicker">CALIBRATION</span>
        <dl className="domain-fields">
          <div><dt>Calibrated</dt><dd>{summary.counts.calibrated}</dd></div>
          <div><dt>Provisional</dt><dd>{summary.counts.provisional}</dd></div>
          <div><dt>Unavailable</dt><dd>{summary.counts.unavailable}</dd></div>
        </dl>
      </div>

      <div className="results-conclusion">
        <h3>Conclusion</h3>
        <p>{digest.conclusion}</p>
      </div>

      <div className="results-grid">
        <FinalistsTable finalists={digest.finalists} />
        <LayerStatsTable layers={digest.layers} />
      </div>

      {digest.pending_candidates.length > 0 ? (
        <div className="results-pending">
          <span className="domain-kicker">DESIGNED, AWAITING PREDICTION</span>
          <ul>
            {digest.pending_candidates.map((candidate) => (
              <li key={candidate.candidate_id}>
                <code>{candidate.candidate_id}</code>
                <span>{candidate.status ?? "status unavailable"}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
