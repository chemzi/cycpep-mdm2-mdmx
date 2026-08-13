## MODIFIED Requirements

### Requirement: Prediction plans use a benchmark-backed GPU-slot estimate
The Planner SHALL estimate `evaluate_new_design_candidates` in GPU-slot wall minutes for the exact Candidate count, using a conservative integer per-Candidate configuration value calibrated above complete production-protocol observations. The estimate SHALL cover the same claim-through-task-closure interval measured by actual completion accounting.

#### Scenario: Two-Candidate production Prediction plan
- **WHEN** an initial Prediction bootstrap plan contains two committed Candidates
- **THEN** its task carries an `estimated_gpu_minutes` value of 30 GPU-slot wall minutes
- **AND** that estimate leaves operational headroom above the observed 22.338975-minute production n=2 maximum
- **AND** the estimate status is `estimated`

#### Scenario: Calibrated budget semantics remain in immutable plan identity
- **WHEN** the configured Prediction estimate differs for the same formal Design source
- **THEN** the bootstrap plan identity differs
- **AND** the older plan and approval remain unchanged

#### Scenario: Other GPU action estimates remain owned by their existing policy
- **WHEN** Planner estimates a GPU action other than `evaluate_new_design_candidates`
- **THEN** its existing proposal/candidate estimate behavior remains unchanged
