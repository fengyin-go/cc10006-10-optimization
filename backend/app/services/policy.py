"""湖区分区策略裁决引擎。

同一分区范围（目标表名 + 分区字段 + 分区范围）只允许一个生效策略：
先比较业务优先级，优先级相同时比较生效时间，仍然相同再比较提交序列。
待归档分区严格按生效策略的保留天数、阈值和判定规则重算；已归档分区保留
归档当时的路径与口径，不参与重算。所有入口都调用这里，保证读到同一份方案。
"""
from __future__ import annotations

import copy
import threading
from datetime import date, datetime
from typing import Any

from app.store import store

POLICY_MODULE = "lakezone"
PARTITION_MODULE = "lake_partitions"
FILE_MODULE = "lake_files"
EVENT_MODULE = "lake_events"
TODO_MODULE = "snapshot_todos"
REVISION_MODULE = "lake_policy_revisions"

SCOPE_FIELDS = ["目标表名", "分区字段", "分区范围"]
DEFAULT_RETENTION_LIMIT = 3650
APPLY_FIELDS = [
    "保留天数", "阈值数值", "最少文件数", "业务优先级", "生效时间",
    "冷热分层", "归档路径", "小文件合并", "说明",
]

_lock = threading.RLock()
_history: list[dict[str, Any]] = []
_sequence = {"revision": 0, "submit": 0, "event": 0, "todo": 0}
_last_recalc: dict[str, Any] = {}


def today() -> date:
    return date.today()


def now_text() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _next(prefix: str) -> int:
    _sequence[prefix] = _sequence.get(prefix, 0) + 1
    return _sequence[prefix]


def _scope_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("目标表名") or ""),
        str(row.get("分区字段") or ""),
        str(row.get("分区范围") or ""),
    )


def _rank(row: dict[str, Any]) -> tuple[int, str, int]:
    """排序键：业务优先级 > 生效时间 > 提交序列。"""
    return (
        int(row.get("业务优先级") or 0),
        str(row.get("生效时间") or ""),
        int(row.get("提交序列") or 0),
    )


def _next_version(version: Any) -> str:
    text = str(version or "v1")
    try:
        return f"v{int(text.lstrip('v')) + 1}"
    except ValueError:
        return "v2"


def _active_policies(scope: tuple[str, str, str]) -> list[dict[str, Any]]:
    """只有“已启用”的候选参与裁决；草稿与已停用不占用唯一生效名额。"""
    return [
        row for row in store.rows(POLICY_MODULE)
        if _scope_key(row) == scope and str(row.get("status")) == "已启用"
    ]


def _sync_rule_fields(policy: dict[str, Any]) -> None:
    retention = int(policy.get("保留天数") or 0)
    threshold = int(policy.get("阈值数值") or 0)
    min_files = int(policy.get("最少文件数") or 0)
    policy["阈值"] = f"单文件≤{threshold}MB 且 文件数≥{min_files}"
    policy["判定规则"] = f"距今天数 > 保留天数({retention}) → 待归档"
    policy["合并配置"] = f"开启，单文件≤{threshold}MB，文件数≥{min_files}"


def _candidate_payload(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "方案编号": policy.get("方案编号"),
        "策略类型": policy.get("策略类型"),
        "作用范围": " / ".join(part for part in _scope_key(policy) if part),
        "目标表名": policy.get("目标表名"),
        "分区字段": policy.get("分区字段"),
        "分区范围": policy.get("分区范围"),
        "业务优先级": int(policy.get("业务优先级") or 0),
        "生效时间": policy.get("生效时间"),
        "提交时间": policy.get("提交时间"),
        "提交序列": int(policy.get("提交序列") or 0),
        "保留天数": int(policy.get("保留天数") or 0),
        "阈值": policy.get("阈值"),
        "阈值数值": int(policy.get("阈值数值") or 0),
        "最少文件数": int(policy.get("最少文件数") or 0),
        "判定规则": policy.get("判定规则"),
        "冷热分层": policy.get("冷热分层"),
        "归档路径": policy.get("归档路径"),
        "小文件合并": policy.get("小文件合并"),
        "合并配置": policy.get("合并配置"),
        "版本": policy.get("版本"),
        "状态": policy.get("status"),
        "提交人": policy.get("提交人"),
        "说明": policy.get("说明"),
    }


