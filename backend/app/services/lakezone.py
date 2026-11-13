"""湖区分区业务规则：状态流转、策略裁决与筛选口径都收在这里。

同一个分区范围只允许一个生效策略，裁决口径见 app.services.policy；
两个入口（湖区分区页、版本快照页）都通过 effective_decision() 读取同一份生效方案。
"""
from __future__ import annotations

from typing import Any

from app.services.policy import (
    concurrent_adjust,
    effective_decision,
    evaluate_partitions,
    export_effective,
    list_events,
    list_files,
    list_partitions,
    list_revisions,
    list_scopes,
    list_todos,
    merge_partition,
    rollback,
    adjust_policy,
)
from app.store import store

MODULE = "lakezone"
REQUIRED_FIELDS = ["方案编号", "目标表名", "分区字段", "分区范围"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用方案": "已启用", "停用方案": "已停用"}
NEGATIVE_ACTIONS = ["停用方案"]


class LakezoneService:
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
            rows = [row for row in rows if keyword in str(row.get("方案编号", ""))]
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
        entry["策略类型"] = values.get("策略类型") or "热数据"
        entry["业务优先级"] = int(values.get("业务优先级") or 0)
        entry["生效时间"] = values.get("生效时间") or ""
        entry["保留天数"] = int(values.get("保留天数") or 0)
        entry["阈值数值"] = int(values.get("阈值数值") or 0)
        entry["最少文件数"] = int(values.get("最少文件数") or 0)
        entry["版本"] = "v1"
        entry["保留上限"] = int(values.get("保留上限") or 3650)
        entry["提交时间"] = ""
        entry["提交序列"] = 0
        entry["小文件合并"] = values.get("小文件合并") or "开启"
        entry["合并配置"] = values.get("合并配置") or ""
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"分区方案 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于湖区分区可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"分区方案已{action}"

    # ---- 策略裁决入口：所有读写都落在 policy 引擎，保证两个入口读同一份方案 ----

    def decision(self) -> dict[str, Any]:
        return effective_decision()

    def decision_for(self, scope_value: str | None) -> dict[str, Any]:
        if not scope_value:
            return effective_decision()
        for scope in list_scopes():
            if " / ".join(part for part in scope if part) == scope_value:
                return effective_decision(scope)
        return effective_decision()

    def scopes(self) -> list[dict[str, Any]]:
        return [
            {
                "scope": " / ".join(part for part in scope if part),
                "目标表名": scope[0],
                "分区字段": scope[1],
                "分区范围": scope[2],
            }
            for scope in list_scopes()
        ]

    def partitions(self) -> dict[str, Any]:
        return list_partitions()

    def files(self, partition: str | None = None) -> list[dict[str, Any]]:
        return list_files(partition)

    def merge(self, partition_id: int) -> tuple[bool, str, dict[str, Any] | None]:
        return merge_partition(partition_id)

    def adjust(self, payload: dict[str, Any], actor: str = "策略审批人") -> tuple[bool, str, dict[str, Any]]:
        """兼容两种写法：{"id","values"} 或前端直接提交 {"方案编号","保留天数"}。"""
        policy_id = int(payload.get("id") or payload.get("方案id") or 0)
        if policy_id == 0 and payload.get("方案编号"):
            for row in store.rows(MODULE):
                if str(row.get("方案编号")) == str(payload["方案编号"]):
                    policy_id = int(row["id"])
                    break
        nested = payload.get("values")
        values = dict(nested) if isinstance(nested, dict) else {
            key: payload[key]
            for key in (
                "保留天数", "阈值数值", "最少文件数", "业务优先级",
                "生效时间", "冷热分层", "归档路径", "小文件合并", "说明",
            )
            if key in payload
        }
        if not policy_id:
            return False, "缺少策略 id 或方案编号，无法定位要调整的方案", {}
        return adjust_policy(policy_id, values, actor)

    def concurrent(self, payload: dict[str, Any], actor: str = "策略审批人") -> tuple[bool, str, dict[str, Any]]:
        submissions = list(payload.get("submissions") or [])
        return concurrent_adjust(submissions, actor)

    def rollback(self, actor: str = "策略审批人") -> tuple[bool, str, dict[str, Any]]:
        return rollback(actor)

    def events(self) -> list[dict[str, Any]]:
        return list_events()

    def todos(self) -> list[dict[str, Any]]:
        return list_todos()

    def revisions(self) -> list[dict[str, Any]]:
        return list_revisions()

    def export_effective(self) -> dict[str, Any]:
        return export_effective()

    def recalculate(self) -> dict[str, Any]:
        return evaluate_partitions(persist=True, decision=effective_decision())
