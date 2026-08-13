## MODIFIED Requirements

### Requirement: Frozen Decision input is non-authoritative and non-operative in E3-A
Planner SHALL place the canonical Decision only in its private local State copy for the duration of plan construction and MUST NOT persist it through State or Evidence. In the combined E3-A/E3-B contract, Planner SHALL apply that explicitly bound Decision only to the peptide lengths of existing iterate-design jobs according to `workflow/exploration-decision-design-materialization`; it MUST NOT alter task selection, proposal counts, target allocation, routes, seeds, approvals, orchestration, execution, protocol, thresholds, or project configuration.

Planner MUST treat `_frozen_exploration_decision` as an invocation-owned reserved key: any caller-supplied value under that key MUST be removed from the local copy before an explicitly supplied and validated Decision may be injected. When no explicit Decision is supplied, Planner MUST use approved configured lengths or the deterministic static fallback and MUST NOT consult or record ambient experience.

#### Scenario: Decision changes only iterate-design lengths
- **WHEN** two otherwise identical plans are built with different valid Decisions whose adjustments select different approved peptide lengths
- **THEN** their iterate-design lengths reflect the respective Decisions while task selection, budget requests, approval requests, execution policy, proposal counts, target allocation, routes, seeds, protocol, thresholds, and project configuration remain identical

#### Scenario: Plan construction performs no formal persistence
- **WHEN** Planner builds a plan with a valid explicit Decision
- **THEN** no State update and no Evidence append occurs

#### Scenario: Missing configured lengths use the deterministic approved envelope
- **WHEN** Planner builds with an explicit Decision and a required target has no configured design lengths
- **THEN** task construction starts from the static approved fallback `[8, 10, 12]` and applies only a valid Decision narrowing without consulting or recording ambient experience

#### Scenario: Decision absence never restores ambient adaptation
- **WHEN** Planner builds without an explicit Decision and a required target has no configured design lengths
- **THEN** task construction uses the static fallback `[8, 10, 12]` without consulting or recording ambient experience

#### Scenario: Ambient State cannot impersonate the explicit Decision path
- **WHEN** caller State contains a `_frozen_exploration_decision` value but no explicit Decision argument is supplied
- **THEN** Planner removes the ambient value from its local copy and uses the deterministic no-Decision length policy
