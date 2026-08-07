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
import json, csv, hashlib, os, statistics, sys, tempfile, types, uuid
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from project_config import (
    global_value,
    load_project_config,
    required_target_ids,
    target_slug,
    target_value,
    threshold_for_target,
)
from threshold_contract import merge_thresholds, normalize_thresholds
from contracts.event import EvidenceEvent
from contracts.trace import TraceContext
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


_LAZY_ATTRIBUTES = {
    "ACTIVE_PROJECT_CONFIG": _active_project_config,
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


def get_storage_backend():
    """Return the sole formal backend; files are one-way projections only."""
    db_path = _module_attr("SQLITE_DB_PATH")
    return SQLiteStore(
        db_path,
        project_id=_module_attr("ACTIVE_PROJECT_CONFIG")["project_id"],
    )


def _project_id() -> str:
    return str(_module_attr("ACTIVE_PROJECT_CONFIG")["project_id"])


def _project_state(store=None) -> dict:
    backend = store or get_storage_backend()
    return backend.initialize_state(_project_id(), State._default)


def _project_state_file(store=None) -> None:
    backend = store or get_storage_backend()
    write_json_projection(_module_attr("STATE_PATH"), backend.get_state(_project_id()))


def _project_candidates(store=None) -> None:
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
    _project_candidates(backend)
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
            for raw in csv.DictReader(stream):
                backend.upsert(
                    CandidateIndex._prepare_row(raw),
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

_LEGACY_DEFAULT_DESIGN_BUDGET = {
    "route_A_mdm2": 400,
    "route_A_mdmx": 400,
    "route_B": 400,
    "route_C": 200,
}


def _default_design_budget(config: dict) -> dict[str, int]:
    """Build route capacity keys from the approved target identities."""
    budget = {
        f"route_A_{target_slug(target_id)}": 400
        for target_id in required_target_ids(config)
    }
    budget.update({"route_B": 400, "route_C": 200})
    return budget


def _default_state(config: dict) -> dict:
    """Default State projection for the given project config (PR5, lazy)."""
    return {
        "project": config.get("name", config["project_id"]),
        "project_id": config["project_id"],
        "project_config": config,
        "targets": {
            target["id"]: {key: value for key, value in target.items() if key != "id"}
            for target in config["targets"]
        },
        "phase": "research",
        "round": 1,
        "pocket_differences": {},
        "known_dual_binders": [],
        "design_budget": _default_design_budget(config),
        "candidate_count": 0,
        "iteration_history": [],
        "thresholds": {},
    }


def _write_json_atomic(path: str | Path, payload: dict):
    """Write JSON beside its destination and atomically replace the old file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(temp_name, destination)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)

# v5: 七层指标电池主列。旧列名保留做 alias（见 _ALIAS_MAP），不破坏已有代码。
INDEX_COLUMNS = [
    # --- 基础标识 ---
    "candidate_id","sequence","length","source_route","source_batch",
    # --- 化学与结构交接契约 ---
    "cyclization_type","cyclization_bonds","design_pdb_path","design_pdb_hash",
    "manifest_path",
    # --- v6 通用指标载荷（任意靶点；旧 MDM2/MDMX 列继续用于表格展示）---
    "metrics_json",
    # --- L1 环肽质量 ---
    "plddt","l1_pass",
    # --- L2 界面置信度（ipSAE 主, ipTM 仅做参考, 不卡门槛）---
    "ipsae_mdm2","ipsae_mdmx","ipae_mdm2","iptm_mdm2","iptm_mdmx","l2_pass",
    # --- L3 界面物理 ---
    "dg_mdm2","dg_mdmx","dg_method","sc_mdm2","sc_mdmx","dsasa_mdm2","dsasa_mdmx","l3_pass",
    # --- L4 环化几何 QC（relax 前后各一次）---
    "nc_distance_pre","nc_distance_post","ring_closure_pre","ring_closure_post","l4_pass",
    # --- L5 设计意图 ---
    "hotspot_cov_mdm2","hotspot_cov_mdmx","site_consistency_mdm2",
    "site_consistency_mdmx","site_consistency","l5_pass",
    # --- L6 鲁棒性（多预测器/多 seed 收敛）---
    "pose_rmsd_mdm2","pose_rmsd_mdmx","seed_convergence_mdm2",
    "seed_convergence_mdmx","pose_rmsd","seed_convergence",
    "colab_iptm_mdm2","colab_iptm_mdmx","l6_pass",
    # --- L7 可设计性（scRMSD）---
    "scrmsd","l7_pass",
    # --- 综合判定 ---
    "all_layers_pass","metric_clearance","competition_clearance",
    "triage_status","threshold_audit_json","pareto_front",
    # --- 可合成性（Critic 检查）---
    "synth_pass",
    # --- ADME bonus ---
    "adme_net_charge","adme_tpsa","adme_clogp","adme_chameleonicity","novelty_score",
    # --- 双靶参考（导师禁止做加权门槛但保留做汇报）---
    "asymmetry","dual_score",
    # --- 状态/产出 ---
    "final_status","final_rank","notes","last_updated",
    # --- v4/早期原型字段，只保留历史含义，不参与 v5 判定 ---
    "legacy_self_rmsd","legacy_haddock_mdm2","legacy_haddock_mdmx",
    "legacy_layer1_pass","legacy_layer2_3_pass","legacy_layer4_pass",
    "legacy_cross_tool_ok",
]

# 旧名 → 兼容列。只有 monomer_plddt 与当前 pLDDT 含义一致；
# 其余旧指标保存在 legacy_*，不能直接参与新版七层判定。
_ALIAS_MAP = {
    "monomer_plddt": "plddt",
    "self_rmsd": "legacy_self_rmsd",
    "haddock_mdm2": "legacy_haddock_mdm2",
    "haddock_mdmx": "legacy_haddock_mdmx",
    "layer1_pass": "legacy_layer1_pass",
    "layer2_3_pass": "legacy_layer2_3_pass",
    "layer4_pass": "legacy_layer4_pass",
    "cross_tool_ok": "legacy_cross_tool_ok",
}


def _alias_keys(row: dict) -> dict:
    """旧字段名 → 新字段名（向前兼容旧 Agent 代码）。"""
    for old, new in _ALIAS_MAP.items():
        if old in row and row.get(old) not in (None, "") and row.get(new) in (None, ""):
            row[new] = row.pop(old)
    return row

# ============================================================
# 全局状态
# ============================================================
class State:
    """Shared project state backed by SQLite; state.json is a projection."""
    
    _project_config = _LazyClassAttribute(_active_project_config)
    _default = _LazyClassAttribute(lambda: _default_state(_active_project_config()))
    
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
        desired_budget = _default_design_budget(config)
        budget_migrated = False
        if (
            config_changed
            or previous_budget in (None, {})
            or previous_budget == _LEGACY_DEFAULT_DESIGN_BUDGET
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
        if trace_context is not None and not isinstance(trace_context, TraceContext):
            trace_context = TraceContext.from_dict(trace_context)
        event = EvidenceEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_id=str(uuid.uuid4())[:12],
            agent=agent,
            event_type=event_type,
            payload=dict(payload or {}),
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
    def research_complete(cls, hotspot_analysis: dict, known_binders: list, refs: list):
        return cls.log("research", "research_targets", {
            "pdb_complexes": hotspot_analysis.get("pdb_list", []),
            "hotspot_analysis": hotspot_analysis,
            "known_binders": known_binders,
            "literature_refs": refs
        }, targets=list(required_target_ids((State.load().get("project_config") or State._project_config))),
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
                      report_sha256: str = ""):
        payload = {
            "issues": issues, "pass": passed,
            "summary": summary, "recommendation": recommendation,
            "metrics_snapshot": metrics
        }
        if report_id:
            payload["report_id"] = report_id
        if report_path:
            payload["report_path"] = report_path
        if report_sha256:
            payload["report_sha256"] = report_sha256
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
class CandidateIndex:
    """Candidate compatibility API backed by SQLite with a CSV projection."""

    @classmethod
    def _ensure_exists(cls):
        if not _module_attr("INDEX_PATH").exists():
            _project_candidates()

    @classmethod
    def _migrate_schema(cls, old_header: list[str]):
        """把旧 CSV 显式迁移到当前 schema，并在同目录保留原始备份。"""
        index_path = _module_attr("INDEX_PATH")
        with open(index_path, "r", encoding="utf-8-sig", newline="") as f:
            old_rows = list(csv.DictReader(f))

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = index_path.with_name(f"{index_path.stem}.pre_v5_{stamp}.csv")
        backup.write_bytes(index_path.read_bytes())

        migrated = []
        for old_row in old_rows:
            extra_values = old_row.pop(None, None)
            row = _alias_keys(dict(old_row))
            if extra_values:
                note = row.get("notes", "")
                warning = f"schema migration found {len(extra_values)} unlabelled legacy values"
                row["notes"] = f"{note}; {warning}".strip("; ")
            row["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            migrated.append({col: row.get(col, "") for col in INDEX_COLUMNS})
        cls._write_rows(migrated)

        EvidenceLogger.log("system", "candidate_index_migrated", {
            "old_columns": old_header,
            "new_column_count": len(INDEX_COLUMNS),
            "row_count": len(migrated),
            "backup_path": str(backup),
        }, phase="evaluate")

    @classmethod
    def _write_rows(cls, rows: list[dict]):
        """Write the compatibility projection; formal writes use Store methods."""
        write_csv_projection(_module_attr("INDEX_PATH"), rows, INDEX_COLUMNS)

    @classmethod
    def _prepare_row(cls, row: dict) -> dict:
        row = _alias_keys(dict(row))
        if not row.get("candidate_id") or not row.get("sequence"):
            raise ValueError("candidate_id and sequence are required")
        row.setdefault("source_route", "")
        row.setdefault("source_batch", "")
        row.setdefault("length", len(row["sequence"]))
        row.setdefault("final_status", "pending")
        row.setdefault("last_updated", datetime.now().strftime("%Y-%m-%d %H:%M"))
        if isinstance(row.get("cyclization_bonds"), (list, dict)):
            row["cyclization_bonds"] = json.dumps(
                row["cyclization_bonds"], ensure_ascii=False, separators=(",", ":")
            )
        if isinstance(row.get("metrics"), dict):
            row["metrics_json"] = json.dumps(
                row.pop("metrics"), ensure_ascii=False, separators=(",", ":")
            )
        if isinstance(row.get("threshold_audit"), dict):
            row["threshold_audit_json"] = json.dumps(
                row.pop("threshold_audit"), ensure_ascii=False, separators=(",", ":")
            )
        for pass_col in [
            "l1_pass", "l2_pass", "l3_pass", "l4_pass", "l5_pass", "l6_pass",
            "l7_pass", "all_layers_pass", "metric_clearance",
            "competition_clearance", "synth_pass", "pareto_front",
        ]:
            row.setdefault(pass_col, "")
        return {col: row.get(col, "") for col in INDEX_COLUMNS}

    @classmethod
    def add(cls, row: dict):
        """添加一条新候选。必须包含 candidate_id 和 sequence。"""
        ordered = cls._prepare_row(row)
        backend = get_storage_backend()
        backend.upsert(ordered, duplicate_policy="raise_duplicate")
        _project_candidates(backend)

    @classmethod
    def add_batch(cls, rows: list[dict]):
        prepared = [cls._prepare_row(row) for row in rows]
        backend = get_storage_backend()
        backend.add_candidates(prepared)
        _project_candidates(backend)

    @classmethod
    def load(cls) -> list[dict]:
        return [
            {column: row.get(column, "") for column in INDEX_COLUMNS}
            for row in get_storage_backend().list()
        ]

    @classmethod
    def find(cls, candidate_id: str) -> Optional[dict]:
        row = get_storage_backend().get(candidate_id)
        if row is None:
            return None
        return {column: row.get(column, "") for column in INDEX_COLUMNS}

    @classmethod
    def update_score(cls, candidate_id: str, scores: dict):
        """Atomically update one candidate's score fields in SQLite.
        scores 中的旧字段名（如 monomer_plddt / layer1_pass）会自动 alias 到新名。
        """
        scores = _alias_keys(dict(scores))
        patches = {}
        for key, value in scores.items():
            if key == "metrics" and isinstance(value, dict):
                patches[key] = value
            elif key == "threshold_audit" and isinstance(value, dict):
                patches["threshold_audit_json"] = json.dumps(
                    value, ensure_ascii=False, separators=(",", ":")
                )
            elif key in INDEX_COLUMNS:
                patches[key] = str(value) if not isinstance(value, str) else value
        patches["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        backend = get_storage_backend()
        backend.update_candidate(candidate_id, patches)
        _project_candidates(backend)

    @classmethod
    def update_status(cls, candidate_id: str, status: str, notes: str = ""):
        patches = {
            "final_status": status,
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        if notes:
            patches["notes"] = notes
        backend = get_storage_backend()
        backend.update_candidate(candidate_id, patches)
        _project_candidates(backend)

    @classmethod
    def filter_by_status(cls, status: str) -> list[dict]:
        return [r for r in cls.load() if r.get("final_status") == status]

    @classmethod
    def filter_by_layer(cls, layer_pass: bool, layer: int = 1) -> list[dict]:
        col = {1: "l1_pass", 2: "l2_pass", 3: "l3_pass",
               4: "l4_pass", 5: "l5_pass", 6: "l6_pass", 7: "l7_pass"}[layer]
        return [r for r in cls.load() if r.get(col) == str(layer_pass)]

    @classmethod
    def top_n(cls, n: int = 10, by: str = "dual_score",
              direction: str = "maximize") -> list[dict]:
        """Rank a flat column or ``TARGET:metric`` nested objective."""
        if direction not in {"maximize", "minimize"}:
            raise ValueError("direction must be maximize or minimize")
        if ":" in by:
            target_id, metric = by.split(":", 1)
            value_of = lambda row: _f(target_value(row, target_id, metric))
        else:
            value_of = lambda row: _f(row.get(by))
        rows = [row for row in cls.load() if value_of(row) is not None]
        rows.sort(key=value_of, reverse=direction == "maximize")
        return rows[:n]

    @classmethod
    def stats(cls) -> dict:
        """快速统计：总数、七层各层通过数、ipSAE/dG 中位数（v5 主指标）"""
        rows = cls.load()
        ipsae_m2 = [float(r["ipsae_mdm2"]) for r in rows if r.get("ipsae_mdm2")]
        ipsae_mx = [float(r["ipsae_mdmx"]) for r in rows if r.get("ipsae_mdmx")]
        dg_m2   = [float(r["dg_mdm2"]) for r in rows if r.get("dg_mdm2")]
        scrmsds = [float(r["scrmsd"]) for r in rows if r.get("scrmsd")]
        # ipTM 保留做参考（导师 Trap 1：不做门槛）
        iptm_m2 = [float(r["iptm_mdm2"]) for r in rows if r.get("iptm_mdm2")]

        def med(lst):
            return statistics.median(lst) if lst else 0

        result = {
            "total_candidates": len(rows),
            # 七层通过计数（v5 主判定）
            "l1_pass": sum(1 for r in rows if r.get("l1_pass") == "True"),
            "l2_pass": sum(1 for r in rows if r.get("l2_pass") == "True"),
            "l3_pass": sum(1 for r in rows if r.get("l3_pass") == "True"),
            "l4_pass": sum(1 for r in rows if r.get("l4_pass") == "True"),
            "l5_pass": sum(1 for r in rows if r.get("l5_pass") == "True"),
            "l6_pass": sum(1 for r in rows if r.get("l6_pass") == "True"),
            "l7_pass": sum(1 for r in rows if r.get("l7_pass") == "True"),
            "all_layers_pass": sum(1 for r in rows if r.get("all_layers_pass") == "True"),
            "synth_pass": sum(1 for r in rows if r.get("synth_pass") == "True"),
            "pareto_front": sum(1 for r in rows if r.get("pareto_front") == "True"),
            "finalized": sum(1 for r in rows if r.get("final_status") == "finalized"),
            # 主指标中位数（v5: ipSAE 替代 ipTM）
            "ipsae_mdm2_median": round(med(ipsae_m2), 3),
            "ipsae_mdmx_median": round(med(ipsae_mx), 3),
            "dg_mdm2_median": round(med(dg_m2), 3),
            "scrmsd_median": round(med(scrmsds), 3),
            "iptm_mdm2_median": round(med(iptm_m2), 3),  # 参考
        }
        project_config = State.load().get("project_config") or State._project_config
        result["target_metrics"] = {}
        for target in required_target_ids(project_config):
            metric_summary = {}
            for metric in ("ipsae", "dg", "sc", "dsasa", "pose_rmsd"):
                values = [
                    value for value in (_f(target_value(row, target, metric)) for row in rows)
                    if value is not None
                ]
                metric_summary[f"{metric}_median"] = round(med(values), 3)
                metric_summary[f"{metric}_n"] = len(values)
            result["target_metrics"][target] = metric_summary
        return result


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


# ============================================================
# v5: 七层指标电池判定器（所有 Agent 共用）
# ============================================================
def _cmp(value: float, op: str, threshold: float) -> bool:
    """安全比较（value 对 op threshold）。值缺失返回 False。"""
    if value is None or value == "" or threshold is None:
        return False
    ops = {">": lambda a, b: a > b,
           "<": lambda a, b: a < b,
           ">=": lambda a, b: a >= b,
           "<=": lambda a, b: a <= b}
    return ops.get(op, lambda a, b: False)(float(value), float(threshold))


def _f(v):
    """尝试转 float，失败返回 None。"""
    if v is None or v == "": return None
    try: return float(v)
    except (TypeError, ValueError): return None


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def _threshold_has_value(entry: dict) -> bool:
    return bool(entry and entry.get("value") is not None)


def _threshold_is_justified(entry: dict) -> tuple[bool, str]:
    """Audit whether an entry may support final competition clearance.

    Provisional numbers remain usable for metric exploration, but they cannot
    turn ``competition_clearance`` true until evidence or calibration exists.
    """
    if not _threshold_has_value(entry):
        return False, "missing_value"
    source = str(entry.get("source") or "").strip()
    if not source:
        return False, "missing_source"
    calibration = str(entry.get("calibration_status") or "").casefold()
    if calibration in {"calibrated", "validated", "complete"}:
        return True, "calibrated"
    grade = str(entry.get("evidence_grade") or entry.get("grade") or "").casefold()
    if grade in {
        "paper_explicit", "method_explicit", "design_rule",
        "field_consensus", "positive_control", "empirical_null",
    }:
        return True, grade
    return False, grade or calibration or "ungraded"


def evaluate_battery(
    c: dict,
    thresholds: dict | None = None,
    required_targets: tuple[str, ...] | None = None,
) -> dict:
    """
    七层指标电池判定（v5 主判定）。

    导师要求（DeeCamp）：七层全清才算成功，缺一不可。每层阈值须有出处
    （来自 thresholds 由 Research Agent 文献检索+正对照标定填入）。

    参数:
      c: 候选 dict（含七层指标字段，旧名自动 alias）
      thresholds: 来自 state.json["thresholds"]
      required_targets: 本次判定必须覆盖的靶标。None 时从 project_config
                        读取；可传任意合法靶点 ID，不再限制 MDM2/MDMX。

    返回:
      {l1_pass..l7_pass: bool, all_layers_pass: bool,
       failed_layers: list[str],
       layer_values: dict,  # 每层主值 }
    """

    c, th, targets = _normalize_battery_context(c, thresholds, required_targets)

    l1 = _battery_l1(c, th)
    l2, l2_by_target = _battery_l2(c, th, targets)
    l3, l3_by_target, method_ok = _battery_l3(c, th, targets)
    l4, nc_pre, nc_post = _battery_l4(c, th)
    l5, l5_by_target = _battery_l5(c, th, targets)
    l6, l6_by_target = _battery_l6(c, th, targets)
    l7 = _battery_l7(c, th)

    layer_pass = {
        "l1_pass": bool(l1), "l2_pass": bool(l2),
        "l3_pass": bool(l3), "l4_pass": bool(l4),
        "l5_pass": bool(l5), "l6_pass": bool(l6),
        "l7_pass": bool(l7),
    }
    failed = [k for k, v in layer_pass.items() if not v]

    threshold_audit, all_thresholds_justified = _battery_threshold_audit(th, targets)
    missing_evidence = _battery_missing_evidence(c, targets)
    missing_thresholds = [
        name for name, item in threshold_audit.items() if item["reason"] == "missing_value"
    ]

    hard_failures = []
    if (
        nc_pre is not None
        and nc_post is not None
        and _threshold_has_value(th.get("L4_nc_term_dist") or {})
        and not l4
    ):
        hard_failures.append("ring_closure_geometry")
    if hard_failures:
        triage_status = "invalid"
    elif missing_evidence or missing_thresholds:
        triage_status = "needs_more_evidence"
    elif not failed:
        triage_status = "shortlisted"
    else:
        triage_status = "needs_optimization"

    return {
        **layer_pass,
        "all_layers_pass": len(failed) == 0,
        "metric_clearance": len(failed) == 0,
        "competition_clearance": len(failed) == 0 and all_thresholds_justified,
        "all_thresholds_justified": all_thresholds_justified,
        "threshold_audit": threshold_audit,
        "triage_status": triage_status,
        "hard_failures": hard_failures,
        "missing_evidence": missing_evidence,
        "missing_thresholds": missing_thresholds,
        "failed_layers": failed,
        "required_targets": list(targets),
        "target_pass": {
            target: {
                "l2_pass": l2_by_target[target],
                "l3_pass": l3_by_target[target] and method_ok,
                "l5_pass": l5_by_target[target],
                "l6_pass": l6_by_target[target],
            }
            for target in targets
        },
        "layer_values": {
            "L1_plddt": _f(global_value(c, "plddt")),
            **{
                f"L2_ipsae_{target_slug(target)}": _f(target_value(c, target, "ipsae"))
                for target in targets
            },
            **{
                f"L3_dg_{target_slug(target)}": _f(target_value(c, target, "dg"))
                for target in targets
            },
            **{
                f"L3_sc_{target_slug(target)}": _f(target_value(c, target, "sc"))
                for target in targets
            },
            **{
                f"L3_dsasa_{target_slug(target)}": _f(target_value(c, target, "dsasa"))
                for target in targets
            },
            "L4_nc_distance_pre": nc_pre,
            "L4_nc_distance_post": nc_post,
            **{
                f"L5_hotspot_cov_{target_slug(target)}": _f(target_value(c, target, "hotspot_cov"))
                for target in targets
            },
            **{
                f"L6_pose_rmsd_{target_slug(target)}": _f(
                    target_value(c, target, "pose_rmsd")
                    if target_value(c, target, "pose_rmsd") not in (None, "")
                    else c.get("pose_rmsd") if len(targets) == 1 else None
                )
                for target in targets
            },
            "L7_scrmsd": _f(global_value(c, "scrmsd")),
        },
    }


def _normalize_battery_context(
    c: dict,
    thresholds: dict | None,
    required_targets: tuple[str, ...] | None,
) -> tuple[dict, dict, tuple[str, ...]]:
    c = _alias_keys(dict(c))  # 旧名兜底
    th = thresholds or {}
    if required_targets is None:
        candidate_targets = c.get("required_targets")
        if candidate_targets:
            required_targets = tuple(candidate_targets)
        else:
            state = State.load()
            config = state.get("project_config") or State._project_config
            required_targets = required_target_ids(config)
    targets = tuple(str(target).strip() for target in required_targets if str(target).strip())
    if not targets:
        raise ValueError("required_targets must contain at least one target id")
    if len(set(target.casefold() for target in targets)) != len(targets):
        raise ValueError("required_targets contains duplicate target ids")
    return c, th, targets


def _battery_l1(c: dict, th: dict) -> bool:
    return (
        _cmp(
            _f(global_value(c, "plddt")),
            th.get("L1_plddt", {}).get("operator", ">"),
            th.get("L1_plddt", {}).get("value", 0.8),
        )
        if _threshold_has_value(th.get("L1_plddt") or {})
        else False
    )


def _battery_l2(c: dict, th: dict, targets: tuple[str, ...]) -> tuple[bool, dict]:
    t2_by_target = {
        target: threshold_for_target(th, "L2_ipsae", target) for target in targets
    }
    l2_by_target = {
        target: _cmp(
            _f(target_value(c, target, "ipsae")),
            t2_by_target[target].get("operator", ">"),
            t2_by_target[target].get("value"),
        )
        for target in targets
    }
    l2 = (
        all(_threshold_has_value(t2_by_target[target]) for target in targets)
        and all(l2_by_target.values())
    )
    return l2, l2_by_target


def _battery_l3(c: dict, th: dict, targets: tuple[str, ...]) -> tuple[bool, dict, bool]:
    l3_by_target = {}
    for target in targets:
        tdg = threshold_for_target(th, "L3_dg", target)
        tsc = threshold_for_target(th, "L3_sc", target)
        tdsasa = threshold_for_target(th, "L3_dsasa", target)
        l3_by_target[target] = all([
            _cmp(_f(target_value(c, target, "dg")), tdg.get("operator", "<"), tdg.get("value")),
            _cmp(_f(target_value(c, target, "sc")), tsc.get("operator", ">"), tsc.get("value")),
            _cmp(_f(target_value(c, target, "dsasa")), tdsasa.get("operator", ">"), tdsasa.get("value")),
        ])
    required_dg_method = th.get("L3_dg", {}).get("method")
    method_ok = (
        not required_dg_method
        or str(c.get("dg_method", "")).strip().casefold()
        == str(required_dg_method).strip().casefold()
    )
    l3 = (
        all(
            _threshold_has_value(threshold_for_target(th, key, target))
            for key in ("L3_dg", "L3_sc", "L3_dsasa")
            for target in targets
        )
        and method_ok
        and all(l3_by_target.values())
    )
    return l3, l3_by_target, method_ok


def _battery_l4(c: dict, th: dict) -> tuple[bool, object, object]:
    nc_pre = _f(global_value(c, "nc_distance_pre"))
    nc_post = _f(global_value(c, "nc_distance_post"))
    t4 = th.get("L4_nc_term_dist", {})
    l4 = (
        _threshold_has_value(t4)
        and _cmp(nc_pre, t4.get("operator", "<"), t4.get("value"))
        and _cmp(nc_post, t4.get("operator", "<"), t4.get("value"))
    )
    return l4, nc_pre, nc_post


def _battery_l5(c: dict, th: dict, targets: tuple[str, ...]) -> tuple[bool, dict]:
    l5_by_target = {}
    for target in targets:
        t5 = threshold_for_target(th, "L5_hotspot_coverage", target)
        specific_site = target_value(c, target, "site_consistency")
        if specific_site in (None, "") and len(targets) == 1:
            specific_site = c.get("site_consistency")
        l5_by_target[target] = (
            _cmp(
                _f(target_value(c, target, "hotspot_cov")),
                t5.get("operator", ">="),
                t5.get("value"),
            )
            and _truthy(specific_site)
        )
    l5 = (
        all(
            _threshold_has_value(threshold_for_target(th, "L5_hotspot_coverage", target))
            for target in targets
        )
        and all(l5_by_target.values())
    )
    return l5, l5_by_target


def _battery_l6(c: dict, th: dict, targets: tuple[str, ...]) -> tuple[bool, dict]:
    l6_by_target = {}
    for target in targets:
        t6 = threshold_for_target(th, "L6_pose_rmsd", target)
        min_seed_fraction = t6.get("min_seed_fraction", 0.67)
        pose = target_value(c, target, "pose_rmsd")
        seed = target_value(c, target, "seed_convergence")
        if len(targets) == 1:
            pose = pose if pose not in (None, "") else c.get("pose_rmsd")
            seed = seed if seed not in (None, "") else c.get("seed_convergence")
        l6_by_target[target] = (
            _cmp(_f(pose), t6.get("operator", "<"), t6.get("value"))
            and _f(seed) is not None
            and _f(seed) >= float(min_seed_fraction)
        )
    l6 = (
        all(
            _threshold_has_value(threshold_for_target(th, "L6_pose_rmsd", target))
            for target in targets
        )
        and all(l6_by_target.values())
    )
    return l6, l6_by_target


def _battery_l7(c: dict, th: dict) -> bool:
    t7 = th.get("L7_scrmsd", {})
    return (
        _cmp(_f(global_value(c, "scrmsd")), t7.get("operator", "<"), t7.get("value"))
        if _threshold_has_value(t7)
        else False
    )


def _battery_threshold_audit(th: dict, targets: tuple[str, ...]) -> tuple[dict, bool]:
    threshold_audit = {}
    for key in ("L1_plddt", "L4_nc_term_dist", "L7_scrmsd"):
        ok, reason = _threshold_is_justified(th.get(key) or {})
        threshold_audit[key] = {"justified": ok, "reason": reason}
    for key in ("L2_ipsae", "L3_dg", "L3_sc", "L3_dsasa", "L5_hotspot_coverage", "L6_pose_rmsd"):
        for target in targets:
            ok, reason = _threshold_is_justified(threshold_for_target(th, key, target))
            threshold_audit[f"{key}:{target}"] = {"justified": ok, "reason": reason}
    return threshold_audit, all(item["justified"] for item in threshold_audit.values())


def _battery_missing_evidence(c: dict, targets: tuple[str, ...]) -> list[str]:
    missing_evidence = []
    global_required = {
        "plddt": global_value(c, "plddt"),
        "nc_distance_pre": global_value(c, "nc_distance_pre"),
        "nc_distance_post": global_value(c, "nc_distance_post"),
        "scrmsd": global_value(c, "scrmsd"),
    }
    missing_evidence.extend(name for name, value in global_required.items() if value in (None, ""))
    for target in targets:
        for metric in (
            "ipsae", "dg", "sc", "dsasa", "hotspot_cov",
            "site_consistency", "pose_rmsd", "seed_convergence",
        ):
            value = target_value(c, target, metric)
            if value in (None, "") and not (
                len(targets) == 1
                and metric in {"site_consistency", "pose_rmsd", "seed_convergence"}
                and c.get(metric) not in (None, "")
            ):
                missing_evidence.append(f"{target}:{metric}")
    return missing_evidence

def compute_pareto_front(
    candidates: list[dict],
    objectives: tuple = ("ipsae_mdm2", "ipsae_mdmx"),
) -> list[str]:
    """Return non-dominated candidates for mixed-direction objectives.

    Backward-compatible strings mean ``maximize``. A generic objective may be
    ``{"target": "NEW1", "metric": "dg", "direction": "minimize"}`` or
    ``{"key": "scrmsd", "direction": "minimize"}``.
    """
    specs = []
    for objective in objectives:
        if isinstance(objective, str):
            specs.append({"key": objective, "direction": "maximize"})
        elif isinstance(objective, dict):
            direction = objective.get("direction", "maximize")
            if direction not in {"maximize", "minimize"}:
                raise ValueError(f"invalid Pareto direction: {direction}")
            if not objective.get("key") and not (objective.get("target") and objective.get("metric")):
                raise ValueError("Pareto objective requires key or target+metric")
            specs.append(dict(objective, direction=direction))
        else:
            raise TypeError("Pareto objectives must be strings or dictionaries")

    def objective_value(candidate: dict, spec: dict):
        if spec.get("target"):
            value = target_value(candidate, spec["target"], spec["metric"])
        else:
            value = candidate.get(spec["key"])
        number = _f(value)
        if number is None:
            return None
        return -number if spec["direction"] == "minimize" else number

    valid = []
    for candidate in candidates:
        values = tuple(objective_value(candidate, spec) for spec in specs)
        if candidate.get("candidate_id") and all(value is not None for value in values):
            valid.append((candidate["candidate_id"], values))

    front = []
    for candidate_id, values in valid:
        dominated = any(
            all(other >= current for other, current in zip(other_values, values))
            and any(other > current for other, current in zip(other_values, values))
            for other_id, other_values in valid
            if other_id != candidate_id
        )
        if not dominated:
            front.append(candidate_id)
    return front


if __name__ == "__main__":
    print("=== data_layer.py 自检 ===")
    
    # 测试State
    s = State.load()
    print(f"State loaded: phase={s['phase']}, round={s['round']}")
    
    # 测试日志
    eid = EvidenceLogger.log("system", "test", {"msg": "冒烟测试"},
                              targets=["both"], phase="research")
    print(f"Log written: event_id={eid}")
    
    # 测试候选索引
    CandidateIndex.add({
        "candidate_id": "C0001",
        "sequence": "GFEWALAAK",
        "source_route": "route_A_mdm2_first",
        "source_batch": "batch_mdm2_len10"
    })
    CandidateIndex.add({
        "candidate_id": "C0002",
        "sequence": "PFNWALGGS",
        "source_route": "route_A_mdmx_first",
        "source_batch": "batch_mdmx_len12"
    })
    
    # 模拟评分
    CandidateIndex.update_score("C0001", {
        "monomer_plddt": 0.85,
        "self_rmsd": 1.2,
        "layer1_pass": "True",
        "iptm_mdm2": 0.84,
        "iptm_mdmx": 0.72,
        "dual_score": 0.72,
        "asymmetry": 0.12
    })
    CandidateIndex.update_score("C0002", {
        "monomer_plddt": 0.78,
        "self_rmsd": 1.8,
        "layer1_pass": "True",
        "iptm_mdm2": 0.61,
        "iptm_mdmx": 0.79,
        "dual_score": 0.64,
        "asymmetry": 0.18
    })
    
    print(f"\n索引表统计: {CandidateIndex.stats()}")
    print(f"\nTop 候选: {CandidateIndex.top_n(2)}")
    print("\n日志条目数:", len(EvidenceLogger.get_all()))
    print("自检通过")
