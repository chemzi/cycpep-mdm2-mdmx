"""
环肽Agent证据日志 & 候选索引表 - 共享数据层
所有Agent通过此模块读写 state.json、evidence_log.jsonl、candidate_index.csv

使用方式:
    from data_layer import State, EvidenceLogger, CandidateIndex, evaluate_battery

    # 读全局状态
    state = State.load()

    # 记一条日志
    EvidenceLogger.log(agent="design", event_type="design_batch",
                       payload={"route": "route_A", "n_generated": 200})

    # 加一条候选到索引表（旧字段名 monomer_plddt/layer1_pass 等会自动 alias 到新列）
    CandidateIndex.add({"candidate_id": "C0001", "sequence": "GFEWALA...", ...})

    # 更新候选分数
    CandidateIndex.update_score("C0001", {"ipsae_mdm2": 0.72, "iptm_mdm2": 0.84})  # ipTM 仅做参考

    # 七层指标电池全清判定（v5 主判定函数）
    result = evaluate_battery(candidate_dict, thresholds=State.load().get("thresholds"))
    if result["all_layers_pass"]:
        # 准入下一阶段
        ...
"""
import functools
import csv
import json, hashlib, os, sys, types, uuid
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from project_config import (
    load_project_config,
    required_target_ids,
    target_slug,
)
from threshold_contract import merge_thresholds, normalize_thresholds
from contracts.event import EvidenceEvent
from contracts.trace import TRACE_ID_RE, TraceContext
from core.context import ProjectPaths  # noqa: E402
from storage import (  # noqa: E402
    SQLiteStore,
    write_csv_projection,
    write_json_projection,
    write_jsonl_projection,
)

ROOT = Path(__file__).resolve().parent

# ============================================================
# 惰性项目运行时（Engineering Standard §7 / Roadmap PR5）
# ============================================================
# 项目配置与派生路径不再于 import 时解析；首次访问（模块属性或内部 helper）
# 时才加载并缓存。模块级名字（ACTIVE_PROJECT_CONFIG / DATA_DIR / ...）通过
# PEP 562 ``__getattr__`` 提供，``from data_layer import DATA_DIR`` 等旧用法
# 保持不变；显式赋值重定向（测试与 mock 常用）由 _LazyPathsModule 协调，
# 赋值/删除时同步失效路径缓存，删除后重新按环境解析，不会永久残留。


@functools.lru_cache(maxsize=1)
def _active_project_config() -> dict:
    """Approved project config, resolved once on first access (PR5)."""
    return load_project_config()


def _sqlite_db_path() -> Path:
    """Formal project database path, resolved lazily with ProjectContext paths."""
    raw = os.environ.get("CYCPEP_DB_PATH")
    if raw:
        return Path(raw)
    explicit_data_dir = sys.modules[__name__].__dict__.get("DATA_DIR")
    data_dir = Path(explicit_data_dir) if explicit_data_dir is not None else _paths()["data_dir"]
    return data_dir / "store.db"


def _project_data_dir(config: dict) -> Path:
    """Project-scoped data dir (single source: core.context.ProjectPaths)."""
    return ProjectPaths().resolve(config["project_id"], root=ROOT).data_dir


def _project_evidence_dir(config: dict) -> Path:
    """Project-scoped evidence dir (single source: core.context.ProjectPaths)."""
    return ProjectPaths().resolve(config["project_id"], root=ROOT).evidence_dir


_runtime_paths: dict | None = None


def _paths() -> dict:
    """Resolve project data/evidence paths once; honour env overrides."""
    global _runtime_paths
    if _runtime_paths is None:
        config = _active_project_config()
        data_dir = Path(os.environ.get("CYCPEP_DATA_DIR", _project_data_dir(config)))
        evidence_dir = Path(os.environ.get("CYCPEP_EVIDENCE_DIR", _project_evidence_dir(config)))
        _runtime_paths = {
            "data_dir": data_dir,
            "evidence_dir": evidence_dir,
            "state_path": data_dir / "state.json",
            "log_path": evidence_dir / "evidence_log.jsonl",
            "index_path": data_dir / "candidate_index.csv",
        }
    return _runtime_paths


def _reset_runtime_paths() -> None:
    """Drop the cached path snapshot so the next access re-resolves."""
    global _runtime_paths
    _runtime_paths = None


def _get_candidate_index():
    """Lazy re-export: CandidateIndex lives in candidate_index.py (PR8)."""
    from candidate_index import CandidateIndex
    return CandidateIndex