def _rule_from(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "保留天数": int(policy.get("保留天数") or 0),
        "阈值": policy.get("阈值"),
        "阈值数值": int(policy.get("阈值数值") or 0),
        "最少文件数": int(policy.get("最少文件数") or 0),
        "判定规则": policy.get("判定规则"),
        "冷热分层": policy.get("冷热分层"),
        "归档路径": policy.get("归档路径"),
        "小文件合并": policy.get("小文件合并"),
        "合并配置": policy.get("合并配置"),
        "版本": policy.get("版本"),
        "业务优先级": int(policy.get("业务优先级") or 0),
        "生效时间": policy.get("生效时间"),
    }


def _conflict_points(candidate: dict[str, Any], winner: dict[str, Any]) -> list[str]:
    points: list[str] = []
    cp, wp = int(candidate["业务优先级"]), int(winner["业务优先级"])
    if cp != wp:
        points.append(f"业务优先级 {cp} 低于最终方案的 {wp}")
    elif str(candidate["生效时间"]) != str(winner["生效时间"]):
        order = "早于" if str(candidate["生效时间"]) < str(winner["生效时间"]) else "晚于"
        points.append(
            f"业务优先级同为 {wp}，生效时间 {candidate['生效时间']} {order}最终方案的 {winner['生效时间']}"
        )
    if int(candidate["保留天数"]) != int(winner["保留天数"]):
        points.append(f"保留天数 {candidate['保留天数']} 天与最终方案 {winner['保留天数']} 天不一致")
    if candidate.get("阈值") != winner.get("阈值"):
        points.append(f"合并阈值「{candidate.get('阈值')}」与最终方案「{winner.get('阈值')}」不一致")
    if candidate.get("判定规则") != winner.get("判定规则"):
        points.append("分区判定规则与最终方案不一致")
    if candidate.get("冷热分层") != winner.get("冷热分层"):
        points.append(f"冷热分层「{candidate.get('冷热分层')}」与最终方案「{winner.get('冷热分层')}」不一致")
    if candidate.get("归档路径") != winner.get("归档路径"):
        points.append("归档路径与最终方案不一致")
    if candidate.get("策略类型") != winner.get("策略类型"):
        points.append(f"策略类型「{candidate.get('策略类型')}」被「{winner.get('策略类型')}」覆盖")
    return points


def resolve_scope(scope: tuple[str, str, str], as_of: date | None = None) -> dict[str, Any]:
    """按业务优先级与生效时间裁决一个分区范围内的唯一生效方案。"""
    policies = _active_policies(scope)
    candidates = sorted((_candidate_payload(row) for row in policies), key=_rank, reverse=True)
    base = {
        "scope": " / ".join(part for part in scope if part),
        "scope_fields": dict(zip(SCOPE_FIELDS, scope)),
        "as_of": (as_of or today()).isoformat(),
        "arbitrated_at": now_text(),
        "basis": ["业务优先级降序", "优先级相同取生效时间最新", "仍相同取提交序列最大"],
    }
    if not candidates:
        return {
            **base,
            "candidates": [],
            "winner": None,
            "rule": None,
            "conflict_points": [],
            "decision_note": "该分区范围没有可用策略",
        }
    winner = candidates[0]
    winner_policy = next(
        (row for row in policies if str(row.get("方案编号")) == str(winner["方案编号"])), {}
    )
    conflict_candidates = []
    for candidate in candidates:
        points = _conflict_points(candidate, winner)
        conflict_candidates.append({**candidate, "是否生效": not points, "冲突点": points})
    conflicts = [
        {"方案编号": item["方案编号"], "策略类型": item["策略类型"], "冲突点": item["冲突点"]}
        for item in conflict_candidates if item["冲突点"]
    ]
    if conflicts:
        note = (
            f"同一分区范围 {winner['作用范围']} 共命中 {len(candidates)} 条策略；"
            f"按业务优先级（{winner['业务优先级']}）与生效时间（{winner['生效时间']}）"
            f"裁决 {winner['方案编号']}（{winner['策略类型']}，{winner['版本']}）为唯一生效方案。"
        )
    else:
        note = f"分区范围 {winner['作用范围']} 仅有 {winner['方案编号']} 一条策略，无冲突。"
    return {
        **base,
        "candidates": conflict_candidates,
        "winner": {**winner, "是否生效": True, "冲突点": []},
        "rule": _rule_from(winner_policy),
        "conflict_points": conflicts,
        "decision_note": note,
    }


def list_scopes() -> list[tuple[str, str, str]]:
    return sorted({_scope_key(row) for row in store.rows(POLICY_MODULE) if row.get("分区范围")})


def effective_decision(scope: tuple[str, str, str] | None = None) -> dict[str, Any]:
    scopes = list_scopes()
    if not scopes:
        return resolve_scope(("", "", ""))
    return resolve_scope(scope or scopes[0])


