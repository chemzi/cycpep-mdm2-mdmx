## Why

The repository has a correctly named root `AGENTS.md`, but it is not tracked, and its required long-term remediation document does not exist. Without durable, linked governance context, future remediation work can revert to stale phase plans, restart broad audits, or create competing task trackers.

## What Changes

- Review the existing root `AGENTS.md` and formally add it as the repository-level development workflow and remediation entrypoint.
- Narrow its anti-overdefense guidance so it rejects unnecessary new hash checks without weakening hash or SHA256 behavior already required by protocol, artifact, or integrity contracts.
- Add `docs/engineering/remediation-strategy.md` as the durable record of audit-derived governance direction, prioritization principles, high-risk boundaries, and rules for selecting the next OpenSpec change.
- State that the strategy is not a task list or progress tracker; each change's scope, requirements, design, tasks, and progress remain exclusively in that change's OpenSpec artifacts.
- Keep the change documentation-only: no production code, public interface, CLI, business behavior, data format, dependency, or migration changes.
- Leave README status wording, PR3/PR4 claims, Web GUI documentation, and all other documentation drift unchanged for a later `documentation-reality-alignment` change.
- Leave all production legacy paths and bypasses unchanged; this change only records how later changes are selected and governed.

## Capabilities

### New Capabilities

- `engineering/remediation-governance`: Durable repository instructions and a long-term remediation decision framework that routes concrete work into independent OpenSpec changes.

### Modified Capabilities

None.

## Impact

- Affected files are limited to root `AGENTS.md`, `docs/engineering/remediation-strategy.md`, and this change's OpenSpec artifacts.
- Production modules, tests of business behavior, public APIs, CLI behavior, persistence, transactions, and scientific protocols are unaffected.
- No dependency or data migration is required.
