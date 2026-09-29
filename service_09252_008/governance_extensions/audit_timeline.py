"""操作审计时间线：append-only 的预约操作链。

时间线只记录“谁在什么时候对哪个预约做了什么操作”，覆盖四类操作：
创建、修改（改期）、取消与人工介入，是运营主管回放一次预约全流程的依据。

设计约定：
- **只增不改**：事件写入独立的 ``audit_timeline`` 集合（SQLite 中持久化），
  每条事件带单调递增的事件序号 ``seq``，``seq`` 是排序的唯一依据；
  即使两条事件发生在同一时刻，先后顺序仍由序号稳定决定；
- **事务内追加**：:func:`append_audit_event` 必须在调用方的
  ``store.transaction()`` 内执行——序号在事务内读取，事件随业务写入
  一起提交；业务事务回滚时审计事件一并回滚，不会留下与实际状态不符的记录。
  SQLite 写事务由 ``BEGIN IMMEDIATE`` 串行化，内存后端由排他锁保护，
  因此 ``MAX(seq)+1`` 在并发下仍然连续无冲突；
- **过滤不改序**：:func:`query_audit_events` 始终按 ``seq`` 升序返回，
  操作者/预约过滤只筛选行，不参与排序，故过滤结果必为全量链的子序列；
- **重启可查**：事件随主库持久化，服务重启后仍可按序号复现整条链。
"""
from __future__ import annotations

from typing import Any

from ..persistence.store import Store

COLLECTION_AUDIT_TIMELINE = "audit_timeline"

# 操作类型：预约创建 / 修改（改期）/ 取消 / 人工介入
ACTION_BOOKING_CREATED = "booking_created"
ACTION_BOOKING_MODIFIED = "booking_modified"
ACTION_BOOKING_CANCELLED = "booking_cancelled"
ACTION_MANUAL_INTERVENTION = "manual_intervention"

TIMELINE_ACTIONS = frozenset(
    {
        ACTION_BOOKING_CREATED,
        ACTION_BOOKING_MODIFIED,
        ACTION_BOOKING_CANCELLED,
        ACTION_MANUAL_INTERVENTION,
    }
)

# 未显式给出操作者时（如旧客户端、系统恢复任务）使用的兜底操作者
SYSTEM_OPERATOR = "system"


def normalize_operator(operator: Any) -> str:
    """校验并规范化操作者：缺省为 ``system``，给出时必须是非空字符串。"""
    if operator is None:
        return SYSTEM_OPERATOR
    if not isinstance(operator, str) or not operator.strip():
        raise ValueError("operator must be a non-empty string")
    return operator.strip()


def append_audit_event(
    store: Store,
    *,
    booking_id: str,
    action: str,
    occurred_at: str,
    operator: str = SYSTEM_OPERATOR,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """在当前事务内追加一条审计事件，返回含事件序号的事件副本。

    调用方必须已进入 ``store.transaction()``；本函数不自行开启/提交事务。
    """
    if action not in TIMELINE_ACTIONS:
        raise ValueError(f"unknown audit action: {action}")
    if not isinstance(booking_id, str) or not booking_id:
        raise ValueError("booking_id is required for an audit event")
    # 序号在调用方事务内分配：写事务串行化，MAX+1 不会撞号；
    # 外层回滚时序号与事件一同撤销，不留空洞记录。
    existing = store.query(COLLECTION_AUDIT_TIMELINE)
    seq = max((int(row["seq"]) for row in existing), default=0) + 1
    event = {
        "seq": seq,
        "booking_id": booking_id,
        "action": action,
        "operator": operator,
        "occurred_at": occurred_at,
        "detail": dict(detail or {}),
    }
    store.put(COLLECTION_AUDIT_TIMELINE, f"aud_{seq:012d}", event)
    return dict(event)


def query_audit_events(
    store: Store,
    *,
    booking_id: str | None = None,
    operator: str | None = None,
) -> list[dict[str, Any]]:
    """按事件序号升序返回审计事件。

    ``booking_id`` / ``operator`` 仅做等值过滤，过滤不改变 ``seq`` 顺序，
    返回结果始终是全量时间线的一个子序列。
    """
    filters: dict[str, Any] = {}
    if booking_id is not None:
        filters["booking_id"] = booking_id
    if operator is not None:
        filters["operator"] = operator
    events = store.query(COLLECTION_AUDIT_TIMELINE, **filters)
    events.sort(key=lambda event: int(event["seq"]))
    return events
