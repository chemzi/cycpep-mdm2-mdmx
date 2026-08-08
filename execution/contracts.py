"""Typed, closed-world contracts shared by Planner, Orchestrator and Worker.

The task contract intentionally contains semantic actions rather than command
strings.  Only actions in ``CORE_ACTIONS`` may reach a production handler.
Docking and molecular-dynamics actions have stable names for v2 planning, but
remain non-executable until their scientific and runtime contracts are added.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Iterable

from prediction_pipeline.contracts import file_sha256, object_sha256
from prediction_pipeline.protocol import (
    PREDICTOR_PROTOCOLS,
    protocol_binding,
)
from contracts.action import (
    ACTION_CATALOG,
    ALL_ACTION_TYPES,
    EXECUTABLE_ACTION_TYPES,
    KNOWN_UNIMPLEMENTED_ACTION_TYPES,
    V2_RESERVED_ACTION_TYPES,
    get_action_spec,
)
from contracts.trace import TraceContext


EXECUTION_SCHEMA_VERSION = 1
DISPATCH_SCHEMA_VERSION = 2
LEGACY_DISPATCH_SCHEMA_VERSION = 1
EXECUTION_WORKER_VERSION = "1.0.1"

CORE_ACTIONS = frozenset(action.value for action in EXECUTABLE_ACTION_TYPES)
V2_RESERVED_ACTIONS = frozenset(action.value for action in V2_RESERVED_ACTION_TYPES)
KNOWN_UNIMPLEMENTED_ACTIONS = frozenset(
    action.value for action in KNOWN_UNIMPLEMENTED_ACTION_TYPES
)
ALL_KNOWN_ACTIONS = frozenset(action.value for action in ALL_ACTION_TYPES)

CANDIDATE_ID_RE = re.compile(r"^C[0-9]{4,}$")
TARGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
TASK_ID_RE = re.compile(r"^T[0-9]{3}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

ACTION_OUTPUT_ROLES = {
    action.value: spec.output_roles
    for action, spec in ACTION_CATALOG.items()
    if spec.output_roles
}

ACTION_DECLARED_OUTPUTS = {
    "iterate_design": ("design_task_result.json",),
    "evaluate_new_design_candidates": ("prediction_handoff.json",),
    "review_prediction_handoff": ("critic_report.json",),
    "propose_threshold_calibration": ("threshold_calibration_proposal.json",),
}


class ExecutionContractError(ValueError):
    """A task, dispatch packet or handler output cannot be executed safely."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _require_object(value: Any, code: str, label: str) -> dict:
    if not isinstance(value, dict):
        raise ExecutionContractError(code, f"{label} must be an object")
    return value