_LAZY_ATTRIBUTES = {
    "ACTIVE_PROJECT_CONFIG": _active_project_config,
    "CandidateIndex": _get_candidate_index,
    "DATA_DIR": lambda: _paths()["data_dir"],
    "EVIDENCE_DIR": lambda: _paths()["evidence_dir"],
    "STATE_PATH": lambda: _paths()["state_path"],
    "LOG_PATH": lambda: _paths()["log_path"],
    "INDEX_PATH": lambda: _paths()["index_path"],
    "SQLITE_DB_PATH": _sqlite_db_path,
}


def __getattr__(name):
    """PEP 562: serve legacy data-layer names lazily on first access."""
    getter = _LAZY_ATTRIBUTES.get(name)
    if getter is not None:
        return getter()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _module_attr(name):
    """Read a lazy module name through the module object (PEP 562 does not
    apply to bare globals inside this module)."""
    return getattr(sys.modules[__name__], name)


class _LazyPathsModule(types.ModuleType):
    """Coordinate explicit path redirections with the lazy accessors.

    Repository code and tests follow the established pattern of redirecting
    paths with ``data_layer.DATA_DIR = tmp``, and ``unittest.mock.patch``
    applies/drops such attributes via ``__setattr__``/``__delattr__``.  PEP 562
    ``__getattr__`` alone cannot serve this: a plain assignment writes a
    permanent ``__dict__`` entry that shadows the lazy accessor for the rest
    of the process.  Intercepting assignment/deletion keeps the contract
    consistent:

    - reads prefer an explicit ``__dict__`` value (the repo-wide redirect
      pattern) and otherwise fall back to the ``_runtime_paths`` cache;
    - assignments write ``__dict__`` AND invalidate the cache, so a later
      ``del`` makes the next read re-resolve from the environment;
    - deletions drop both, matching ``mock.patch`` ``stop`` semantics.
    """

    _PATH_KEYS = {
        "DATA_DIR": "data_dir",
        "EVIDENCE_DIR": "evidence_dir",
        "STATE_PATH": "state_path",
        "LOG_PATH": "log_path",
        "INDEX_PATH": "index_path",
    }

    def __setattr__(self, name: str, value) -> None:
        if name in _LazyPathsModule._PATH_KEYS:
            _reset_runtime_paths()
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in _LazyPathsModule._PATH_KEYS:
            _reset_runtime_paths()
        try:
            super().__delattr__(name)
        except AttributeError:
            if name not in _LazyPathsModule._PATH_KEYS:
                raise


# Install the module-level hooks so redirects never break the lazy contract.
sys.modules[__name__].__class__ = _LazyPathsModule


class _LazyClassAttribute:
    """Descriptor that resolves a class attribute once on first access (PR5)."""

    _MISSING = object()

    def __init__(self, getter):
        self._getter = getter
        self._value = self._MISSING

    def __get__(self, obj, owner=None):
        if self._value is self._MISSING:
            self._value = self._getter()
        return self._value


def get_storage_backend(*, read_only: bool = False):
    """Return the sole formal backend; files are one-way projections only."""
    db_path = _module_attr("SQLITE_DB_PATH")
    return SQLiteStore(
        db_path,
        project_id=_module_attr("ACTIVE_PROJECT_CONFIG")["project_id"],
        read_only=read_only,
    )


def validate_storage_backend(
    database_path: str | Path, *, project_id: str
) -> None:
    """Prove one exact formal Store can be opened without writes."""

    SQLiteStore(database_path, project_id=project_id, read_only=True)


def _project_id() -> str:
    return str(_module_attr("ACTIVE_PROJECT_CONFIG")["project_id"])


def _project_state(store=None) -> dict:
    backend = store or get_storage_backend()
    return backend.initialize_state(_project_id(), State._default)


def _project_state_file(store=None) -> None:
    backend = store or get_storage_backend()
    write_json_projection(_module_attr("STATE_PATH"), backend.get_state(_project_id()))


def project_candidates(store=None) -> None:
    backend = store or get_storage_backend()
    write_csv_projection(_module_attr("INDEX_PATH"), backend.list(), INDEX_COLUMNS)


def _project_evidence(store=None) -> None:
    backend = store or get_storage_backend()
    write_jsonl_projection(_module_attr("LOG_PATH"), backend.query())


