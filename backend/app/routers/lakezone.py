"""湖区分区接口：维护分区方案，覆盖策略裁决、待归档重算、小文件合并与回滚导出。

注意：静态路径（/effective、/partitions、/export 等）必须注册在 /{entry_id} 之前，
否则「export」会被当成 entry_id 解析成整数而返回 422。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.lakezone import LakezoneService

router = APIRouter(prefix="/api/lakezone", tags=["湖区分区"])

service = LakezoneService()

LIST_FIELDS = ["方案编号", "目标表名", "分区字段", "分区粒度", "保留天数", "冷热分层", "归档路径", "小文件合并"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按方案编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按方案编号与状态过滤湖区分区列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export/effective")
def export_effective() -> dict[str, Any]:
    """策略审批人导出给归档团队复核：只包含当前唯一生效方案。"""
    return service.export_effective()


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出湖区分区清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "lakezone", "total": total, "items": items}


@router.get("/effective")
def effective(scope: str | None = Query(default=None, description="按“表 / 字段 / 范围”读取唯一生效方案")) -> dict[str, Any]:
    """两个入口都从这里读取同一份生效方案；冲突原因与最终方案一并返回。"""
    return service.decision_for(scope)


@router.get("/scopes")
def scopes() -> dict[str, Any]:
    """列出所有分区范围及其唯一生效方案摘要。"""
    items = []
    for item in service.scopes():
        decision = service.decision_for(item["scope"])
        items.append({
            **item,
            "生效方案": (decision.get("winner") or {}).get("方案编号"),
            "策略类型": (decision.get("winner") or {}).get("策略类型"),
            "版本": (decision.get("rule") or {}).get("版本"),
            "冲突数": len(decision.get("conflict_points") or []),
            "decision_note": decision.get("decision_note"),
        })
    return {"items": items, "total": len(items)}


@router.get("/partitions")
def partitions() -> dict[str, Any]:
    """按新口径重算待归档分区；已归档分区保持原路径。"""
    return service.partitions()


@router.get("/partitions/files")
def partition_files(partition: str | None = Query(default=None, description="分区表达式，如 dt=2025-02-01")) -> dict[str, Any]:
    """读取分区文件清单；历史清单始终保留在返回结果里。"""
    items = service.files(partition)
    return {"items": items, "total": len(items), "partition": partition}


@router.post("/partitions/{partition_id}/merge")
def merge_partition(partition_id: int) -> ActionResult:
    """小文件合并：只作用于最终方案选中的文件集合，未命中的历史清单不得消失。"""
    ok, message, entry = service.merge(partition_id)
    return ActionResult(ok=ok, message=message, entry=entry)


@router.get("/events")
def events() -> dict[str, Any]:
    items = service.events()
    return {"items": items, "total": len(items)}


@router.get("/todos")
def todos() -> dict[str, Any]:
    """跨模块同步快照待办：策略调整后由裁决引擎自动登记。"""
    items = service.todos()
    return {"items": items, "total": len(items)}


@router.get("/revisions")
def revisions() -> dict[str, Any]:
    items = service.revisions()
    return {"items": items, "total": len(items)}


@router.post("/adjust")
def adjust(payload: EntryPayload) -> ActionResult:
    """调整策略口径；保留天数超出范围直接拒绝，并以新口径重算待归档分区。"""
    ok, message, entry = service.adjust(payload.values)
    return ActionResult(ok=ok, message=message, entry=entry)


@router.post("/concurrent")
def concurrent(payload: EntryPayload) -> ActionResult:
    """同一分区范围的并发修改：只让优先级最高且生效时间最新的一条生效。"""
    ok, message, entry = service.concurrent(payload.values)
    return ActionResult(ok=ok, message=message, entry=entry)


@router.post("/rollback")
def rollback(payload: EntryPayload) -> ActionResult:
    """回滚到上一份策略快照；返回恢复旧方案的原因说明。"""
    ok, message, entry = service.rollback()
    return ActionResult(ok=ok, message=message, entry=entry)


@router.post("/recalculate")
def recalculate(payload: EntryPayload) -> ActionResult:
    """按当前唯一生效方案重算待归档分区，并把结果写入事件留痕。"""
    result = service.recalculate()
    return ActionResult(ok=True, message="已按当前唯一生效方案重算待归档分区", entry=result)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条分区方案明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"分区方案 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条分区方案，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="分区方案已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条分区方案执行提交校验、启用方案、停用方案；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or payload.action or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
