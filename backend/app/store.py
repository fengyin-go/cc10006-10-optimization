"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from typing import Any

from app.policy_seed import AUX_SEED, POLICY_ROWS
from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 湖区分区模块改由策略库提供示例数据：同一分区范围三条策略互相冲突，
        # 页面和接口都从这份数据裁决唯一生效方案。
        self._tables["lakezone"] = [dict(row) for row in POLICY_ROWS]
        for name, rows in AUX_SEED.items():
            self._tables[name] = [dict(row) for row in rows]
        self._tables.setdefault("lake_policy_revisions", [])

    # 概览只统计业务模块；lake_partitions 等是策略裁决的内部数据表，不当作模块展示。
    INTERNAL_TABLES = {"lake_partitions", "lake_files", "lake_events", "snapshot_todos", "lake_policy_revisions"}

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in self.INTERNAL_TABLES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
