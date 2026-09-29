"""SQLite 审计流：Python 侧 INSERT 追加，``AUTOINCREMENT`` 分配全局序号。"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_timeline (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT NOT NULL,
    booking_id   TEXT,
    action       TEXT NOT NULL,
    operator_id  TEXT NOT NULL,
    occurred_at  TEXT NOT NULL,
    summary      TEXT,
    details      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_booking ON audit_timeline (booking_id, seq);
CREATE INDEX IF NOT EXISTS idx_audit_operator ON audit_timeline (operator_id, seq);
"""


class SQLiteAuditLog:
    """基于 SQLite 的操作审计时间线。

    - 事件由应用代码（Python）以 ``INSERT`` 追加，序号由数据库单调分配；
    - 查询一律 ``ORDER BY seq``，过滤条件（预约/操作者）只做筛选，不改变先后；
    - ``AUTOINCREMENT`` 保证重启后序号继续递增且不复用。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if self._path != Path(":memory:"):
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.RLock()

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        payload = dict(record)
        payload.pop("seq", None)  # 序号由数据库分配，忽略调用方取值
        details_json = json.dumps(payload.get("details", {}), ensure_ascii=False, sort_keys=True)
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO audit_timeline "
                "(event_id, booking_id, action, operator_id, occurred_at, summary, details) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    payload["event_id"],
                    payload.get("booking_id"),
                    payload["action"],
                    payload["operator_id"],
                    payload["occurred_at"],
                    payload.get("summary"),
                    details_json,
                ),
            )
            seq = int(cursor.lastrowid)
            self._conn.commit()
        return {**payload, "details": json.loads(details_json), "seq": seq}

    def list_events(
        self,
        *,
        booking_id: str | None = None,
        operator_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if booking_id is not None:
            clauses.append("booking_id = ?")
            params.append(booking_id)
        if operator_id is not None:
            clauses.append("operator_id = ?")
            params.append(operator_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT seq, event_id, booking_id, action, operator_id, occurred_at, summary, details "
            f"FROM audit_timeline {where} ORDER BY seq ASC"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "seq": row["seq"],
                "event_id": row["event_id"],
                "booking_id": row["booking_id"],
                "action": row["action"],
                "operator_id": row["operator_id"],
                "occurred_at": row["occurred_at"],
                "summary": row["summary"],
                "details": json.loads(row["details"]),
            }
            for row in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
