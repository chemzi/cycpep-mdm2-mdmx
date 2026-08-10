"""Default adapters from Launcher coordination to existing public seams."""

from __future__ import annotations

from pathlib import Path

from core.context import ProjectContext

from .boundaries import FormalBoundaryInspector


class DefaultWorkflowRuntime:
    """One project-bound view over the existing workflow authorities."""

    def __init__(self, context: ProjectContext, launcher_run_id: str):
        from agents import orchestrator, research
        from agents.design import Design, DesignContext
        from agents.design.initial import (
            InitialDesignCorrelation,
            validate_initial_invocation,
        )
        from agents.prediction import validate_prediction_invocation
        from agents.prediction_contract import PredictionCorrelation
        from data_layer import get_storage_backend

        self.context = context
        self.launcher_run_id = launcher_run_id
        self.store = get_storage_backend()
        self.research = research
        binding = _approved_binding(context)
        payload = launcher_run_id.removeprefix("launcher_")
        self.research_correlation = research.ResearchCorrelation(
            research_invocation_id=f"research_{payload}",
            launcher_run_id=launcher_run_id,
            project_id=context.project_id,
            approved_content_binding=binding,
        )
        self.design_correlation = InitialDesignCorrelation.from_launcher(
            launcher_run_id=launcher_run_id,
            project_id=context.project_id,
            approved_content_binding=binding,
        )
        paths = context.resolve_paths()
        output = paths.output_dir or (Path(paths.data_dir) / "design_outputs")
        self.design = Design(DesignContext(dict(context.config), str(output)))
        self.prediction_correlation = PredictionCorrelation.for_launcher(
            launcher_run_id=launcher_run_id,
            project_id=context.project_id,
            approved_content_binding=binding,
        )
        self.inspector = FormalBoundaryInspector(
            store=self.store,
            research_validator=research.validate_research_invocation,
            design_validator=validate_initial_invocation,
            prediction_validator=validate_prediction_invocation,
            orchestrator_status=orchestrator.status,
        )

    def inspect_research(self):
        return self.inspector.research(self.research_correlation)

    def run_research(self):
        return self.research.run_with_receipt(
            project_config=dict(self.context.config), correlation=self.research_correlation
        )

    def inspect_design(self):
        return self.inspector.design(self.design_correlation)

    def run_design(self):
        return self.design.run_initial(self.design_correlation, store=self.store)

    @property
    def prediction_invocation_id(self):
        return self.prediction_correlation.prediction_invocation_id

    @property
    def prediction_run_id(self):
        return self.prediction_correlation.prediction_run_id

    def inspect_prediction(self):
        return self.inspector.prediction(self.prediction_correlation)

    def run_prediction(self, candidate_ids):
        from agents.prediction import resolve_prediction_run_root, run

        return run(
            project_config=dict(self.context.config),
            candidate_ids=list(candidate_ids),
            run_id=self.prediction_run_id,
            run_root=resolve_prediction_run_root(),
            correlation=self.prediction_correlation,
        )

    def inspect_critic(self, prediction):
        return self.inspector.critic(
            project_id=self.context.project_id,
            prediction_run_id=self.prediction_run_id,
        )

    def run_critic(self, handoff_path):
        from agents.critic import run

        return run(handoff_path=handoff_path, project_config=dict(self.context.config))

    def inspect_planner(self, critic):
        return self.inspector.planner(
            project_id=self.context.project_id,
            critic_report_id=critic.references["report_id"],
        )

    def run_planner(self, report_path):
        from agents.planner import run

        return run(critic_report_path=report_path, project_config=dict(self.context.config))

    def inspect_approvals(self, planner):
        return self.inspector.approvals(
            project_id=self.context.project_id,
            plan_id=planner.references["plan_id"],
            plan_sha256=planner.references["plan_sha256"],
        )

    def inspect_orchestrator(self, plan):
        return self.inspector.orchestrator_for_plan(
            project_id=self.context.project_id, plan_id=plan["plan_id"]
        )

    @staticmethod
    def initialize_orchestrator(plan_path, approvals):
        from agents.orchestrator import initialize

        return initialize(plan_path=plan_path, approval_paths=approvals)

    def inspect_execution_failure(self, orchestrator):
        return self.inspector.execution_failure(
            run_id=orchestrator.references["run_id"]
        )

    @staticmethod
    def recover_transactions():
        from execution import ensure_transaction_recovery_clean

        return ensure_transaction_recovery_clean()

    @staticmethod
    def drain(run_path):
        from execution.worker import drain_run

        return drain_run(run_path=run_path, worker_id="workflow-launcher")


def _approved_binding(context: ProjectContext) -> str:
    value = (context.config.get("review") or {}).get("approved_digest")
    if not isinstance(value, str) or not value:
        raise ValueError("approved project has no approved-content binding")
    return value


__all__ = ["DefaultWorkflowRuntime"]
