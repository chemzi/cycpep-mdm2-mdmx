## 1. Confirm the Governance Baseline

- [x] 1.1 Review the existing root `AGENTS.md` against `ENGINEERING_STANDARD.md` and the approved remediation-governance spec, preserving its concise workflow and routing role.
- [x] 1.2 Reconcile the prior repository audit with current focused evidence sources, separating durable architectural direction from stale status claims and deferred documentation drift without restarting a full-repository audit.

## 2. Persist the Governance Context

- [x] 2.1 Formally add the correctly named root `AGENTS.md` with OpenSpec as the per-change source of truth and the remediation strategy as the long-term direction source.
- [x] 2.2 Create `docs/engineering/remediation-strategy.md` covering evidence sources, durable architecture direction, prioritization principles, high-risk boundaries, next-change selection rules, and explicit non-goals.
- [x] 2.3 Ensure the strategy contains no task list, implementation checklist, progress table, ownership schedule, or authoritative per-change status, and explicitly routes concrete work to independent OpenSpec changes.
- [x] 2.4 Leave README, PR3/PR4 status wording, Web GUI documentation, all other documentation drift, and every production file unchanged.
- [x] 2.5 Narrow the `AGENTS.md` anti-overdefense rule so unnecessary new hash checks are prohibited while existing protocol, artifact, and integrity contract requirements remain mandatory.

## 3. Verify the Documentation-Only Change

- [x] 3.1 Confirm the implementation diff is limited to `AGENTS.md`, `docs/engineering/remediation-strategy.md`, and this change's OpenSpec artifacts.
- [x] 3.2 Review links and cited repository evidence, and confirm the strategy distinguishes verified durable direction from deferred or uncertain documentation claims.
- [x] 3.3 Run strict OpenSpec validation and the Architecture Gate; run runtime regression tests only if an unexpected runtime file enters the diff.
- [x] 3.4 Run OpenSpec implementation verification and code review, confirming the governance documents and artifacts agree before requesting archive approval.
- [x] 3.5 Re-run strict validation and implementation/standards verification after the governance-rule consistency correction, then confirm all requirements, scenarios, and tasks pass.