def allocate_candidate_id() -> str:
    """Reserve one collision-free candidate ID inside a database transaction."""
    backend = get_storage_backend()
    _project_state(backend)
    candidate_id = backend.reserve_candidate_ids(1)[0]
    _project_state_file(backend)
    return candidate_id


def refresh_projections() -> None:
    """Rebuild all compatibility files from the formal database."""
    backend = get_storage_backend()
    _project_state(backend)
    _project_state_file(backend)
    project_candidates(backend)
    _project_evidence(backend)


def migrate_legacy_data(
    *,
    state_path: str | Path | None = None,
    candidate_path: str | Path | None = None,
    evidence_path: str | Path | None = None,
) -> dict[str, int]:
    """Explicitly import legacy files once, then rebuild projections from SQLite."""
    backend = get_storage_backend()
    stats = {"states": 0, "candidates": 0, "events": 0}
    if state_path and Path(state_path).is_file():
        backend.replace_state(
            _project_id(), json.loads(Path(state_path).read_text(encoding="utf-8"))
        )
        stats["states"] = 1
    if candidate_path and Path(candidate_path).is_file():
        source = Path(candidate_path)
        if source.resolve() == _module_attr("INDEX_PATH").resolve():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = source.with_name(f"{source.stem}.pre_store_{stamp}.csv")
            backup.write_bytes(source.read_bytes())
        with source.open("r", encoding="utf-8-sig", newline="") as stream:
            candidate_index_cls = _module_attr("CandidateIndex")
            for raw in csv.DictReader(stream):
                backend.upsert(
                    candidate_index_cls._prepare_row(raw),
                    duplicate_policy="insert_only",
                )
                stats["candidates"] += 1
    if evidence_path and Path(evidence_path).is_file():
        for line in Path(evidence_path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                backend.append(json.loads(line))
                stats["events"] += 1
            except sqlite3.IntegrityError as exc:
                if "evidence_events.event_id" not in str(exc):
                    raise
    refresh_projections()
    return stats




class EvidenceTraceQueryError(ValueError):
    """A trace query is unsafe because its natural key is ambiguous."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ============================================================
# 全局状态
# ============================================================
class State:
    """Shared project state backed by SQLite; state.json is a projection."""
    
    _project_config = _LazyClassAttribute(_active_project_config)
    _default = _LazyClassAttribute(lambda: default_state(_active_project_config()))
    
    @classmethod
    def load(cls) -> dict:
        backend = get_storage_backend()
        return _project_state(backend)
    
    @classmethod
    def save(cls, data: dict):
        backend = get_storage_backend()
        backend.replace_state(_project_id(), data)
        _project_state_file(backend)
    
    @classmethod
    def update(cls, patches: dict):
        """合并更新，不覆盖已有字段"""
        backend = get_storage_backend()
        _project_state(backend)
        state = backend.update_state(_project_id(), patches)
        _project_state_file(backend)
        return state
    
    @classmethod
    def append_history(cls, entry: dict):
        backend = get_storage_backend()
        _project_state(backend)
        backend.append_state_item(_project_id(), "iteration_history", entry)
        _project_state_file(backend)

    @classmethod
    def append_history_if_absent(
        cls, entry: dict, *, identity_path: tuple[str, ...], identity_value: object
    ):
        backend = get_storage_backend()
        _project_state(backend)
        backend.append_state_item_if_absent(
            _project_id(),
            "iteration_history",
            entry,
            identity_path=identity_path,
            identity_value=identity_value,
        )
        _project_state_file(backend)

    @classmethod
    def register_artifact(cls, artifact: dict) -> str:
        """Register a research/calibration artifact in the formal store.

        The row is identified by ``artifact_id``; no hash/sha256 is computed
        (repository rule against hash machinery).
        """
        backend = get_storage_backend()
        return backend.register_artifact(artifact)

    @classmethod
    def sync_project_config(cls, config: dict) -> dict:
        """Make the approved config authoritative for target identity in state."""
        state = cls.load()
        previous_targets = set((state.get("targets") or {}).keys())
        previous_digest = state.get("approved_digest") or (
            ((state.get("project_config") or {}).get("review") or {}).get("approved_digest")
        )
        approved_digest = (config.get("review") or {}).get("approved_digest")
        config_changed = bool(previous_digest and approved_digest and previous_digest != approved_digest)
        if config_changed:
            # Strong evidence from a different approved target/config is not transferable.
            state["thresholds"] = {}
        previous_budget = state.get("design_budget")
        desired_budget = default_design_budget(config)
        budget_migrated = False
        if (
            config_changed
            or previous_budget in (None, {})
            or previous_budget == LEGACY_DEFAULT_DESIGN_BUDGET
        ) and previous_budget != desired_budget:
            state["design_budget"] = desired_budget
            budget_migrated = True
        state["project"] = config.get("name", config["project_id"])
        state["project_id"] = config["project_id"]
        state["project_config"] = json.loads(json.dumps(config))
        state["approved_digest"] = approved_digest
        # Deliberate replacement: removed targets must not survive a re-approval.
        state["targets"] = {
            target["id"]: {key: value for key, value in target.items() if key != "id"}
            for target in config.get("targets", [])
        }
        cls.save(state)
        current_targets = set(state["targets"])
        EvidenceLogger.log("research", "state_project_config_sync", {
            "previous_approved_digest": previous_digest,
            "approved_digest": approved_digest,
            "config_changed": config_changed,
            "required_target_ids": list(required_target_ids(config)),
            "removed_targets": sorted(previous_targets - current_targets),
            "final_targets": sorted(current_targets),
            "thresholds_cleared": config_changed,
            "design_budget_migrated": budget_migrated,
            "design_budget_keys": sorted((state.get("design_budget") or {}).keys()),
        }, phase="research")
        return state

    @classmethod
    def sync_thresholds_from_cache(cls, cache_path: str | Path) -> dict:
        """Recover/merge canonical Research thresholds from a validated cache.

        The threshold cache is the durable Research artifact. ``state.json`` is
        its evidence-aware runtime projection and may safely be rebuilt.
        """
        path = Path(cache_path)
        state = cls.load()
        status = "complete"
        error = None
        incoming = {}
        if not path.exists():
            status = "cache_missing"
        else:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                incoming = payload.get("thresholds", payload) if isinstance(payload, dict) else {}
            except (OSError, json.JSONDecodeError) as exc:
                status = "cache_invalid"
                error = f"{type(exc).__name__}: {str(exc)[:160]}"

        existing, existing_audit = normalize_thresholds(state.get("thresholds") or {})
        if status == "complete":
            merged, audit = merge_thresholds(existing, incoming)
        else:
            merged = existing
            audit = {
                "cache_keys": [], "final_keys": list(merged),
                "overwritten": [], "skipped": [], "conflict_reasons": {},
                "state_normalization": existing_audit,
                "cache_normalization": {"input_keys": [], "canonical_keys": [], "conflicts": []},
            }
        state["thresholds"] = merged
        cls.save(state)
        EvidenceLogger.log("research", "threshold_cache_sync", {
            "status": status,
            "cache_path": str(path),
            "cache_keys": audit["cache_keys"],
            "final_keys": audit["final_keys"],
            "overwritten": audit["overwritten"],
            "skipped": audit["skipped"],
            "conflict_reasons": audit["conflict_reasons"],
            "normalization_conflicts": audit["cache_normalization"].get("conflicts", []),
            "error": error,
        }, phase="research")
        return {"state": state, "status": status, "audit": audit, "error": error}


# ============================================================
# 证据日志
# ============================================================
class EvidenceLogger:
    """Append-only evidence backed by SQLite; JSONL is a projection."""
    
    @classmethod
    def _write(cls, entry: dict):
        # Every new write passes through the same event contract.  Existing
        # JSONL rows remain untouched and are still readable by get_all().
        EvidenceEvent.from_dict(entry)
        backend = get_storage_backend()
        backend.append(entry)
        _project_evidence(backend)
    
    @classmethod
    def log(cls, agent: str, event_type: str, payload: dict,
            targets: list = None, phase: str = None,
            round_num: int = None, blocks: list = None,
            trace_context: TraceContext | dict | None = None):
        payload = dict(payload or {})
        if event_type == "exploration_decision":
            raise ValueError(
                "exploration_decision requires the dedicated source-validating writer"
            )
        if event_type == "critic_review":
            project_id = payload.get("project_id")
            if not isinstance(project_id, str) or not TRACE_ID_RE.fullmatch(project_id):
                raise ValueError("critic_review project_id must be a valid trace ID")
        if trace_context is not None and not isinstance(trace_context, TraceContext):
            trace_context = TraceContext.from_dict(trace_context)
        event = EvidenceEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_id=str(uuid.uuid4())[:12],
            agent=agent,
            event_type=event_type,
            payload=payload,
            trace_context=trace_context,
            phase=phase,
            round_num=round_num,
            targets=targets,
            blocks=blocks,
        )
        entry = event.to_dict()
        cls._write(entry)
        return entry["event_id"]

    @classmethod
    def research_complete(cls, hotspot_analysis: dict, known_binders: list, refs: list,
                          project_id: str = ""):
        payload = {
            "pdb_complexes": hotspot_analysis.get("pdb_list", []),
            "hotspot_analysis": hotspot_analysis,
            "known_binders": known_binders,
            "literature_refs": refs
        }
        if project_id:
            payload["project_id"] = project_id
        return cls.log("research", "research_targets", payload,
                targets=list(required_target_ids((State.load().get("project_config") or State._project_config))),
                phase="research", round_num=1)

    @classmethod
    def design_batch(cls, route: str, n_generated: int, n_valid: int,
                     tool_name: str, tool_version: str, duration_sec: float):
        return cls.log("design", "design_batch", {
            "route": route, "n_generated": n_generated, "n_valid": n_valid,
            "tool_trace": {"tool_name": tool_name, "tool_version": tool_version,
                           "exit_code": 0, "duration_sec": duration_sec}
        }, targets=list(required_target_ids((State.load().get("project_config") or State._project_config))),
                phase="design")

    @classmethod
    def candidate_registered(cls, candidate: dict):
        cls.log("design", "candidate_registered", {"candidate": candidate},
                targets=list(required_target_ids((State.load().get("project_config") or State._project_config))),
                phase="design")
        # candidate_count is exclusively managed by _next_candidate_id() in
        # agents/design.py.  Calling _increment_counter() here would double-
        # count every candidate (P0-3).

    @classmethod
    def evaluate_layer_start(cls, layer: int, n_candidates: int, thresholds: dict):
        return cls.log("prediction", "evaluate_layer_start", {
            "layer": layer, "n_candidates_in": n_candidates, "thresholds": thresholds
        }, targets=list(required_target_ids((State.load().get("project_config") or State._project_config))),
                phase="evaluate")

    @classmethod
    def candidate_scored(cls, candidate_id: str, layer: int, scores: dict,
                         tool_trace: dict, passed: bool, target_ids: list | None = None):
        if target_ids is None:
            nested_targets = scores.get("metrics", {}).get("targets", {})
            target_ids = list(nested_targets) if isinstance(nested_targets, dict) else []
        if not target_ids:
            configured = required_target_ids(State.load().get("project_config") or State._project_config)
            target_ids = [
                target for target in configured
                if any(
                    key.endswith(f"_{target_slug(target)}") and value not in (None, "")
                    for key, value in scores.items()
                )
            ] or list(configured)
        cls.log("prediction", "candidate_scored", {
            "candidate_id": candidate_id, "layer": layer,
            "scores": scores, "tool_trace": tool_trace, "passed": passed
        }, targets=target_ids, phase="evaluate")

    @classmethod
    def candidate_eliminated(cls, candidate_id: str, layer: int,
                             reason: str, score: float, threshold: float):
        cls.log("prediction", "candidate_eliminated", {
            "candidate_id": candidate_id, "layer": layer,
            "reason": reason, "actual_score": score, "threshold": threshold
        }, phase="evaluate")

    @classmethod
    def battery_evaluated(cls, candidate: dict, battery: dict):
        """Record one structured seven-layer verdict for the experience loop.

        payload 保留 failed_layers 与每层实际值，供 experience.summarize_failures()
        聚合为下一轮生成偏好（失败经验库闭环）。candidate 来自 CandidateInput.snapshot()。
        """
        sequence = str(candidate.get("sequence") or "")
        return cls.log("prediction", "battery_evaluated", {
            "candidate_id": candidate.get("candidate_id"),
            "sequence": sequence,
            "length": len(sequence) if sequence else None,
            "route": candidate.get("source_route"),
            "passed": bool(battery.get("all_layers_pass")),
            "competition_clearance": bool(battery.get("competition_clearance")),
            "failed_layers": battery.get("failed_layers") or [],
            "hard_failures": battery.get("hard_failures") or [],
            "missing_thresholds": battery.get("missing_thresholds") or [],
            "triage_status": battery.get("triage_status"),
            "layer_values": battery.get("layer_values") or {},
            "target_pass": battery.get("target_pass") or {},
        }, targets=list(battery.get("required_targets") or []), phase="evaluate")

    @classmethod
    def evaluate_layer_complete(cls, layer: int, n_in: int, n_pass: int, n_fail: int):
        cls.log("prediction", "evaluate_layer_complete", {
            "layer": layer, "n_in": n_in, "n_pass": n_pass, "n_fail": n_fail,
            "pass_rate": round(n_pass / max(n_in, 1), 3)
        }, targets=list(required_target_ids((State.load().get("project_config") or State._project_config))),
                phase="evaluate")

    @classmethod
    def critic_review(cls, issues: list, passed: bool, summary: str,
                      recommendation: str, metrics: dict,
                      report_id: str = "", report_path: str = "",
                      report_sha256: str = "",
                      prediction_run_id: str = "", project_id: str = ""):
        payload = {
            "issues": issues, "pass": passed,
            "summary": summary, "recommendation": recommendation,
            "metrics_snapshot": metrics,
            "project_id": project_id,
        }
        if report_id:
            payload["report_id"] = report_id
        if report_path:
            payload["report_path"] = report_path
        if report_sha256:
            payload["report_sha256"] = report_sha256
        if prediction_run_id:
            payload["prediction_run_id"] = prediction_run_id
        return cls.log("critic", "critic_review", payload,
                targets=list(required_target_ids((State.load().get("project_config") or State._project_config))),
                phase="critic")

    @classmethod
    def planner_adjust(cls, trigger_event_id: str, old_strategy: dict,
                       new_strategy: dict, reason: str):
        return cls.log("planner", "planner_adjust", {
            "trigger_event_id": trigger_event_id,
            "old_strategy": old_strategy, "new_strategy": new_strategy,
            "reason": reason
        }, targets=list(required_target_ids((State.load().get("project_config") or State._project_config))),
                phase="iterate")

    @classmethod
    def planner_plan(cls, plan_id: str, plan_path: str, plan_sha256: str,
                     critic_report_id: str, status: str, task_count: int,
                     required_approval_task_ids: list,
                     critic_report_path: str | None = None,
                     critic_report_sha256: str | None = None,
                     trace_context: TraceContext | dict | None = None):
        """Record one immutable Planner plan without authorizing execution."""
        payload = {
            "plan_id": plan_id,
            "plan_path": plan_path,
            "plan_sha256": plan_sha256,
            "critic_report_id": critic_report_id,
            "status": status,
            "task_count": task_count,
            "required_approval_task_ids": required_approval_task_ids,
        }
        if critic_report_path:
            payload["critic_report_path"] = critic_report_path
        if critic_report_sha256:
            payload["critic_report_sha256"] = critic_report_sha256
        return cls.log("planner", "planner_plan", payload, targets=list(required_target_ids(
            State.load().get("project_config") or State._project_config
        )), phase="iterate", trace_context=trace_context)

    @classmethod
    def planner_approval_recorded(
        cls, approval_id: str, approval_path: str, approval_sha256: str,
        plan_id: str, plan_sha256: str, approved_task_ids: list,
        approver: str, budget_limits: dict,
        trace_context: TraceContext | dict | None = None,
    ):
        """Record a human approval artifact bound to an immutable plan digest."""
        return cls.log("planner", "planner_approval_recorded", {
            "approval_id": approval_id,
            "approval_path": approval_path,
            "approval_sha256": approval_sha256,
            "plan_id": plan_id,
            "plan_sha256": plan_sha256,
            "approved_task_ids": approved_task_ids,
            "approver": approver,
            "budget_limits": budget_limits,
        }, targets=list(required_target_ids(
            State.load().get("project_config") or State._project_config
        )), phase="iterate", trace_context=trace_context)

    @classmethod
    def error(cls, agent: str, error_type: str, message: str,
              recovery: str = "", trace: str = ""):
        cls.log(agent, "error", {
            "error_type": error_type, "error_message": message,
            "recovery_action": recovery, "stack_trace": trace[:500]
        })

    @classmethod
    def _increment_counter(cls):
        s = State.load()
        s["candidate_count"] = s.get("candidate_count", 0) + 1
        State.save(s)

    @classmethod
    def get_all(cls) -> list:
        return get_storage_backend().query()

    @classmethod
    def filter(cls, agent: str = None, event_type: str = None, candidate_id: str = None) -> list:
        results = []
        for entry in cls.get_all():
            if agent and entry.get("agent") != agent: continue
            if event_type and entry.get("event_type") != event_type: continue
            if candidate_id and entry.get("candidate_id") != candidate_id: continue
            results.append(entry)
        return results

    @classmethod
    def trace_candidate(cls, candidate_id: str) -> list:
        """追溯某条候选的完整生命周期"""
        return [e for e in cls.get_all()
                if e.get("candidate_id") == candidate_id
                or (isinstance(e.get("candidate"), dict) and e["candidate"].get("candidate_id") == candidate_id)]

    @classmethod
    def _trace_field(cls, field: str, value: str) -> list:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} must be a non-empty string")
        return [entry for entry in cls.get_all() if entry.get(field) == value]

    @classmethod
    def trace_workflow(cls, workflow_id: str) -> list:
        return cls._trace_field("workflow_id", workflow_id)

    @classmethod
    def trace_run(cls, run_id: str) -> list:
        return cls._trace_field("run_id", run_id)

    @classmethod
    def trace_task(
        cls,
        task_id: str,
        *,
        workflow_id: str | None = None,
        run_id: str | None = None,
        attempt_id: str | None = None,
    ) -> list:
        """Read task evidence without silently joining distinct workflows.

        ``task_id`` is plan-local (for example, every plan can contain
        ``T001``).  Existing single-workflow callers remain compatible, while
        an unscoped query spanning multiple workflows fails closed.
        """
        entries = cls._trace_field("task_id", task_id)
        if workflow_id is not None:
            if not isinstance(workflow_id, str) or not workflow_id:
                raise ValueError("workflow_id must be a non-empty string")
            entries = [entry for entry in entries if entry.get("workflow_id") == workflow_id]
        if run_id is not None:
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("run_id must be a non-empty string")
            entries = [entry for entry in entries if entry.get("run_id") == run_id]
        if attempt_id is not None:
            if not isinstance(attempt_id, str) or not attempt_id:
                raise ValueError("attempt_id must be a non-empty string")
            entries = [entry for entry in entries if entry.get("attempt_id") == attempt_id]
        if workflow_id is None and run_id is None:
            workflow_scopes = {entry.get("workflow_id") for entry in entries}
            if len(workflow_scopes) > 1:
                raise EvidenceTraceQueryError(
                    "ambiguous_trace_query",
                    f"task_id {task_id} occurs in multiple workflows; provide workflow_id or run_id",
                )
        return entries

    @classmethod
    def trace_artifact(
        cls,
        artifact_id: str | None = None,
        *,
        path: str | None = None,
        sha256: str | None = None,
    ) -> list:
        if not any((artifact_id, path, sha256)):
            raise ValueError("trace_artifact requires artifact_id, path or sha256")
        values = {key: value for key, value in {
            "artifact_id": artifact_id, "path": path, "sha256": sha256
        }.items() if value}
        results = []
        for entry in cls.get_all():
            if any(entry.get(key) == value for key, value in values.items()):
                results.append(entry)
                continue
            for collection_name in ("artifacts", "outputs"):
                artifacts = entry.get(collection_name)
                if isinstance(artifacts, list) and any(
                    isinstance(item, dict)
                    and any(item.get(key) == value for key, value in values.items())
                    for item in artifacts
                ):
                    results.append(entry)
                    break
        return results


# ============================================================
# 候选索引表
# ============================================================
# ============================================================
# 工具函数
# ============================================================
def file_hash(path: str) -> str:
    """流式计算完整文件 SHA-256，返回前 12 位用于索引和证据追溯。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def sanitize_id(s: str) -> str:
    """C0001格式的候选ID"""
    return s if (len(s) == 5 and s.startswith("C")) else f"C{int(s):04d}"

# PR8: schema/battery/candidate blocks were moved out to keep this module under
# the architecture-gate file-size limit. Re-export the public names so the
# legacy module API stays unchanged.
from data_layer_schema import (  # noqa: E402
    INDEX_COLUMNS,
    LEGACY_DEFAULT_DESIGN_BUDGET,
    default_design_budget,
    default_state,
)
from battery_evaluation import (  # noqa: E402
    compute_pareto_front,
    evaluate_battery,
)
# soft_desirability is re-exported here for the same reason: the production
# entry point (data_layer.soft_desirability) stays stable without pulling the
# soft-view module into the top-of-file import chain.
from soft_desirability import (  # noqa: E402
    soft_desirability,
)
