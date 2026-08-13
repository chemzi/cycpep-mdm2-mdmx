## Context

The real `design.n=2` run committed bootstrap Prediction transaction `tx-89fd...`. The original run held `inputs/thresholds.json`, but transaction promotion registered only the handoff and record/inventory artifacts. The formal handoff Evidence therefore points into the committed artifact root where no adjacent threshold snapshot exists. See proposal.md and the new capability spec.

## Goals / Non-Goals

**Goals:**

- Make the Prediction transaction the owner of an immutable, committed threshold snapshot locator.
- Validate the locator while forming bootstrap owner readiness and pass it explicitly to E3 publication.
- Preserve atomicity and exact digest binding.

**Non-Goals:**

- Do not read attempt staging after commit, reconstruct thresholds from State, or backfill the failed run.
- Do not change threshold values, scientific protocol, Prediction readiness policy, retry, budgeting, Store schema, Planner policy, or E3 Decision semantics.
- Do not modify direct Prediction storage layout.

## Decisions

1. **Add a dedicated `prediction_thresholds` additional Artifact, not an output role.** `evaluate_new_design_candidates` retains its sole `prediction_handoff` semantic output. The effects contract declares the exact `run_dir/inputs/thresholds.json`; the transaction adapter stages it using deterministic ID `${transaction_id}-prediction-thresholds`, and the Worker commits it atomically beside the handoff. This follows the existing record/input additional-artifact seam and does not expand the Planner→Execution output contract.

2. **Keep locator ownership singular and the two digests distinct.** The internal strict Prediction effects contract gains one `thresholds_artifact` proposal. The adapter stages it as an additional Artifact, and `prediction_handoff_ready` carries only `thresholds_artifact_id` plus its existing `thresholds_digest`. The Store Artifact row is the sole owner of committed path and file-byte SHA-256. `thresholds_digest` remains the canonical JSON scientific authority and is never compared directly with the file SHA-256. CommitManager moves both files and registers Artifacts plus Evidence in its existing atomic commit/recovery boundary; no committed path is predicted before commit and no Evidence is patched afterward.

3. **Validate at the bootstrap owner boundary.** Bootstrap inspection selects the formal handoff event by its explicit `handoff_artifact_id`, resolves the event's `thresholds_artifact_id` through typed Store lookup, and proves transaction membership, producer task, artifact type, Store byte SHA-256 against the committed file, and parsed canonical digest against both handoff and Evidence. The completed `FormalBoundary` carries named handoff Artifact identity plus `thresholds_path`, `thresholds_sha256`, and `thresholds_artifact_id`. It never relies on `artifact_ids[0]` or a transactional `handoff_path` Evidence field.

4. **Make publication accept the explicit owner locator.** Direct Prediction may continue using its handoff-adjacent snapshot; bootstrap completion must provide the committed locator. Publication never searches attempt directories and never uses current State thresholds.

Alternative rejected: copy thresholds next to the promoted handoff after commit. That creates an untracked post-commit mutation outside artifact ownership. Alternative rejected: derive the attempt run path from receipt metadata. That makes staging authoritative after commit and breaks cleanup/recovery semantics.

## Risks / Trade-offs

- [Formal Evidence format gains one optional bootstrap field] → keep direct/legacy completion compatible; require `thresholds_artifact_id` only for the bootstrap transaction path.
- [The handoff and threshold Artifacts must agree on the threshold digest] → validate before commit and again at owner readiness.
- [Promotion failure could otherwise leave partial state] → both artifacts remain in the existing single Execution transaction.

## Migration Plan

Merge the narrow contract change and deploy a fresh `design.n=2` run. Do not resume or mutate the failed invocation; rollback reverts the new additional-Artifact binding and leaves all old Stores untouched.
