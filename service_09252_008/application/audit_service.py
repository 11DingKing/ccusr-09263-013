"""操作审计时间线应用服务：追加审计事件与按序号查询。

时间线把预约的创建、修改、取消与运营主管人工介入串成一条链：
- 追加只在预约用例事务内发生，序号由存储层单调分配；
- 查询固定按 ``seq`` 升序，操作者过滤只筛选事件、不改变相对顺序。
"""
from __future__ import annotations

from typing import Any

from ..domain.models import AuditAction, AuditEvent
from ..persistence.audit_store import AuditLog
from .ports import Clock, IdGenerator


def resolve_operator(request: dict[str, Any] | None) -> str:
    """从请求载荷提取操作者；未携带时记为系统自动触发。"""
    if not request:
        from ..domain.models import SYSTEM_OPERATOR

        return SYSTEM_OPERATOR
    operator = request.get("operator_id")
    if not isinstance(operator, str) or not operator.strip():
        from ..domain.models import SYSTEM_OPERATOR

        return SYSTEM_OPERATOR
    return operator.strip()


class AuditTimelineService:
    """操作审计时间线查询与追加。"""

    def __init__(self, audit_log: AuditLog, clock: Clock, ids: IdGenerator) -> None:
        self._log = audit_log
        self._clock = clock
        self._ids = ids

    def append(
        self,
        *,
        action: AuditAction,
        booking_id: str | None,
        operator_id: str,
        summary: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            event_id=self._ids.new_id("aud"),
            booking_id=booking_id,
            action=action,
            operator_id=operator_id,
            occurred_at=self._clock.now(),
            summary=summary,
            details=details or {},
        )
        stored = self._log.append(event.to_dict())
        return AuditEvent.from_dict(stored)

    def get_timeline(
        self,
        *,
        booking_id: str | None = None,
        operator_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """返回时间线；结果一律按事件序号升序，过滤不改变顺序。"""
        return self._log.list_events(booking_id=booking_id, operator_id=operator_id)
