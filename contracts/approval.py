"""Approval artifact contract, retaining the existing security semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class Approval:
    plan_id: str
    plan_path: str
    plan_sha256: str
    project_id: str
    approved_task_ids: tuple[str, ...]
    approver: str
    justification: str
    budget_limits: Mapping[str, int]
    approval_id: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in (
            "plan_id",
            "plan_path",
            "plan_sha256",
            "project_id",
            "approver",
            "justification",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be non-empty")
        object.__setattr__(self, "approved_task_ids", tuple(self.approved_task_ids))
        object.__setattr__(self, "budget_limits", dict(self.budget_limits))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "approval_id": self.approval_id,
            "plan_id": self.plan_id,
            "plan_path": self.plan_path,
            "plan_sha256": self.plan_sha256,
            "project_id": self.project_id,
            "approved_task_ids": list(self.approved_task_ids),
            "approver": self.approver,
            "justification": self.justification,
            "budget_limits": dict(self.budget_limits),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Approval":
        return cls(
            schema_version=value.get("schema_version", 1),
            approval_id=value.get("approval_id"),
            plan_id=value.get("plan_id"),
            plan_path=value.get("plan_path"),
            plan_sha256=value.get("plan_sha256"),
            project_id=value.get("project_id"),
            approved_task_ids=tuple(value.get("approved_task_ids", ())),
            approver=value.get("approver"),
            justification=value.get("justification", ""),
            budget_limits=dict(value.get("budget_limits", {})),
        )
