import type {
  ArtifactView,
  ProtocolView,
  TraceLink,
} from "../domain";
import { artifactContentState, traceEntries } from "../scientific-selectors";
import { StructureViewer } from "./structure-viewer";

export function TraceDetail({ trace }: { trace: TraceLink }) {
  const entries = traceEntries(trace);
  return (
    <dl className="trace-fields" aria-label="Trace linkage">
      {entries.length === 0 ? (
        <div><dt>Trace</dt><dd>Unavailable</dd></div>
      ) : entries.map(([name, value]) => (
        <div key={name}><dt>{name}</dt><dd><code>{value}</code></dd></div>
      ))}
    </dl>
  );
}

export function ProtocolDetail({ protocol }: { protocol?: ProtocolView }) {
  return (
    <dl className="domain-fields" aria-label="Protocol identity">
      <div><dt>Name</dt><dd>{protocol?.name ?? "Unavailable"}</dd></div>
      <div><dt>Version</dt><dd>{protocol?.version ?? "Unavailable"}</dd></div>
      <div><dt>Integrity identity</dt><dd><code>{protocol?.integrity_identity ?? "Unavailable"}</code></dd></div>
    </dl>
  );
}

export interface ArtifactTraceInspectorProps {
  artifacts: ArtifactView[];
  protocols: ProtocolView[];
  selectedArtifactId: string | null;
  onSelectArtifact: (artifactId: string) => void;
}

export function ArtifactTraceInspector({
  artifacts,
  protocols,
  selectedArtifactId,
  onSelectArtifact,
}: ArtifactTraceInspectorProps) {
  const selected =
    artifacts.find((artifact) => artifact.artifact_id === selectedArtifactId) ?? null;

  return (
    <section className="artifact-trace-inspector" aria-labelledby="artifact-heading">
      <header className="domain-section-header">
        <div>
          <span className="domain-kicker">FORMAL PROVENANCE</span>
          <h2 id="artifact-heading">Artifact / Protocol / Trace</h2>
        </div>
      </header>

      {artifacts.length === 0 ? (
        <p className="domain-empty">No artifact records returned.</p>
      ) : (
        <div className="artifact-inspector-grid">
          <ul aria-label="Artifacts" className="artifact-list">
            {artifacts.map((artifact, index) => {
              const artifactId = artifact.artifact_id;
              return <li key={artifactId ?? `artifact-${index}`}>
                <button
                  type="button"
                  disabled={!artifactId}
                  aria-pressed={artifactId === selected?.artifact_id}
                  onClick={() => artifactId && onSelectArtifact(artifactId)}
                >
                  <b>{artifactId ?? "Opaque identity unavailable"}</b>
                  <span>{artifact.artifact_type ?? "unknown type"} · {artifact.role ?? "unknown role"}</span>
                  <small>{artifact.run_relation.replaceAll("_", " ")}</small>
                </button>
              </li>;
            })}
          </ul>

          {!selected ? (
            <p className="domain-empty">Select an artifact to inspect formal provenance.</p>
          ) : (
            <article className="artifact-detail">
              <header><h3>{selected.artifact_id ?? "Artifact"}</h3></header>
              <dl className="domain-fields">
                <div><dt>Type</dt><dd>{selected.artifact_type ?? "Unavailable"}</dd></div>
                <div><dt>Role</dt><dd>{selected.role ?? "Unavailable"}</dd></div>
                <div><dt>Schema</dt><dd>{selected.schema_version ?? "Unavailable"}</dd></div>
                <div><dt>Size</dt><dd>{selected.size_bytes ?? "Unavailable"}</dd></div>
                <div><dt>Integrity identity</dt><dd><code>{selected.sha256 ?? "Unavailable"}</code></dd></div>
                <div><dt>Producer task</dt><dd><code>{selected.producer_task_id ?? "Unavailable"}</code></dd></div>
                <div><dt>Input artifacts</dt><dd>{selected.input_artifact_ids?.join(", ") || "None returned"}</dd></div>
              </dl>
              <h4>Protocol</h4>
              <ProtocolDetail protocol={selected.protocol} />
              <h4>Trace</h4>
              <TraceDetail trace={selected.trace} />
              <StructureViewer artifact={selected} />
            </article>
          )}
        </div>
      )}

      <section aria-labelledby="protocol-catalog-heading">
        <h3 id="protocol-catalog-heading">Returned protocol catalog</h3>
        {protocols.length === 0 ? (
          <p className="domain-empty">No protocols returned.</p>
        ) : (
          <div className="protocol-list">
            {protocols.map((protocol, index) => (
              <article key={`${protocol.name ?? "protocol"}-${protocol.version ?? index}`}>
                <ProtocolDetail protocol={protocol} />
              </article>
            ))}
          </div>
        )}
      </section>
    </section>
  );
}

export function ArtifactContentAvailability({ artifact }: { artifact: ArtifactView | null }) {
  const state = artifactContentState(artifact);
  return state.available
    ? <span>Browser-safe content link available</span>
    : <span>Content unavailable: no formal content_link returned</span>;
}
