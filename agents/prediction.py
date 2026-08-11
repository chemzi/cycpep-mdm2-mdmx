"""Prediction Agent: production artifact ingestion and seven-layer evaluation.

Examples
--------
Run a new batch::

    python agents/prediction.py run \
      --artifacts-root /root/damodel-tmp/novapeptide/prediction_artifacts

Resume the same immutable run after an interruption::

    python agents/prediction.py run --run-id prediction_... --resume \
      --artifacts-root /root/damodel-tmp/novapeptide/prediction_artifacts

The command never creates demo candidates, placeholder scores, or fallback
thresholds.  Missing artifacts/thresholds become explicit pending records.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_layer import CandidateIndex, EvidenceLogger, State, get_storage_backend  # noqa: E402
from prediction_pipeline import PredictionConfig, PredictionPipeline  # noqa: E402
from prediction_pipeline.contracts import ContractError  # noqa: E402
from agents.prediction_contract import (  # noqa: E402
    PredictionCorrelation,
    PredictionInvocationInputs,
    PredictionInvocationRecovery,
    start_receipt_payload,
    validate_prediction_invocation as _validate_prediction_invocation,
)


def resolve_prediction_run_root(
    run_root: str | Path | None = None,
) -> Path:
    """Resolve Prediction's run root without creating or inspecting runs."""
    if run_root is not None:
        return Path(run_root).expanduser().resolve()
    explicit = os.environ.get("CYCPEP_PREDICTION_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    data_root = os.environ.get("NP_DATA")
    if data_root:
        return (Path(data_root).expanduser() / "prediction_runs").resolve()
    return (ROOT / "data" / "prediction_runs").resolve()


def _default_run_root() -> Path:
    """Compatibility helper for the existing CLI default."""
    return resolve_prediction_run_root()


def _default_artifacts_root() -> Path:
    explicit = os.environ.get("CYCPEP_PREDICTION_ARTIFACTS")
    if explicit:
        return Path(explicit)
    data_root = os.environ.get("NP_DATA")
    if data_root:
        return Path(data_root) / "prediction_artifacts"
    return ROOT / "data" / "prediction_artifacts"


def run(
    state: dict | None = None,
    *,
    artifacts_root: str | Path | None = None,
    run_root: str | Path | None = None,
    config: PredictionConfig | None = None,
    candidate_ids: list[str] | None = None,
    run_id: str | None = None,
    resume: bool = False,
    project_config: dict | None = None,
    effects_output: str | Path | None = None,
    transaction_id: str | None = None,
    correlation: PredictionCorrelation | None = None,
) -> dict:
    """Run Prediction for existing Design candidates.

    ``state`` remains accepted for compatibility with the agent scheduler.  A
    supplied state object is read-only here.  When ``effects_output`` is set,
    all formal mutations are returned as proposals for Execution to commit;
    otherwise the compatibility path uses the existing atomic helpers.

    ``project_config`` optionally injects an explicit approved project config
    (PR5, Engineering Standard §7) and must agree with ``state``'s
    ``project_id``.  When omitted, the project is resolved from ``state`` or
    the lazy environment default.  The override lasts only for this call.
    """
    current_state = state if state is not None else State.load()
    if project_config is None:
        project = current_state.get("project_config") or State._project_config
    else:
        state_project_id = str(current_state.get("project_id") or "").strip()
        injected_project_id = str(project_config.get("project_id") or "").strip()
        if state_project_id and injected_project_id and state_project_id != injected_project_id:
            raise ContractError(
                "prediction_project_mismatch",
                "injected project config differs from State project ID",
            )
        project = project_config
    thresholds = current_state.get("thresholds") or {}
    if effects_output is not None and not transaction_id:
        raise ContractError(
            "prediction_transaction_missing",
            "transaction_id is required when emitting Prediction effects",
        )
    if correlation is not None:
        approved_binding = str((project.get("review") or {}).get("approved_digest") or "")
        if (
            correlation.project_id != str(project.get("project_id") or "")
            or correlation.approved_content_binding != approved_binding
            or run_id != correlation.prediction_run_id
        ):
            raise ContractError(
                "prediction_correlation_mismatch",
                "Prediction correlation does not match project approval or run identity",
            )
        if run_root is None:
            raise ContractError(
                "prediction_locator_missing",
                "Launcher-correlated Prediction requires an explicit resolved run root",
            )
    pipeline = PredictionPipeline(
        candidate_rows=CandidateIndex.load(),
        project=project,
        thresholds=thresholds,
        artifacts_root=artifacts_root or _default_artifacts_root(),
        run_root=resolve_prediction_run_root(run_root),
        config=config,
        candidate_ids=candidate_ids,
        run_id=run_id,
        resume=resume,
        defer_formal_writes=effects_output is not None,
        artifact_id_prefix=transaction_id,
        launcher_correlation=(correlation.receipt_fields() if correlation else None),
    )
    if correlation is not None:
        inputs = PredictionInvocationInputs.from_pipeline(pipeline)
        existing = _validate_prediction_invocation(
            correlation,
            store=get_storage_backend(),
            expected_inputs=inputs,
        )
        if existing.status != "not_started":
            raise ContractError(
                existing.blocker_code or "prediction_recovery_ambiguous",
                "Prediction invocation has already started; validate recovery before retrying",
            )
        EvidenceLogger.log(
            "prediction",
            "prediction_invocation_started",
            start_receipt_payload(
                correlation,
                run_root=pipeline.run_root,
                inputs=inputs,
                expected_run_manifest=pipeline.run_manifest(),
            ),
            targets=list(pipeline.required_targets),
            phase="evaluate",
        )
    summary = pipeline.run()
    if effects_output is not None:
        destination = Path(effects_output).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(
                pipeline.transaction_effects(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    return summary


def validate_prediction_invocation(
    correlation: PredictionCorrelation,
    *,
    store=None,
    expected_inputs: PredictionInvocationInputs | None = None,
) -> PredictionInvocationRecovery:
    """Read and validate Prediction formal state for one Launcher invocation."""
    return _validate_prediction_invocation(
        correlation,
        store=store or get_storage_backend(),
        expected_inputs=expected_inputs,
    )


def _load_config(path: str | None) -> PredictionConfig:
    if not path:
        return PredictionConfig()
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ContractError("config_type", "Prediction config JSON must be an object")
    return PredictionConfig.from_dict(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("run", help="ingest artifacts and evaluate candidates")
    command.add_argument(
        "--artifacts-root", default=str(_default_artifacts_root()),
        help="root containing <candidate_id>/artifacts.json",
    )
    command.add_argument(
        "--run-root", default=str(_default_run_root()),
        help="versioned Prediction run root",
    )
    command.add_argument("--config", help="Prediction method-parameter JSON")
    command.add_argument("--candidate", action="append", dest="candidates")
    command.add_argument("--run-id")
    command.add_argument("--resume", action="store_true")
    command.add_argument(
        "--effects-output",
        help="write staged mutation/evidence proposals instead of formal writes",
    )
    command.add_argument("--transaction-id")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "run":
            summary = run(
                artifacts_root=args.artifacts_root,
                run_root=args.run_root,
                config=_load_config(args.config),
                candidate_ids=args.candidates,
                run_id=args.run_id,
                resume=args.resume,
                effects_output=args.effects_output,
                transaction_id=args.transaction_id,
            )
        else:
            raise AssertionError(args.command)
    except (ContractError, json.JSONDecodeError, OSError) as exc:
        code = getattr(exc, "code", exc.__class__.__name__)
        print(json.dumps(
            {"status": "error", "code": code, "message": str(exc)},
            ensure_ascii=False,
        ))
        return 2
    print(json.dumps({"status": "complete", "summary": summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
