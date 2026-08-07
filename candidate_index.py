"""Candidate index compatibility API backed by SQLite with a CSV projection.

Split from data_layer.py (PR8) so the core module stays under the
architecture-gate file-size limit.  ``data_layer.CandidateIndex`` is served
lazily via PEP 562 and re-exports the class defined here.
"""

import csv
import json
import statistics
from datetime import datetime, timezone
from typing import Optional

import data_layer as _dl
from data_layer import EvidenceLogger, State, get_storage_backend, project_candidates
from data_layer_schema import INDEX_COLUMNS, alias_keys, to_float
from project_config import required_target_ids, target_value
from storage import write_csv_projection


class CandidateIndex:
    """Candidate compatibility API backed by SQLite with a CSV projection."""

    @classmethod
    def _ensure_exists(cls):
        if not _dl.INDEX_PATH.exists():
            project_candidates()

    @classmethod
    def _migrate_schema(cls, old_header: list[str]):
        """把旧 CSV 显式迁移到当前 schema，并在同目录保留原始备份。"""
        index_path = _dl.INDEX_PATH
        with open(index_path, "r", encoding="utf-8-sig", newline="") as f:
            old_rows = list(csv.DictReader(f))

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = index_path.with_name(f"{index_path.stem}.pre_v5_{stamp}.csv")
        backup.write_bytes(index_path.read_bytes())

        migrated = []
        for old_row in old_rows:
            extra_values = old_row.pop(None, None)
            row = alias_keys(dict(old_row))
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
        write_csv_projection(_dl.INDEX_PATH, rows, INDEX_COLUMNS)

    @classmethod
    def _prepare_row(cls, row: dict) -> dict:
        row = alias_keys(dict(row))
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
        project_candidates(backend)

    @classmethod
    def add_batch(cls, rows: list[dict]):
        prepared = [cls._prepare_row(row) for row in rows]
        backend = get_storage_backend()
        backend.add_candidates(prepared)
        project_candidates(backend)

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
        scores = alias_keys(dict(scores))
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
        project_candidates(backend)

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
        project_candidates(backend)

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
            value_of = lambda row: to_float(target_value(row, target_id, metric))
        else:
            value_of = lambda row: to_float(row.get(by))
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
                    value for value in (to_float(target_value(row, target, metric)) for row in rows)
                    if value is not None
                ]
                metric_summary[f"{metric}_median"] = round(med(values), 3)
                metric_summary[f"{metric}_n"] = len(values)
            result["target_metrics"][target] = metric_summary
        return result

