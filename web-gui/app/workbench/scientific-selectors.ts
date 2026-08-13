import type {
  ArtifactView,
  CandidateView,
  EvidenceView,
  ExplorationShortlistEvidence,
  MetricValue,
  TraceLink,
} from "./domain";

export function candidateEvidence(
  candidateId: string,
  evidence: EvidenceView[],
): EvidenceView[] {
  return evidence.filter((item) => item.trace.candidate_id === candidateId);
}

export function candidateArtifacts(
  candidateId: string,
  artifacts: ArtifactView[],
): ArtifactView[] {
  return artifacts.filter((item) => item.trace.candidate_id === candidateId);
}

export function isStructureBearingArtifact(artifact: ArtifactView): boolean {
  if (artifact.artifact_type === "structure" || artifact.artifact_type === "design_pdb") {
    return true;
  }
  if (!artifact.artifact_type?.startsWith("prediction_input:")) return false;
  return artifact.role?.endsWith(".pdb") === true
    || artifact.artifact_type === "prediction_input:global.post_relax_pdb"
    || artifact.artifact_type === "prediction_input:global.design_reference_pdb";
}

export function candidateById(
  candidates: CandidateView[],
  candidateId: string | null,
): CandidateView | null {
  return candidates.find((item) => item.candidate_id === candidateId) ?? null;
}

export function metricEntries(
  candidate: CandidateView,
): Array<[string, MetricValue]> {
  return Object.entries(candidate.metrics ?? {});
}

export function traceEntries(trace: TraceLink): Array<[string, string]> {
  return Object.entries(trace).filter(
    (entry): entry is [string, string] => typeof entry[1] === "string",
  );
}

export interface SourceEventReference {
  eventId: string;
  evidence: EvidenceView | null;
}

export interface ExplorationShortlistPresentation {
  passedSummary: string;
  shortlist: ExplorationShortlistEvidence["shortlist"];
  calibration: ExplorationShortlistEvidence["calibration"];
  sourceEvents: SourceEventReference[];
  unmappedMetrics: string[];
}

export function explorationShortlistPresentation(
  shortlist: ExplorationShortlistEvidence,
  evidence: EvidenceView[],
): ExplorationShortlistPresentation {
  return {
    passedSummary: `${shortlist.n_passed} / ${shortlist.n_evaluated} passed`,
    shortlist: shortlist.shortlist,
    calibration: shortlist.calibration,
    sourceEvents: shortlist.source_event_ids.map((eventId) => ({
      eventId,
      evidence:
        evidence.find((item) => item.event_id === eventId) ?? null,
    })),
    unmappedMetrics: shortlist.unmapped_metrics,
  };
}

export type ArtifactContentState =
  | { available: true; contentLink: string }
  | { available: false; contentLink: null };

export function artifactContentState(
  artifact: ArtifactView | null,
): ArtifactContentState {
  return artifact?.content_link
    ? { available: true, contentLink: artifact.content_link }
    : { available: false, contentLink: null };
}
