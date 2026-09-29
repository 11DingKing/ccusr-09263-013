"""操作审计时间线存储。

与文档型主存储分开：审计流是只追加（append-only）的事件序列，
顺序由存储层分配的单调递增序号 ``seq`` 保证，不依赖时钟或事件 ID，
因此即使多个事件在同一时刻产生、即使服务重启，顺序仍然稳定。
"""
from __future__ import annotations

import threading
from typing import Any, Protocol


class AuditLog(Protocol):
    """审计流端口：追加事件、按序号读取。"""

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        """追加一条审计记录，分配下一个 ``seq``；返回带序号的副本。"""
        ...

    def list_events(
        self,
        *,
        booking_id: str | None = None,
        operator_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """按 ``seq`` 升序返回事件；过滤只筛选，绝不重排。"""
        ...

    def close(self) -> None:
        ...


class InMemoryAuditLog:
    """进程内审计流：测试与演示用。"""

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._next_seq = 1

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        from copy import deepcopy

        stored = deepcopy(record)
        with self._lock:
            stored["seq"] = self._next_seq
            self._next_seq += 1
            self._events.append(stored)
        return deepcopy(stored)

    def list_events(
        self,
        *,
        booking_id: str | None = None,
        operator_id: str | None = None,
    ) -> list[dict[str, Any]]:
        from copy import deepcopy

        with self._lock:
            events = sorted(self._events, key=lambda e: e["seq"])
        if booking_id is not None:
            events = [e for e in events if e.get("booking_id") == booking_id]
        if operator_id is not None:
            events = [e for e in events if e.get("operator_id") == operator_id]
        return deepcopy(events)

    def close(self) -> None:  # pragma: no cover - 对称接口
        pass