def _judgement(retention: int, days: int) -> tuple[str, bool]:
    if days > retention:
        return f"距今天数({days}) > 保留天数({retention}) → 待归档", True
    return f"距今天数({days}) ≤ 保留天数({retention}) → 保留", False


def _archive_path(rule: dict[str, Any], partition: dict[str, Any]) -> str:
    template = str(rule.get("归档路径") or "")
    return (
        template.replace("{version}", str(rule.get("版本") or ""))
        .replace("{retention}", str(rule.get("保留天数") or ""))
        .replace("{partition}", str(partition.get("分区") or ""))
    )


def _scope_decisions() -> list[dict[str, Any]]:
    """每个分区范围各自的裁决结果，便于一张表里放多个范围。"""
    return [resolve_scope(scope) for scope in list_scopes()]


def _decision_for_partition(row: dict[str, Any], fallback: dict[str, Any], decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """优先用与分区同表同字段的范围裁决；没有就退回当前主方案。"""
    matches = [
        decision for decision in decisions
        if (decision.get("scope_fields") or {}).get("目标表名") == row.get("目标表名")
        and (decision.get("scope_fields") or {}).get("分区字段") == row.get("分区字段")
        and decision.get("winner")
    ]
    if not matches:
        return fallback
    return max(matches, key=lambda item: _rank(item["winner"]))


def evaluate_partitions(*, persist: bool = False, decision: dict[str, Any] | None = None) -> dict[str, Any]:
    """按当前唯一生效方案重算待归档分区；已归档分区保持原路径与归档时口径。"""
    decision = decision or effective_decision()
    if decision.get("winner") is None:
        return {
            "summary": {
                "方案": None,
                "版本": None,
                "保留天数": None,
                "阈值": None,
                "判定规则": None,
                "待归档分区数": 0,
                "保留分区数": 0,
                "已归档分区数": sum(1 for row in store.rows(PARTITION_MODULE) if row.get("是否已归档")),
                "判定发生变化": [],
                "重算基准日": today().isoformat(),
                "说明": "当前没有唯一生效方案，待归档分区不做重算",
            },
            "partitions": list(store.rows(PARTITION_MODULE)),
            "decision": decision,
            "decision_brief": None,
        }
    rule = decision.get("rule") or {}
    retention = int(rule.get("保留天数") or 0)
    per_scope = _scope_decisions() if len(list_scopes()) > 1 else [decision]
    as_of = date.fromisoformat(str(decision.get("as_of") or today().isoformat()))
    rows = store.rows(PARTITION_MODULE)
    changed: list[dict[str, Any]] = []
    archived: list[dict[str, Any]] = []
    for row in rows:
        days = (as_of - date.fromisoformat(str(row.get("分区日期")))).days
        row["距今天数"] = days
        if row.get("是否已归档"):
            row["当前判定"] = "已归档（按原策略保留）"
            row["是否待归档"] = False
            row["当前方案"] = row.get("归档时策略")
            row["当前版本"] = row.get("归档时版本")
            row["当前保留天数"] = row.get("归档时保留天数")
            row["当前阈值"] = row.get("归档时阈值")
            row["当前判定规则"] = row.get("归档时判定规则")
            row["处理说明"] = "历史已归档：保持原归档路径与归档时策略，不参与新口径重算"
            archived.append(row)
            continue
        row_decision = _decision_for_partition(row, decision, per_scope)
        row_rule = row_decision.get("rule") or rule
        row_retention = int(row_rule.get("保留天数") or retention)
        rule_text, pending = _judgement(row_retention, days)
        before = row.get("当前判定")
        row["当前方案"] = row_decision["winner"]["方案编号"]
        row["当前版本"] = row_rule.get("版本")
        row["当前保留天数"] = row_retention
        row["当前阈值"] = row_rule.get("阈值")
        row["当前判定规则"] = rule_text
        row["当前判定"] = "待归档" if pending else "保留"
        row["是否待归档"] = pending
        row["计划归档路径"] = _archive_path(row_rule, row) if pending else None
        # 未归档分区的路径永远跟随当前生效方案；已归档分区在上面的分支里保持原路径。
        row["归档路径"] = row["计划归档路径"]
        if before is not None and before != row["当前判定"]:
            changed.append({"分区": row.get("分区"), "原判定": before, "新判定": row["当前判定"]})
    summary = {
        "方案": decision["winner"]["方案编号"],
        "版本": rule.get("版本"),
        "保留天数": retention,
        "阈值": rule.get("阈值"),
        "判定规则": rule.get("判定规则"),
        "待归档分区数": sum(1 for row in rows if row.get("是否待归档")),
        "保留分区数": sum(1 for row in rows if not row.get("是否已归档") and not row.get("是否待归档")),
        "已归档分区数": len(archived),
        "判定发生变化": changed,
        "重算基准日": as_of.isoformat(),
    }
    if persist:
        _last_recalc.clear()
        _last_recalc.update({
            "time": now_text(),
            "summary": copy.deepcopy(summary),
        })
        _append_event(
            "待归档重算",
            str(decision["winner"]["方案编号"]),
            "策略审批人",
            f"按 {decision['winner']['方案编号']} {rule.get('版本')}（保留 {retention} 天，{rule.get('判定规则')}）"
            f"重算待归档分区；判定变化 {len(changed)} 个，{len(archived)} 个已归档分区保持原路径",
            f"待归档 {summary['待归档分区数']} 个，保留 {summary['保留分区数']} 个"
            + (
                "；变化："
                + "；".join(
                    "{分区} {原判定}→{新判定}".format(**item) for item in changed
                )
                if changed else ""
            ),
        )
    return {
        "summary": summary,
        "partitions": rows,
        "decision": decision,
        # 文件清单只保留画面上需要的字段，避免每条分区都回传整份决策。
        "decision_brief": {
            "方案": decision["winner"]["方案编号"],
            "策略类型": decision["winner"]["策略类型"],
            "版本": rule.get("版本"),
            "保留天数": retention,
            "阈值": rule.get("阈值"),
            "判定规则": rule.get("判定规则"),
        },
    }


def list_partitions() -> dict[str, Any]:
    result = evaluate_partitions(persist=False)
    result["last_recalc"] = copy.deepcopy(_last_recalc)
    return result


def list_files(partition: str | None = None) -> list[dict[str, Any]]:
    rows = store.rows(FILE_MODULE)
    if partition:
        rows = [row for row in rows if str(row.get("分区")) == partition]
    return rows


def merge_partition(partition_id: int) -> tuple[bool, str, dict[str, Any] | None]:
    """只合并最终生效方案选中的文件；未命中的历史清单保留在清单中并标注原因。"""
    partition = store.find(PARTITION_MODULE, partition_id)
    if partition is None:
        return False, f"待归档分区 {partition_id} 不存在", None
    decision = effective_decision()
    rule = decision.get("rule") or {}
    if decision.get("winner") is None:
        return False, "当前没有唯一生效方案，合并动作不允许执行", None
    if str(rule.get("小文件合并") or "开启") != "开启":
        return False, (
            f"最终方案 {decision['winner']['方案编号']} 未开启小文件合并，合并动作不允许执行；文件清单保持原样"
        ), None
    threshold = int(rule.get("阈值数值") or 0)
    min_files = int(rule.get("最少文件数") or 0)
    files = [row for row in store.rows(FILE_MODULE) if str(row.get("分区")) == str(partition.get("分区"))]
    candidates = [
        row for row in files
        if not row.get("是否历史清单")
        and str(row.get("处理状态")) != "已合并"
        and float(row.get("大小MB") or 0) <= threshold
    ]
    batch = f"MERGE-{datetime.now().strftime('%Y%m%d%H%M%S')}-{len(files)}"
    if len(candidates) < min_files:
        if partition.get("合并批次"):
            return False, (
                f"{partition.get('分区')} 已在批次 {partition['合并批次']} 按最终方案合并，"
                "没有新的待合并文件；清单中的历史与未命中记录保持不变"
            ), None
        return False, (
            f"最终方案 {decision['winner']['方案编号']} {rule.get('版本')} 要求命中文件数≥{min_files}，"
            f"当前命中 {len(candidates)} 个，合并未执行；文件清单保持原样"
        ), None
    selected_ids = {int(row["id"]) for row in candidates}
    for row in files:
        if int(row["id"]) in selected_ids:
            row["命中最终方案"] = True
            row["命中方案"] = decision["winner"]["方案编号"]
            row["命中版本"] = rule.get("版本")
            row["命中阈值"] = f"单文件≤{threshold}MB 且 文件数≥{min_files}"
            row["合并批次"] = batch
            row["处理状态"] = "已合并"
            row["未命中原因"] = None
        else:
            row["命中最终方案"] = False
            if row.get("是否历史清单"):
                row["未命中原因"] = "历史清单不参与合并，保留在原始清单中"
            elif str(row.get("处理状态")) == "已合并":
                row["未命中原因"] = "已在上一次合并中处理，保留记录"
            else:
                row["未命中原因"] = (
                    f"大小 {row.get('大小MB')}MB 超出最终方案单文件阈值 {threshold}MB，不在选中文件集合"
                )
    partition["合并状态"] = "已按最终方案合并"
    partition["合并批次"] = batch
    partition["合并时间"] = now_text()
    partition["合并文件集合"] = sorted(selected_ids)
    unmatched = [row for row in files if int(row["id"]) not in selected_ids]
    _append_event(
        "小文件合并",
        str(decision["winner"]["方案编号"]),
        "策略审批人",
        f"{partition.get('分区')} 合并只作用于最终方案选中的 {len(selected_ids)} 个文件（单文件≤{threshold}MB）；"
        f"未命中 {len(unmatched)} 个文件（含历史清单）全部保留并标注原因",
        f"批次 {batch}，选中 {len(selected_ids)} 个，未命中 {len(unmatched)} 个",
    )
    return True, (
        f"已按 {decision['winner']['方案编号']} {rule.get('版本')} 合并 {len(selected_ids)} 个文件，"
        f"批次 {batch}；未命中的 {len(unmatched)} 个文件（含历史清单）仍在清单中"
    ), {
        "batch": batch,
        "selected_file_ids": sorted(selected_ids),
        "unmatched_file_ids": sorted(int(row["id"]) for row in unmatched),
        "files": files,
        "decision": decision,
    }


def _append_event(event_type: str, policy_no: str, actor: str, description: str, result: str) -> dict[str, Any]:
    rows = store.rows(EVENT_MODULE)
    entry_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
    entry = {
        "id": entry_id,
        "事件编号": f"EVT-{entry_id:04d}",
        "时间": now_text(),
        "事件类型": event_type,
        "方案编号": policy_no,
        "操作人": actor,
        "说明": description,
        "结果": result,
    }
    rows.append(entry)
    return entry


def list_events() -> list[dict[str, Any]]:
    return list(store.rows(EVENT_MODULE))


def _append_todo(decision: dict[str, Any], action: str, revision_no: int | None = None) -> dict[str, Any]:
    winner = decision.get("winner") or {}
    rule = decision.get("rule") or {}
    rows = store.rows(TODO_MODULE)
    partitions = store.rows(PARTITION_MODULE)
    pending = sum(1 for row in partitions if row.get("是否待归档"))
    kept = sum(1 for row in partitions if not row.get("是否已归档") and not row.get("是否待归档"))
    archived = sum(1 for row in partitions if row.get("是否已归档"))
    entry_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
    entry = {
        "id": entry_id,
        "待办编号": f"SYNC-{entry_id:04d}",
        "目标表名": winner.get("目标表名") or "orders",
        "关联方案": winner.get("方案编号"),
        "待办类型": action,
        "摘要": (
            f"将 {winner.get('目标表名') or '—'} 当前唯一生效方案 {winner.get('方案编号')} {rule.get('版本')}"
            f"（保留 {rule.get('保留天数')} 天，{rule.get('阈值')}）同步至版本快照清单；"
            f"覆盖 {pending} 个待归档、{kept} 个保留、{archived} 个已归档分区"
        ),
        "状态": "待处理",
        "创建时间": now_text(),
        "来源": "湖区分区裁决",
        "方案版本": rule.get("版本"),
        "revision": revision_no,
    }
    rows.append(entry)
    return entry


def list_todos() -> list[dict[str, Any]]:
    return list(store.rows(TODO_MODULE))


def _snapshot_history(decision: dict[str, Any], reason: str) -> None:
    _history.append({
        "time": now_text(),
        "decision": copy.deepcopy(decision),
        "policies": copy.deepcopy(store.rows(POLICY_MODULE)),
        "reason": reason,
    })


def _validate_values(policy: dict[str, Any], values: dict[str, Any]) -> str | None:
    if "保留天数" in values:
        try:
            retention = int(values.get("保留天数"))
        except (TypeError, ValueError):
            return f"保留天数「{values.get('保留天数')}」不是整数，已拒绝"
        limit = int(policy.get("保留上限") or DEFAULT_RETENTION_LIMIT)
        if retention <= 0:
            return f"保留天数 {retention} 超出范围：必须大于 0 天，已拒绝"
        if retention > limit:
            return f"保留天数 {retention} 超出范围：当前方案上限 {limit} 天，已拒绝"
    for field, label in (("阈值数值", "阈值数值"), ("最少文件数", "最少文件数")):
        if field in values:
            try:
                number = int(values.get(field))
            except (TypeError, ValueError):
                return f"{label}「{values.get(field)}」不是整数，已拒绝"
            if number <= 0:
                return f"{label} {number} 超出范围：必须大于 0，已拒绝"
    if "业务优先级" in values:
        try:
            priority = int(values.get("业务优先级"))
        except (TypeError, ValueError):
            return f"业务优先级「{values.get('业务优先级')}」不是整数，已拒绝"
        if not 0 <= priority <= 1000:
            return f"业务优先级 {priority} 超出范围：允许 0-1000，已拒绝"
    if "生效时间" in values and not str(values.get("生效时间") or "").strip():
        return "生效时间不能为空，已拒绝"
    return None


def _overlay(policy: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(policy)
    for field in APPLY_FIELDS:
        if field in values and values[field] not in (None, ""):
            if field in ("保留天数", "阈值数值", "最少文件数", "业务优先级"):
                merged[field] = int(values[field])
            else:
                merged[field] = values[field]
    if int(merged.get("保留天数") or 0) != int(policy.get("保留天数") or 0) or any(
        field in values for field in ("阈值数值", "最少文件数")
    ):
        _sync_rule_fields(merged)
    return merged


def _record_revision(
    policy: dict[str, Any],
    values: dict[str, Any],
    actor: str,
    validation_error: str | None,
    concurrent_group: str | None = None,
) -> dict[str, Any]:
    def as_int(field: str, fallback: Any) -> int:
        try:
            return int(values.get(field, fallback) or 0)
        except (TypeError, ValueError):
            # 非法值已经由校验阶段拦下，这里只做容错展示，不参与裁决。
            return 0

    rows = store.rows(REVISION_MODULE)
    revision = {
        "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
        "修订编号": f"REV-{_next('revision'):04d}",
        "方案编号": policy.get("方案编号"),
        "策略类型": policy.get("策略类型"),
        "目标表名": policy.get("目标表名"),
        "分区字段": policy.get("分区字段"),
        "分区范围": policy.get("分区范围"),
        "业务优先级": as_int("业务优先级", policy.get("业务优先级")),
        "生效时间": values.get("生效时间") or policy.get("生效时间") or now_text(),
        "保留天数": as_int("保留天数", policy.get("保留天数")),
        "阈值数值": as_int("阈值数值", policy.get("阈值数值")),
        "最少文件数": as_int("最少文件数", policy.get("最少文件数")),
        "提交时间": now_text(),
        "提交序列": _next("submit"),
        "提交人": actor,
        "并发组": concurrent_group,
        "提交值": copy.deepcopy(values),
        "校验失败": validation_error,
        "是否采纳": False,
        "未采纳原因": validation_error,
    }
    rows.append(revision)
    return revision


def _apply_revisions(
    revisions: list[dict[str, Any]], actor: str, *, reason: str, action: str
) -> tuple[bool, str, dict[str, Any]]:
    """把同一批提交按“优先级 > 生效时间 > 提交序列”裁决。

    两条约束同时成立：
    1. 同一分区范围只允许一个生效策略，胜出的那一份写回方案并重算分区；
    2. 同一条修订只改动它自己的方案，落选修订不写回，但原因会留在修订记录里。
    """
    valid = [revision for revision in revisions if not revision["校验失败"]]
    if not valid:
        return False, revisions[0]["校验失败"] or "本批提交没有可落盘的修订", {
            "decision": effective_decision(), "revisions": revisions,
        }
    scopes = {_scope_key(revision) for revision in valid}
    if len(scopes) != 1:
        return False, "并发提交必须落在同一个分区范围", {"decision": effective_decision(), "revisions": revisions}
    scope = next(iter(scopes))
    before = resolve_scope(scope)
    # 先用旧方案跑一遍，记下调整前的判定，后续重算才能给出真实的“判定发生变化”。
    evaluate_partitions(persist=False, decision=before)

    # 每个方案只取本批排序最高的有效修订；未启用的方案不参与裁决。
    by_policy: dict[str, dict[str, Any]] = {}
    for revision in valid:
        base = next(
            (row for row in store.rows(POLICY_MODULE)
             if str(row.get("方案编号")) == str(revision["方案编号"])),
            None,
        )
        if base is None:
            revision["未采纳原因"] = "方案不存在，无法参与裁决"
            continue
        if str(base.get("status")) != "已启用":
            revision["未采纳原因"] = (
                f"方案处于{base.get('status')}状态，不参与唯一生效方案裁决，调整未落盘"
            )
            continue
        key = str(revision["方案编号"])
        if key not in by_policy or _rank(revision) > _rank(by_policy[key]):
            by_policy[key] = revision

    candidates: list[dict[str, Any]] = []
    for policy in _active_policies(scope):
        revision = by_policy.get(str(policy.get("方案编号")))
        candidates.append(_overlay(policy, revision["提交值"]) if revision else policy)
    if not candidates:
        return False, "分区范围内没有已启用策略，无法裁决", {"decision": before, "revisions": revisions}
    winner = max(candidates, key=_rank)
    winner_no = str(winner.get("方案编号"))
    applied = by_policy.get(winner_no)

    if applied is not None:
        changed_fields = {
            field: merged_value
            for policy in _active_policies(scope)
            if str(policy.get("方案编号")) == winner_no
            for field, merged_value in _overlay(policy, applied["提交值"]).items()
            if field in APPLY_FIELDS and policy.get(field) != merged_value
        }
        if not changed_fields:
            applied["未采纳原因"] = "提交值与当前方案一致，不产生新的生效版本"
            applied = None

    if applied is not None:
        _snapshot_history(before, reason)
        for policy in _active_policies(scope):
            if str(policy.get("方案编号")) != winner_no:
                continue
            merged = _overlay(policy, applied["提交值"])
            policy.update({
                field: merged.get(field)
                for field in APPLY_FIELDS + ["阈值", "判定规则", "合并配置"]
                if field in merged
            })
            policy["版本"] = _next_version(policy.get("版本"))
            policy["提交时间"] = applied["提交时间"]
            policy["提交序列"] = max(int(applied["提交序列"]), int(policy.get("提交序列") or 0) + 1)
            applied["是否采纳"] = True
            applied["未采纳原因"] = None
            applied["仲裁结果"] = "唯一生效方案"

    after = resolve_scope(scope)
    for revision in revisions:
        if revision["校验失败"] or revision["是否采纳"] or revision.get("未采纳原因"):
            continue
        if str(revision["方案编号"]) != winner_no:
            base = next(
                (row for row in store.rows(POLICY_MODULE)
                 if str(row.get("方案编号")) == str(revision["方案编号"])),
                None,
            )
            submitted = _overlay(base, revision["提交值"]) if base else {}
            points = _conflict_points(_candidate_payload(submitted), _candidate_payload(winner)) if submitted else []
            revision["未采纳原因"] = "；".join(points) if points else "未在唯一生效方案中胜出"
        else:
            revision["未采纳原因"] = "同方案并发提交中，业务优先级/生效时间/提交序列排序较低"

    if applied is None:
        blocked = [r for r in revisions if r.get("未采纳原因")]
        message = "本批提交没有改变唯一生效方案，未落盘"
        for row in blocked:
            if "不参与唯一生效方案裁决" in str(row.get("未采纳原因")):
                message = f"{row['方案编号']} 当前未启用（草稿或已停用），调整不进入裁决，已拒绝落盘"
                break
        return False, message, {"decision": after, "revisions": revisions}

    _append_todo(after, action, int(applied["id"]))
    _append_event(
        action,
        str(after["winner"]["方案编号"]),
        actor,
        f"{after['decision_note']}本批提交 {len(revisions)} 条，仅采纳 {applied['修订编号']}；"
        f"保留 {after['rule'].get('保留天数')} 天、阈值「{after['rule'].get('阈值')}」、判定规则「{after['rule'].get('判定规则')}」。{reason}",
        f"{after['winner']['方案编号']} {after['rule'].get('版本')} 生效，其余 "
        f"{sum(1 for revision in revisions if not revision['是否采纳'])} 条未采纳",
    )
    evaluate_partitions(persist=True, decision=after)
    return True, f"{after['decision_note']}已按新口径重算待归档分区", {
        "decision": after, "revisions": revisions, "before": before,
    }


def adjust_policy(policy_id: int, values: dict[str, Any], actor: str = "策略审批人") -> tuple[bool, str, dict[str, Any]]:
    policy = store.find(POLICY_MODULE, policy_id)
    if policy is None:
        return False, f"分区策略 {policy_id} 不存在", {}
    with _lock:
        reason = _validate_values(policy, values)
        revision = _record_revision(
            policy, values, actor, reason,
            concurrent_group=f"ADJ-{datetime.now().strftime('%H%M%S')}",
        )
        if reason:
            decision = effective_decision()
            return False, reason, {
                "rejected": True,
                "revision": revision,
                "current_rule": decision.get("rule"),
                "current_winner": (decision.get("winner") or {}).get("方案编号"),
            }
        description = (
            f"调整 {policy.get('方案编号')}：保留天数 {policy.get('保留天数')} → "
            f"{revision.get('保留天数')} 天，阈值 {policy.get('阈值')} → "
            f"单文件≤{revision.get('阈值数值')}MB 且 文件数≥{revision.get('最少文件数')}；"
            f"业务优先级 {revision.get('业务优先级')}，生效时间 {revision.get('生效时间')}。"
        )
        ok, message, extra = _apply_revisions([revision], actor, reason=description, action="策略调整")
        # 响应里只回传本次修订和最近一次重算，避免把整份裁决重复塞进页面请求。
        compact = {
            "decision": extra.get("decision"),
            "revision": revision,
            "last_recalc": copy.deepcopy(_last_recalc),
        }
        if not ok:
            compact["revisions"] = extra.get("revisions", [])
        return ok, message, compact


def concurrent_adjust(submissions: list[dict[str, Any]], actor: str = "策略审批人") -> tuple[bool, str, dict[str, Any]]:
    """同一分区范围的两条并发提交，只让优先级最高且生效时间最新的一条生效。"""
    with _lock:
        group = f"CONC-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        revisions: list[dict[str, Any]] = []
        for item in submissions:
            policy = store.find(POLICY_MODULE, int(item.get("id") or 0))
            if policy is None:
                continue
            values = dict(item.get("values") or {})
            reason = _validate_values(policy, values)
            revisions.append(_record_revision(policy, values, actor, reason, concurrent_group=group))
        if not revisions:
            return False, "并发提交里没有可识别的分区策略", {}
        ok, message, extra = _apply_revisions(
            revisions, actor,
            reason=f"并发提交演练：{len(revisions)} 条同一分区范围修改同时到达，按业务优先级与生效时间裁决。",
            action="并发裁决",
        )
        extra["revisions"] = revisions
        extra["last_recalc"] = copy.deepcopy(_last_recalc)
        return ok, message, extra


def rollback(actor: str = "策略审批人") -> tuple[bool, str, dict[str, Any]]:
    """回滚到上一份策略快照，并解释为什么恢复旧方案。"""
    with _lock:
        if not _history:
            return False, "没有可回滚的策略版本", {}
        snapshot = _history.pop()
        fields = snapshot["decision"].get("scope_fields") or {}
        scope = (
            str(fields.get("目标表名") or ""),
            str(fields.get("分区字段") or ""),
            str(fields.get("分区范围") or ""),
        )
        current = resolve_scope(scope)
        for row in store.rows(POLICY_MODULE):
            for saved in snapshot["policies"]:
                if int(saved.get("id", 0)) == int(row.get("id", 0)):
                    row.clear()
                    row.update(copy.deepcopy(saved))
        after = resolve_scope(scope)
        reason = (
            f"复核发现新方案（保留 {current.get('rule', {}).get('保留天数')} 天、{current.get('rule', {}).get('阈值')}）"
            f"与归档团队复核结论不一致；恢复 {after['winner']['方案编号']} {after['rule'].get('版本')}"
            f"（保留 {after['rule'].get('保留天数')} 天、{after['rule'].get('阈值')}）旧口径。"
            f"待归档分区按恢复后的口径重算，已归档分区保持原路径。"
        )
        _append_todo(after, "策略回滚")
        _append_event("策略回滚", str(after["winner"]["方案编号"]), actor, reason, f"恢复 {after['rule'].get('版本')} 生效")
        evaluate_partitions(persist=True, decision=after)
        return True, reason, {"decision": after, "explanation": reason, "rolled_back_to": after["rule"].get("版本")}


def export_effective() -> dict[str, Any]:
    """导出只能包含当前唯一生效方案，不带落选策略。"""
    decision = effective_decision()
    winner = decision.get("winner") or {}
    return {
        "module": "lakezone",
        "export_type": "当前唯一生效方案",
        "exported_at": now_text(),
        "for_review": "归档团队复核",
        "scope": decision.get("scope"),
        "scope_fields": decision.get("scope_fields"),
        "basis": decision.get("basis"),
        "decision_note": decision.get("decision_note"),
        "total": 1,
        "effective": {**winner, "rule": decision.get("rule"), "as_of": decision.get("as_of")},
    }


def list_revisions() -> list[dict[str, Any]]:
    return list(store.rows(REVISION_MODULE))


def history() -> list[dict[str, Any]]:
    return list(_history)
