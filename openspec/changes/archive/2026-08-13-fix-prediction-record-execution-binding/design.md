## Context

The transaction-mode Prediction persistence adapter already owns the domain `run_id`. Its generic `record_event()` converts a payload key named `run_id` into `prediction_run_id` before effects leave Prediction, deliberately separating Prediction identity from the Worker-injected formal Orchestrator `run_id`. `record_battery_evaluated()` and `record_handoff_ready()` provide that key, but `record_scoring_events()` and `record_invalid_event()` currently create `prediction_recorded` payloads without it. Worker formalization correctly adds the formal trace and does not invent domain fields. The exact Launcher publication proof therefore rejects the otherwise committed event set.

## Goals / Non-Goals

**Goals:**

- Make the Prediction-owned writer emit one consistent domain run identity for all authoritative publication events.
- Test the actual persistence writer through Worker formalization and the production publication validator, rather than hand-constructing already-correct events.
- Preserve the append-only transaction and exact publication proof.

**Non-Goals:**

- No Launcher validation relaxation or reader-side compatibility inference.
- No Store schema, event-envelope, transaction, retry, readiness, scientific protocol, Planner, or Critic change.
- No rewrite of historical committed Evidence or automatic retry of the blocked invocation.

## Decisions

1. Add the adapter's existing Prediction `run_id` to the `prediction_recorded` payload at both normal-scoring and invalid-record writer paths. The existing `record_event()` normalization then produces `prediction_run_id`, using the same seam as battery and handoff events. This avoids duplicating identity translation or touching shared Worker logic.
2. Add a transactional regression that begins with the real Prediction persistence adapter, commits through Worker formalization, and passes the resulting Store events to the existing publication proof. Existing hand-built tamper tests remain responsible for individual fail-closed mutations.
3. Treat the added field as completion of an existing data contract, not a schema migration: the field and meaning already exist on the other required Prediction events, while legacy rows remain readable and immutable.

## Risks / Trade-offs

- [A shallow writer-only assertion could miss Worker envelope loss] → The merge-blocking regression inspects committed Store events and invokes the real publication validator.
- [Adding the field only to successful records would leave typed invalid records inconsistent] → Cover both normal and invalid writer paths in focused tests.
- [A reader-side fallback could make unrelated or historical events authoritative] → Keep Launcher and Store readers unchanged and fail closed on missing identity.

## Migration Plan

Deploy the writer change in a new integration commit and start a fresh n=2 Launcher run. Do not resume or mutate the blocked invocation. Rollback is a code revert; no data migration is required.
