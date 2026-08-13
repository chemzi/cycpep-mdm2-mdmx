export function buildC0006ProductionEnvelope(baseEnvelope) {
  const envelope = structuredClone(baseEnvelope);
  const data = envelope.data;

  data.candidates = {
    scope: "project",
    total: 14,
    returned: 14,
    truncated: false,
    items: Array.from({ length: 14 }, (_, index) => {
      const number = index + 1;
      const candidateId = `C${String(number).padStart(4, "0")}`;
      if (candidateId !== "C0006") {
        return {
          candidate_id: candidateId,
          sequence: `SEQ${number}`,
          status: "proposed",
          metrics: {},
          trace: { project_id: "project-1", candidate_id: candidateId },
          run_relation: "unlinked",
        };
      }
      return {
        candidate_id: "C0006",
        sequence: "GSLALESLAG",
        source_route: "route_A_mdm2",
        status: "needs_optimization",
        final_status: "needs_optimization",
        metrics: {
          L2_ipsae_mdm2: 0.58,
          L7_post_relax_interface_energy: -18.4,
        },
        trace: { project_id: "project-1", candidate_id: "C0006" },
        run_relation: "unlinked",
        associations: {
          evidence_total: 10,
          artifact_total: 8,
          artifact_ids: [
            "artifact-c0006-record",
            "artifact-c0006-post-relax",
            "artifact-c0006-monomer-pdb",
            "artifact-c0006-monomer-pae",
            "artifact-c0006-complex-pdb",
            "artifact-c0006-complex-pae",
            "artifact-c0006-rosetta",
            "artifact-c0006-boltz",
          ],
          complete: true,
          limitations: [],
          status_owner: {
            run_id: "run-1",
            run_relation: "current_run",
          },
          structures: [{
            artifact_id: "artifact-c0006-post-relax",
            artifact_type: "prediction_input:global.post_relax_pdb",
            role: "global.post_relax.pdb",
          }],
          shortlist: [{
            event_id: "evt-shortlist-c0006",
            candidate_id: "C0006",
            passed: false,
            desirability: 0.42,
            pareto_front: true,
            reason: "retained_for_round_2",
            top_margin_metric: "L2_ipsae_mdm2",
          }],
        },
      };
    }),
  };

  data.evidence = {
    scope: "project",
    total: 112,
    returned: 100,
    truncated: true,
    items: Array.from({ length: 100 }, (_, index) => ({
      event_id: `evt-returned-${index + 1}`,
      event_type: "metric_battery",
      trace: { project_id: "project-1", candidate_id: "C0001" },
      run_relation: "current_run",
    })),
  };
  data.artifacts = {
    scope: "project",
    total: 108,
    returned: 100,
    truncated: true,
    items: Array.from({ length: 100 }, (_, index) => ({
      artifact_id: `artifact-returned-${index + 1}`,
      artifact_type: "prediction_record",
      trace: {
        project_id: "project-1",
        candidate_id: "C0001",
        artifact_id: `artifact-returned-${index + 1}`,
      },
      run_relation: "current_run",
    })),
  };
  return envelope;
}
