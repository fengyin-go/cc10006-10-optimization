"""版本快照业务规则：状态流转、字段校验与筛选口径都收在这里。

快照清单的“当前生效方案”不单独维护，直接读取湖区分区裁决引擎的同一份结果，
避免两个入口各算一套导致展示不一致。
"""
from __future__ import annotations

from typing import Any

from app.services.policy import effective_decision, list_todos
from app.store import store

MODULE = "snapshot"
REQUIRED_FIELDS = ["快照编号", "目标表名", "快照时间"]
STATUS_ORDER = ["草稿", "待创建", "已生效", "已过期"]
ACTION_RULES = {"提交创建": "待创建", "生效快照": "已生效", "标记过期": "已过期"}
NEGATIVE_ACTIONS = []


class SnapshotService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("快照编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"快照记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于版本快照可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"快照记录已{action}"

    def effective(self) -> dict[str, Any]:
        """与湖区分区页读同一份生效方案，不做第二次裁决。"""
        decision = effective_decision()
        return {
            "source": "app.services.policy",
            "scope": decision.get("scope"),
            "scope_fields": decision.get("scope_fields"),
            "decision_note": decision.get("decision_note"),
            "basis": decision.get("basis"),
            "winner": decision.get("winner"),
            "rule": decision.get("rule"),
            "conflict_points": decision.get("conflict_points"),
            "candidates": decision.get("candidates"),
            "as_of": decision.get("as_of"),
        }

    def todos(self) -> list[dict[str, Any]]:
        return list_todos()