def _require_exact_keys(value: dict, allowed: set[str], code: str, label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ExecutionContractError(code, f"{label} has unsupported fields: {unknown}")


def _require_bool(value: Any, code: str, label: str) -> bool:
    if not isinstance(value, bool):
        raise ExecutionContractError(code, f"{label} must be boolean")
    return value


def _require_int(
    value: Any,
    code: str,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = 2**31 - 1,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutionContractError(code, f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise ExecutionContractError(
            code, f"{label} must be within [{minimum}, {maximum}]"
        )
    return value


def _require_strings(
    value: Any,
    code: str,
    label: str,
    *,
    pattern: re.Pattern[str] | None = None,
    allow_empty: bool = False,
) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ExecutionContractError(code, f"{label} must be a non-empty array")
    result = []
    for item in value:
        text = str(item or "").strip()
        if not text or (pattern is not None and not pattern.fullmatch(text)):
            raise ExecutionContractError(code, f"{label} contains invalid value {item!r}")
        result.append(text)
    if len(result) != len(set(result)):
        raise ExecutionContractError(code, f"{label} must not contain duplicates")
    return result


def _normalize_design_job(raw: Any, index: int) -> dict:
    value = _require_object(raw, "design_job_invalid", f"design_jobs[{index}]")
    allowed = {"route", "target_id", "lengths", "proposal_count", "seed"}
    _require_exact_keys(value, allowed, "design_job_invalid", f"design_jobs[{index}]")
    if set(value) != allowed:
        raise ExecutionContractError(
            "design_job_invalid", f"design_jobs[{index}] must contain {sorted(allowed)}"
        )
    route = str(value.get("route") or "").upper()
    if route not in {"A", "B", "C"}:
        raise ExecutionContractError("design_route_invalid", f"unsupported route {route!r}")
    target_id = str(value.get("target_id") or "").strip()
    if not TARGET_ID_RE.fullmatch(target_id):
        raise ExecutionContractError("design_target_invalid", f"invalid target {target_id!r}")
    lengths = value.get("lengths")
    if not isinstance(lengths, list) or not lengths:
        raise ExecutionContractError("design_lengths_invalid", "lengths must be non-empty")
    normalized_lengths = sorted({
        _require_int(item, "design_lengths_invalid", "length", minimum=5, maximum=30)
        for item in lengths
    })
    return {
        "route": route,
        "target_id": target_id,
        "lengths": normalized_lengths,
        "proposal_count": _require_int(
            value.get("proposal_count"),
            "design_count_invalid",
            "proposal_count",
            minimum=1,
            maximum=1000,
        ),
        "seed": _require_int(value.get("seed"), "design_seed_invalid", "seed"),
    }


def _validate_iterate_design_parameters(parameters: dict, action: str) -> dict:
    allowed = {
        "strategy_directives",
        "required_targets",
        "route_budget_snapshot",
        "design_jobs",
        "project_config_digest",
        "reuse_existing_prediction_evidence",
    }
    _require_exact_keys(parameters, allowed, "execution_parameters_invalid", action)
    if set(parameters) != allowed:
        raise ExecutionContractError(
            "execution_parameters_invalid",
            f"{action} parameters must contain {sorted(allowed)}",
        )
    jobs = [_normalize_design_job(item, index) for index, item in enumerate(
        parameters.get("design_jobs") or []
    )]
    if not jobs:
        raise ExecutionContractError("design_jobs_missing", "iterate_design has no jobs")
    targets = _require_strings(
        parameters.get("required_targets"),
        "design_targets_invalid",
        "required_targets",
        pattern=TARGET_ID_RE,
    )
    if any(job["target_id"] not in targets for job in jobs):
        raise ExecutionContractError(
            "design_target_invalid", "a design job targets an unapproved target"
        )
    strategies = _require_strings(
        parameters.get("strategy_directives"),
        "design_strategy_invalid",
        "strategy_directives",
    )
    budgets = _require_object(
        parameters.get("route_budget_snapshot"),
        "design_budget_invalid",
        "route_budget_snapshot",
    )
    normalized_budgets = {
        str(key): _require_int(value, "design_budget_invalid", str(key), maximum=10**9)
        for key, value in budgets.items()
    }
    digest = str(parameters.get("project_config_digest") or "")
    if not SHA256_RE.fullmatch(digest):
        raise ExecutionContractError(
            "project_config_digest_invalid", "project config digest must be SHA-256"
        )
    normalized = {
        "strategy_directives": strategies,
        "required_targets": targets,
        "route_budget_snapshot": normalized_budgets,
        "design_jobs": jobs,
        "project_config_digest": digest,
        "reuse_existing_prediction_evidence": _require_bool(
            parameters.get("reuse_existing_prediction_evidence"),
            "reuse_flag_invalid",
            "reuse_existing_prediction_evidence",
        ),
    }
    return normalized


def _validate_evaluate_new_design_candidates_parameters(parameters: dict, action: str) -> dict:
    allowed = {
        "reuse_complete_evidence",
        "evidence_mode",
        "predictor_protocol",
    }
    _require_exact_keys(parameters, allowed, "execution_parameters_invalid", action)
    if set(parameters) != allowed:
        raise ExecutionContractError(
            "execution_parameters_invalid",
            f"{action} parameters must contain {sorted(allowed)}",
        )
    evidence_mode = str(parameters.get("evidence_mode") or "")
    if evidence_mode not in {"reuse_or_generate_full", "ingest_existing"}:
        raise ExecutionContractError(
            "prediction_evidence_mode_invalid", f"unsupported mode {evidence_mode!r}"
        )
    protocol = parameters.get("predictor_protocol")
    if not isinstance(protocol, dict):
        raise ExecutionContractError(
            "prediction_protocol_invalid",
            "predictor_protocol must be a protocol identity object "
            "{name, version, sha256}",
        )
    unknown = sorted(set(protocol) - {"name", "version", "sha256"})
    if unknown:
        raise ExecutionContractError(
            "prediction_protocol_invalid",
            f"predictor_protocol has unsupported fields: {unknown}",
        )
    if not all(
        isinstance(protocol[key], str) and protocol[key]
        for key in ("name", "version", "sha256")
    ):
        raise ExecutionContractError(
            "prediction_protocol_invalid",
            "predictor_protocol name/version/sha256 must be non-empty strings",
        )
    if not SHA256_RE.fullmatch(protocol["sha256"]):
        raise ExecutionContractError(
            "prediction_protocol_invalid",
            "predictor_protocol sha256 must be a SHA-256 hex digest",
        )
    if protocol["name"] not in PREDICTOR_PROTOCOLS:
        raise ExecutionContractError(
            "prediction_protocol_invalid",
            f"unsupported protocol {protocol['name']!r}",
        )
    if protocol != protocol_binding():
        raise ExecutionContractError(
            "prediction_protocol_invalid",
            "predictor_protocol identity does not match the active "
            "protocol; execution can only run the active protocol",
        )
    normalized = {
        "reuse_complete_evidence": _require_bool(
            parameters.get("reuse_complete_evidence"),
            "reuse_flag_invalid",
            "reuse_complete_evidence",
        ),
        "evidence_mode": evidence_mode,
        "predictor_protocol": dict(protocol),
    }
    return normalized


def _validate_review_prediction_handoff_parameters(parameters: dict, action: str) -> dict:
    allowed = {"min_cohort", "low_diversity_similarity"}
    _require_exact_keys(parameters, allowed, "execution_parameters_invalid", action)
    min_cohort = parameters.get("min_cohort", 3)
    similarity = parameters.get("low_diversity_similarity", 0.80)
    _require_int(min_cohort, "critic_parameter_invalid", "min_cohort", minimum=1, maximum=10000)
    try:
        similarity_value = float(similarity)
    except (TypeError, ValueError) as exc:
        raise ExecutionContractError(
            "critic_parameter_invalid", "low_diversity_similarity must be numeric"
        ) from exc
    if not math.isfinite(similarity_value) or not 0.0 <= similarity_value <= 1.0:
        raise ExecutionContractError(
            "critic_parameter_invalid", "low_diversity_similarity must be within [0, 1]"
        )
    normalized = {
        "min_cohort": min_cohort,
        "low_diversity_similarity": similarity_value,
    }
    return normalized


def _validate_propose_threshold_calibration_parameters(parameters: dict, action: str) -> dict:
    allowed = {"threshold_keys"}
    _require_exact_keys(parameters, allowed, "execution_parameters_invalid", action)
    normalized = {
        "threshold_keys": _require_strings(
            parameters.get("threshold_keys") or [],
            "threshold_keys_invalid",
            "threshold_keys",
            allow_empty=True,
        )
    }
    return normalized


def validate_task_parameters(task: dict) -> dict:
    """Validate and normalize one Planner task without executing it."""
    task = _require_object(task, "execution_task_invalid", "task")
    action = str(task.get("action") or "").strip()
    if action not in ALL_KNOWN_ACTIONS:
        raise ExecutionContractError("execution_action_unknown", f"unknown action {action!r}")
    parameters = _require_object(
        task.get("parameters"), "execution_parameters_invalid", f"{action}.parameters"
    )

    if action == "iterate_design":
        normalized = _validate_iterate_design_parameters(parameters, action)
    elif action == "evaluate_new_design_candidates":
        normalized = _validate_evaluate_new_design_candidates_parameters(parameters, action)
    elif action == "review_prediction_handoff":
        normalized = _validate_review_prediction_handoff_parameters(parameters, action)
    elif action == "propose_threshold_calibration":
        normalized = _validate_propose_threshold_calibration_parameters(parameters, action)
    else:
        # Reserved and currently unimplemented actions retain an object payload
        # so their future schema can be versioned without accepting commands.
        normalized = dict(parameters)

    resource = _require_object(
        task.get("resource_request"), "execution_resource_invalid", "resource_request"
    )
    action_spec = get_action_spec(action)
    if resource.get("class") != action_spec.resource_class:
        raise ExecutionContractError(
            "execution_resource_class_mismatch",
            f"{action} requires resource class {action_spec.resource_class!r}",
        )
    proposal_count = _require_int(
        resource.get("proposal_count", 0), "execution_resource_invalid", "proposal_count",
        maximum=10**9,
    )
    candidate_limit = _require_int(
        resource.get("candidate_limit", 0), "execution_resource_invalid", "candidate_limit",
        maximum=10**9,
    )
    if action == "iterate_design":
        job_total = sum(job["proposal_count"] for job in normalized["design_jobs"])
        if job_total != proposal_count:
            raise ExecutionContractError(
                "design_count_mismatch",
                f"design job total {job_total} differs from resource proposal_count {proposal_count}",
            )
    scope = _require_object(
        task.get("candidate_scope"), "candidate_scope_invalid", "candidate_scope"
    )
    _require_strings(
        scope.get("candidate_ids") or [],
        "candidate_scope_invalid",
        "candidate_ids",
        pattern=CANDIDATE_ID_RE,
        allow_empty=True,
    )
    declared_outputs = tuple(task.get("outputs") or [])
    expected_outputs = ACTION_DECLARED_OUTPUTS.get(action)
    if expected_outputs is not None and declared_outputs != expected_outputs:
        raise ExecutionContractError(
            "execution_outputs_invalid",
            f"{action} must declare outputs {list(expected_outputs)}",
        )
    if action == "evaluate_new_design_candidates" and candidate_limit < 1:
        raise ExecutionContractError(
            "prediction_candidate_limit_invalid", "Prediction candidate_limit must be positive"
        )
    return normalized


def assert_action_executable(task: dict) -> dict:
    action = str((task or {}).get("action") or "").strip()
    try:
        spec = get_action_spec(action)
    except ValueError as exc:
        raise ExecutionContractError(
            "execution_action_unknown", f"{action} is not a known execution action"
        ) from exc
    if action in V2_RESERVED_ACTIONS:
        raise ExecutionContractError(
            "execution_action_reserved_v2",
            f"{action} is reserved for v2 and has no executable handler",
        )
    if not spec.executable:
        raise ExecutionContractError(
            "execution_action_unimplemented", f"{action} has no reviewed Execution v1 handler"
        )
    normalized = validate_task_parameters(task)
    # Import lazily: action_registry binds the legacy HANDLERS map and imports
    # this module for the output contracts.
    from execution.action_registry import handler_for

    if handler_for(spec.action) is None:
        raise ExecutionContractError(
            "execution_action_handler_missing",
            f"{action} is marked executable but has no registered handler",
        )
    return normalized


def validate_dispatch_packet(packet: dict, *, expected_sha256: str | None = None) -> dict:
    packet = _require_object(packet, "dispatch_packet_invalid", "dispatch packet")
    schema_version = packet.get("schema_version")
    if schema_version not in {LEGACY_DISPATCH_SCHEMA_VERSION, DISPATCH_SCHEMA_VERSION}:
        raise ExecutionContractError(
            "dispatch_schema_unsupported", "unsupported dispatch schema version"
        )
    if expected_sha256 is not None and object_sha256(packet) != expected_sha256:
        raise ExecutionContractError(
            "dispatch_hash_mismatch", "dispatch packet content differs from expected digest"
        )
    if not TASK_ID_RE.fullmatch(str((packet.get("task") or {}).get("task_id") or "")):
        raise ExecutionContractError("dispatch_task_invalid", "dispatch packet has invalid task ID")
    token = str(packet.get("claim_token") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ExecutionContractError("dispatch_claim_invalid", "dispatch packet has invalid claim token")
    trace = packet.get("trace_context")
    if schema_version == DISPATCH_SCHEMA_VERSION and trace is None:
        raise ExecutionContractError(
            "dispatch_trace_missing", "canonical dispatch packet requires trace_context"
        )
    if trace is not None:
        try:
            context = TraceContext.from_dict(trace)
        except ValueError as exc:
            raise ExecutionContractError(
                "dispatch_trace_invalid", "dispatch packet has invalid trace context"
            ) from exc
        task_id = packet["task"]["task_id"]
        if context.task_id != task_id:
            raise ExecutionContractError(
                "dispatch_trace_invalid", "trace task_id differs from dispatch task"
            )
        attempt = _require_int(
            packet.get("task_attempt"), "dispatch_attempt_invalid", "task_attempt", minimum=1
        )
        expected_attempt_id = TraceContext.attempt_id_for(task_id, attempt)
        if context.attempt_id != expected_attempt_id:
            raise ExecutionContractError(
                "dispatch_trace_invalid", "trace attempt_id differs from dispatch attempt"
            )
        if schema_version == DISPATCH_SCHEMA_VERSION:
            for field in ("workflow_id", "run_id", "plan_id", "attempt_id"):
                if packet.get(field) != getattr(context, field):
                    raise ExecutionContractError(
                        "dispatch_trace_invalid",
                        f"trace {field} differs from dispatch packet binding",
                    )
    assert_action_executable(packet["task"])
    return packet


def _read_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ExecutionContractError(f"{label}_missing", f"missing {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ExecutionContractError(f"{label}_malformed", f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ExecutionContractError(f"{label}_invalid", f"{label} must be an object")
    return value


def _read_json_array(path: Path, label: str) -> list[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ExecutionContractError(f"{label}_missing", f"missing {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ExecutionContractError(f"{label}_malformed", f"invalid JSON: {path}") from exc
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ExecutionContractError(f"{label}_invalid", f"{label} must be an array of objects")
    return value


def _validate_design_result(value: dict, task: dict) -> None:
    required = {
        "schema_version", "execution_worker_version", "action", "task_id", "project_id",
        "project_config_digest", "jobs", "candidate_index_before_sha256",
        "candidate_index_after_sha256", "candidate_index_before_snapshot",
        "candidate_index_after_snapshot", "new_candidate_ids", "candidates",
        "existing_rows_unchanged", "completed_at",
    }
    if not required.issubset(value):
        raise ExecutionContractError(
            "design_result_invalid", f"design result lacks {sorted(required - set(value))}"
        )
    if value.get("schema_version") != EXECUTION_SCHEMA_VERSION or value.get("action") != task["action"]:
        raise ExecutionContractError("design_result_invalid", "design result identity mismatch")
    if value.get("task_id") != task.get("task_id") or value.get("existing_rows_unchanged") is not True:
        raise ExecutionContractError("design_result_invalid", "design result task/index invariant failed")
    snapshots = []
    for key in ("candidate_index_before_snapshot", "candidate_index_after_snapshot"):
        reference = value.get(key)
        if not isinstance(reference, dict):
            raise ExecutionContractError("design_result_invalid", f"{key} must be an object")
        path = Path(str(reference.get("path") or "")).expanduser().resolve()
        if not path.is_file() or file_sha256(path) != reference.get("sha256"):
            raise ExecutionContractError(
                "design_result_invalid", f"candidate index snapshot is missing or changed: {path}"
            )
        snapshots.append(_read_json_array(path, key))
    before_rows, after_rows = snapshots
    if object_sha256(before_rows) != value.get("candidate_index_before_sha256"):
        raise ExecutionContractError("design_result_invalid", "before snapshot digest mismatch")
    if object_sha256(after_rows) != value.get("candidate_index_after_sha256"):
        raise ExecutionContractError("design_result_invalid", "after snapshot digest mismatch")
    before_by_id = {str(item.get("candidate_id") or ""): item for item in before_rows}
    after_by_id = {str(item.get("candidate_id") or ""): item for item in after_rows}
    if "" in before_by_id or "" in after_by_id or len(before_by_id) != len(before_rows) or len(after_by_id) != len(after_rows):
        raise ExecutionContractError("design_result_invalid", "candidate index snapshot IDs are invalid")
    if any(after_by_id.get(candidate_id) != row for candidate_id, row in before_by_id.items()):
        raise ExecutionContractError("design_result_invalid", "existing candidate rows changed")
    ids = _require_strings(
        value.get("new_candidate_ids") or [], "design_result_invalid", "new_candidate_ids",
        pattern=CANDIDATE_ID_RE, allow_empty=True,
    )
    candidates = value.get("candidates")
    if not isinstance(candidates, list) or [item.get("candidate_id") for item in candidates] != ids:
        raise ExecutionContractError("design_result_invalid", "candidate inventory differs from IDs")
    if ids != sorted(set(after_by_id) - set(before_by_id)):
        raise ExecutionContractError("design_result_invalid", "new candidate IDs differ from snapshots")
    try:
        from data_layer import CandidateIndex

        if object_sha256(CandidateIndex.load()) != value.get("candidate_index_after_sha256"):
            raise ExecutionContractError(
                "design_result_invalid", "authoritative CandidateIndex differs from after snapshot"
            )
    except ExecutionContractError:
        raise
    except Exception as exc:
        raise ExecutionContractError(
            "design_result_invalid", "cannot verify authoritative CandidateIndex"
        ) from exc
    for item in candidates:
        manifest = Path(str(item.get("manifest_path") or "")).expanduser().resolve()
        if not manifest.is_file() or file_sha256(manifest) != item.get("manifest_sha256"):
            raise ExecutionContractError(
                "design_result_invalid", f"candidate manifest is missing or changed: {manifest}"
            )


def _validate_prediction_handoff(
    value: dict,
    task: dict,
    dependency_outputs: dict[str, list[dict]] | None = None,
) -> None:
    required = {
        "schema_version", "pipeline_version", "run_id", "project_id",
        "required_targets", "categories", "downstream",
    }
    if not required.issubset(value):
        raise ExecutionContractError(
            "prediction_handoff_invalid", f"Prediction handoff lacks {sorted(required - set(value))}"
        )
    if not str(value.get("run_id") or "").startswith("prediction_"):
        raise ExecutionContractError("prediction_handoff_invalid", "invalid Prediction run ID")
    categories = value.get("categories")
    if not isinstance(categories, dict):
        raise ExecutionContractError("prediction_handoff_invalid", "categories must be an object")
    scope_ids = set((task.get("candidate_scope") or {}).get("candidate_ids") or [])
    from_task_id = (task.get("candidate_scope") or {}).get("from_task_id")
    if from_task_id:
        matches = [
            item
            for item in (dependency_outputs or {}).get(from_task_id, [])
            if item.get("role") == "design_result"
        ]
        if len(matches) != 1:
            raise ExecutionContractError(
                "prediction_handoff_scope_mismatch", "missing unique upstream Design result"
            )
        design_result = _read_json(
            Path(str(matches[0].get("path") or "")).expanduser().resolve(),
            "design_result",
        )
        upstream_ids = set(design_result.get("new_candidate_ids") or [])
        if scope_ids and scope_ids != upstream_ids:
            raise ExecutionContractError(
                "prediction_handoff_scope_mismatch", "task and Design candidate scopes differ"
            )
        scope_ids = upstream_ids
    actual_ids = {
        str(item.get("candidate_id"))
        for entries in categories.values() if isinstance(entries, list)
        for item in entries if isinstance(item, dict) and item.get("candidate_id")
    }
    if scope_ids != actual_ids:
        raise ExecutionContractError(
            "prediction_handoff_scope_mismatch",
            f"handoff candidates {sorted(actual_ids)} differ from task scope {sorted(scope_ids)}",
        )
    for entries in categories.values():
        if not isinstance(entries, list):
            raise ExecutionContractError("prediction_handoff_invalid", "category values must be arrays")
        for item in entries:
            if not isinstance(item, dict):
                raise ExecutionContractError("prediction_handoff_invalid", "category entry must be object")
            record = Path(str(item.get("record_path") or "")).expanduser().resolve()
            if not record.is_file() or file_sha256(record) != item.get("record_sha256"):
                raise ExecutionContractError(
                    "prediction_record_invalid", f"Prediction record missing or changed: {record}"
                )


def _validate_critic_report(
    value: dict,
    task: dict,
    dependency_outputs: dict[str, list[dict]] | None = None,
) -> None:
    required = {
        "schema_version", "critic_version", "report_id", "input_digest", "source",
        "verdict", "passed", "issues", "recommendations", "planner_handoff",
    }
    if not required.issubset(value):
        raise ExecutionContractError(
            "critic_report_invalid", f"Critic report lacks {sorted(required - set(value))}"
        )
    digest = str(value.get("input_digest") or "")
    if value.get("report_id") != f"critic_{digest[:12]}" or not SHA256_RE.fullmatch(digest):
        raise ExecutionContractError("critic_report_invalid", "Critic report ID/digest mismatch")
    dependencies = [
        item for values in (dependency_outputs or {}).values() for item in values
        if item.get("role") == "prediction_handoff"
    ]
    if len(dependencies) != 1:
        raise ExecutionContractError(
            "critic_report_invalid", "Critic requires one upstream Prediction handoff"
        )
    source = value.get("source") or {}
    if source.get("prediction_handoff_sha256") != dependencies[0].get("sha256"):
        raise ExecutionContractError(
            "critic_report_invalid", "Critic source hash differs from upstream handoff"
        )


def _validate_calibration_proposal(
    value: dict,
    task: dict,
    dependency_outputs: dict[str, list[dict]] | None = None,
    approved_project_id: str | None = None,
) -> None:
    required = {
        "schema_version", "execution_worker_version", "action", "task_id", "project_id",
        "status", "requested_threshold_keys", "current_thresholds", "control_requirements",
        "control_data", "applied_to_state", "created_at",
    }
    if not required.issubset(value):
        raise ExecutionContractError(
            "calibration_proposal_invalid",
            f"calibration proposal lacks {sorted(required - set(value))}",
        )
    if value.get("action") != task.get("action") or value.get("task_id") != task.get("task_id"):
        raise ExecutionContractError("calibration_proposal_invalid", "proposal identity mismatch")
    requested = (task.get("parameters") or {}).get("threshold_keys") or []
    if value.get("requested_threshold_keys") != requested:
        raise ExecutionContractError(
            "calibration_proposal_invalid", "requested threshold keys differ from task"
        )
    if value.get("status") not in {"pending_controls", "ready_for_calibration"}:
        raise ExecutionContractError(
            "calibration_proposal_invalid", "calibration proposal status is invalid"
        )
    approved_project_id = approved_project_id or task.get("project_id")
    if not approved_project_id or value.get("project_id") != approved_project_id:
        raise ExecutionContractError(
            "calibration_proposal_invalid", "proposal project differs from approved project"
        )
    control_data = value.get("control_data")
    if (
        not isinstance(control_data, dict)
        or not {"available", "path", "sha256"}.issubset(control_data)
        or not isinstance(control_data.get("available"), bool)
    ):
        raise ExecutionContractError(
            "calibration_proposal_invalid", "control_data must declare availability"
        )
    control_path = control_data.get("path")
    control_digest = control_data.get("sha256")
    if control_data["available"]:
        path = Path(str(control_path or "")).expanduser().resolve()
        if not path.is_file() or not control_digest or file_sha256(path) != control_digest:
            raise ExecutionContractError(
                "calibration_proposal_invalid", "available control_data is missing or changed"
            )
    elif control_path not in (None, "") or control_digest not in (None, ""):
        raise ExecutionContractError(
            "calibration_proposal_invalid", "unavailable control_data cannot carry path or sha256"
        )
    if value.get("applied_to_state") is not False:
        raise ExecutionContractError(
            "calibration_proposal_invalid", "calibration proposal must not mutate thresholds"
        )


OUTPUT_VALIDATORS = {
    "iterate_design": _validate_design_result,
    "evaluate_new_design_candidates": _validate_prediction_handoff,
    "review_prediction_handoff": _validate_critic_report,
    "propose_threshold_calibration": _validate_calibration_proposal,
}


def validate_output_inventory(
    task: dict,
    inventory: Iterable[dict],
    *,
    dependency_outputs: dict[str, list[dict]] | None = None,
    approved_project_id: str | None = None,
) -> None:
    """Validate role, JSON semantics and linked immutable artifacts."""
    action = str(task.get("action") or "")
    assert_action_executable(task)
    expected_roles = ACTION_OUTPUT_ROLES[action]
    values = list(inventory)
    roles = tuple(item.get("role") for item in values)
    if roles != expected_roles:
        raise ExecutionContractError(
            "task_output_role_invalid",
            f"{action} requires output roles {list(expected_roles)}; got {list(roles)}",
        )
    if len(values) != 1:
        raise ExecutionContractError("task_output_invalid", "Execution v1 actions have one primary output")
    path = Path(str(values[0].get("path") or "")).expanduser().resolve()
    value = _read_json(path, expected_roles[0])
    validator = OUTPUT_VALIDATORS[action]
    if action == "iterate_design":
        validator(value, task)
    elif action == "propose_threshold_calibration":
        validator(value, task, dependency_outputs, approved_project_id)
    else:
        validator(value, task, dependency_outputs)
