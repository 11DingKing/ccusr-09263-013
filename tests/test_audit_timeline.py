"""操作审计时间线：创建/修改/取消/人工介入串链、按操作者过滤且顺序稳定。"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest

from service_09252_008.application.audit_service import AuditTimelineService
from service_09252_008.application.ports import ManualClock, SequentialIdGenerator
from service_09252_008.persistence.sqlite_audit_log import SQLiteAuditLog
from service_09252_008.persistence.store import InMemoryStore
from tests.helpers import NOW, apply_payload, make_services, seed_catalog

OP_A = "op_li"
OP_B = "op_wang"
SUPERVISOR = "supervisor_zhao"


def _is_subsequence_in_order(full: list[int], filtered: list[int]) -> bool:
    """filtered 是否为 full 的子序列（元素一致且相对顺序不变）。"""
    rest = iter(full)
    return all(seq in rest for seq in filtered)


class AuditTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog, self.bookings, self.clock, self.store = make_services()
        self.ids = seed_catalog(self.catalog)

    def _create(self, key: str, operator: str) -> str:
        payload = {**apply_payload(self.ids, key, institution=f"学院-{key}"), "operator_id": operator}
        return self.bookings.apply(payload)["booking_id"]

    def test_lifecycle_chained_in_one_timeline(self) -> None:
        booking_id = self._create("k-audit-1", OP_A)
        self.bookings.quote(booking_id, {"operator_id": OP_A})
        self.bookings.lock(booking_id, {"idempotency_key": "k-audit-lock", "operator_id": OP_A})
        # 取消已锁定预约释放资源
        self.bookings.cancel(
            booking_id, {"reason": "院校临时停课", "operator_id": OP_B, "idempotency_key": "k-audit-cancel"}
        )

        other = self._create("k-audit-2", OP_A)
        self.bookings.reschedule(
            other,
            {
                "slot_start": "2026-10-01T03:00:00+00:00",
                "slot_end": "2026-10-01T05:00:00+00:00",
                "operator_id": OP_B,
                "idempotency_key": "k-audit-rs",
            },
        )
        self.bookings.manual_intervention(
            other, {"summary": "运营主管电话确认改期", "operator_id": SUPERVISOR}
        )

        timeline = self.bookings.get_timeline()
        seqs = [e["seq"] for e in timeline]
        # 序号严格递增、全局唯一
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))
        self.assertEqual([1, 2, 3, 4, 5], seqs)

        by_booking = {e["seq"]: e for e in timeline}
        self.assertEqual(by_booking[1]["action"], "CREATED")
        self.assertEqual(by_booking[1]["booking_id"], booking_id)
        self.assertEqual(by_booking[2]["action"], "CANCELLED")
        self.assertEqual(by_booking[2]["operator_id"], OP_B)
        self.assertEqual(by_booking[3]["action"], "CREATED")
        self.assertEqual(by_booking[4]["action"], "MODIFIED")
        self.assertEqual(by_booking[5]["action"], "MANUAL_INTERVENTION")
        self.assertEqual(by_booking[5]["operator_id"], SUPERVISOR)

        # 单预约链：四类动作按发生先后排列
        chain = self.bookings.get_booking_timeline(other)
        self.assertEqual([e["action"] for e in chain], ["CREATED", "MODIFIED", "MANUAL_INTERVENTION"])
        self.assertTrue(all(e["booking_id"] == other for e in chain))

    def test_operator_filter_does_not_reorder(self) -> None:
        # 两个操作者交错产生事件：A建、B建、A取消、B介入
        b1 = self._create("k-f-a1", OP_A)
        b2 = self._create("k-f-b1", OP_B)
        self.bookings.cancel(b1, {"operator_id": OP_A})
        self.bookings.manual_intervention(b2, {"summary": "主管回访", "operator_id": OP_B})

        full = self.bookings.get_timeline()
        full_seqs = [e["seq"] for e in full]
        self.assertEqual(full_seqs, [1, 2, 3, 4])

        for operator, expected_seqs in ((OP_A, [1, 3]), (OP_B, [2, 4])):
            filtered = self.bookings.get_timeline(operator_id=operator)
            filtered_seqs = [e["seq"] for e in filtered]
            # 过滤结果与全量中手工筛出的序号完全一致
            self.assertEqual(
                filtered_seqs,
                [e["seq"] for e in full if e["operator_id"] == operator],
            )
            self.assertEqual(filtered_seqs, expected_seqs)
            # 关键不变量：过滤只筛选，不改变相对顺序（仍是全量序列的子序列）
            self.assertTrue(_is_subsequence_in_order(full_seqs, filtered_seqs))
            self.assertTrue(all(s1 < s2 for s1, s2 in zip(filtered_seqs, filtered_seqs[1:])))
            self.assertTrue(all(e["operator_id"] == operator for e in filtered))

        # 单预约链上再按操作者过滤，同样保持顺序
        chain_b = self.bookings.get_booking_timeline(b2, operator_id=OP_B)
        self.assertEqual([e["seq"] for e in chain_b], [2, 4])
        chain_a = self.bookings.get_booking_timeline(b1, operator_id=OP_A)
        self.assertEqual([e["seq"] for e in chain_a], [1, 3])

    def test_same_timestamp_events_keep_stable_order(self) -> None:
        # 手动时钟不推进：所有事件 occurred_at 相同，顺序仍须由序号决定
        b1 = self._create("k-ts-1", OP_A)
        b2 = self._create("k-ts-2", OP_A)
        self.bookings.cancel(b1, {"operator_id": OP_A})
        self.bookings.cancel(b2, {"operator_id": OP_A})
        timeline = self.bookings.get_timeline()
        self.assertEqual(len({e["occurred_at"] for e in timeline}), 1)
        self.assertEqual(
            [(e["booking_id"], e["action"]) for e in timeline],
            [(b1, "CREATED"), (b2, "CREATED"), (b1, "CANCELLED"), (b2, "CANCELLED")],
        )

    def test_missing_operator_recorded_as_system(self) -> None:
        self._create_without_operator = self.bookings.apply(apply_payload(self.ids, "k-sys-1"))
        timeline = self.bookings.get_timeline()
        self.assertEqual(timeline[0]["operator_id"], "system")
        self.assertEqual([e["seq"] for e in self.bookings.get_timeline(operator_id="system")], [1])

    def test_manual_intervention_requires_summary(self) -> None:
        from service_09252_008.domain.errors import ValidationError

        booking_id = self._create("k-mi-1", OP_A)
        with self.assertRaises(ValidationError):
            self.bookings.manual_intervention(booking_id, {"operator_id": SUPERVISOR})


class SQLiteAuditTimelineTests(unittest.TestCase):
    """SQLite 后端：Python 追加落库，重启后序号继续、顺序稳定。"""

    def test_events_appended_from_python_and_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/audit.db"
            clock = ManualClock(NOW)
            ids = SequentialIdGenerator()
            log = SQLiteAuditLog(db_path)
            store = InMemoryStore()
            catalog, bookings, _, _ = make_services_with_audit(store, log, clock, ids)
            seed = seed_catalog(catalog)

            b1 = bookings.apply({**apply_payload(seed, "k-sq-1"), "operator_id": OP_A})["booking_id"]
            b2 = bookings.apply({**apply_payload(seed, "k-sq-2"), "operator_id": OP_B})["booking_id"]
            bookings.cancel(b1, {"operator_id": OP_A})
            bookings.manual_intervention(b2, {"summary": "回放确认", "operator_id": SUPERVISOR})

            # 直接查 SQLite 表：行确实由 Python INSERT 落库，序号为自增主键
            raw = sqlite3.connect(db_path)
            try:
                rows = raw.execute(
                    "SELECT seq, action, operator_id FROM audit_timeline ORDER BY seq"
                ).fetchall()
            finally:
                raw.close()
            self.assertEqual([r[0] for r in rows], [1, 2, 3, 4])
            self.assertEqual([r[1] for r in rows], ["CREATED", "CREATED", "CANCELLED", "MANUAL_INTERVENTION"])
            log.close()

            # 模拟重启：新实例挂同一数据库，序号继续递增、历史顺序不变
            log2 = SQLiteAuditLog(db_path)
            ids2 = SequentialIdGenerator()
            store2 = InMemoryStore()
            clock2 = ManualClock(NOW)
            catalog2, bookings2, _, _ = make_services_with_audit(store2, log2, clock2, ids2)
            seed2 = seed_catalog(catalog2)
            b3 = bookings2.apply({**apply_payload(seed2, "k-sq-3"), "operator_id": OP_A})["booking_id"]
            del b3

            timeline = log2.list_events()
            self.assertEqual([e["seq"] for e in timeline], [1, 2, 3, 4, 5])
            # 过滤不改变顺序：仍是全量的子序列
            full_seqs = [e["seq"] for e in timeline]
            only_a = [e["seq"] for e in log2.list_events(operator_id=OP_A)]
            self.assertEqual(only_a, [1, 3, 5])
            self.assertTrue(_is_subsequence_in_order(full_seqs, only_a))
            # 按预约过滤同样稳定
            self.assertEqual(
                [e["action"] for e in log2.list_events(booking_id=b2)],
                ["CREATED", "MANUAL_INTERVENTION"],
            )
            log2.close()


def make_services_with_audit(store, audit_log, clock, ids):
    from service_09252_008.application.booking_service import BookingService
    from service_09252_008.application.catalog_service import CatalogService

    audit = AuditTimelineService(audit_log, clock, ids)
    catalog = CatalogService(store, clock, ids)
    bookings = BookingService(store, clock, ids, audit=audit)
    return catalog, bookings, clock, store


if __name__ == "__main__":
    unittest.main()
