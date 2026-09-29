"""操作审计时间线：创建/修改/取消/人工介入成链、按事件序号排序、
操作者过滤不改变顺序，以及 SQLite 重启后顺序稳定。
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from service_09252_008.interfaces.http_api import create_server
from service_09252_008.persistence.sqlite_store import SQLiteStore
from tests.helpers import apply_payload, make_services, seed_catalog

NEW_SLOT_START = "2026-10-01T04:00:00+00:00"  # 12:00-14:00 Asia/Shanghai，仍在接待窗口内
NEW_SLOT_END = "2026-10-01T06:00:00+00:00"


def _seqs(events: list[dict]) -> list[int]:
    return [e["seq"] for e in events]


def _actions(events: list[dict]) -> list[str]:
    return [e["action"] for e in events]


class AuditTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog, self.bookings, self.clock, self.store = make_services()
        self.ids = seed_catalog(self.catalog)

    def _create_modify_intervene_cancel(self) -> str:
        applied = self.bookings.apply(apply_payload(self.ids, "k-audit-1", operator="alice"))
        booking_id = applied["booking_id"]
        # 人工介入发生在改期之前：不改状态，只留痕
        self.bookings.intervene(
            booking_id,
            {"operator": "bob", "note": "运营主管电话确认院校需求"},
        )
        self.bookings.reschedule(
            booking_id,
            {
                "slot_start": NEW_SLOT_START,
                "slot_end": NEW_SLOT_END,
                "operator": "alice",
            },
        )
        self.bookings.cancel(booking_id, {"operator": "alice", "reason": "院校行程冲突"})
        return booking_id

    def test_lifecycle_chained_in_seq_order(self) -> None:
        booking_id = self._create_modify_intervene_cancel()
        timeline = self.bookings.get_audit_timeline(booking_id)["items"]
        self.assertEqual(
            _actions(timeline),
            [
                "booking_created",
                "manual_intervention",
                "booking_modified",
                "booking_cancelled",
            ],
        )
        seqs = _seqs(timeline)
        # 事件序号连续且严格递增，这是时间线稳定顺序的唯一依据
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        self.assertTrue(all(e["booking_id"] == booking_id for e in timeline))
        # 预约视图内嵌同一条链
        view = self.bookings.get_booking(booking_id)
        self.assertEqual(_actions(view["audit_timeline"]), _actions(timeline))

    def test_operator_filter_is_order_preserving_subsequence(self) -> None:
        booking_id = self._create_modify_intervene_cancel()
        full = self.bookings.get_audit_timeline(booking_id)["items"]
        full_by_seq = {e["seq"]: e for e in full}

        alice = self.bookings.get_audit_timeline(booking_id, operator="alice")["items"]
        bob = self.bookings.get_audit_timeline(booking_id, operator="bob")["items"]

        # 过滤不改变顺序：过滤结果必须是全量链按相同相对顺序取出的子序列
        self.assertEqual(_seqs(alice), [s for s in _seqs(full) if full_by_seq[s]["operator"] == "alice"])
        self.assertEqual(_actions(alice), ["booking_created", "booking_modified", "booking_cancelled"])
        self.assertEqual(_actions(bob), ["manual_intervention"])
        # 跨预约的操作者过滤同样稳定
        self.assertEqual(
            _actions(self.bookings.get_audit_timeline(operator="alice")["items"]),
            ["booking_created", "booking_modified", "booking_cancelled"],
        )

    def test_default_operator_is_system(self) -> None:
        applied = self.bookings.apply(apply_payload(self.ids, "k-audit-sys"))
        timeline = self.bookings.get_audit_timeline(applied["booking_id"])["items"]
        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline[0]["operator"], "system")
        self.assertEqual(timeline[0]["action"], "booking_created")

    def test_failed_operation_appends_no_event(self) -> None:
        applied = self.bookings.apply(apply_payload(self.ids, "k-audit-rollback", operator="alice"))
        booking_id = applied["booking_id"]
        self.bookings.cancel(booking_id, {"operator": "alice"})

        # 终态预约再次取消：业务失败，审计事务一并回滚，不留第二条取消事件
        with self.assertRaises(Exception):
            self.bookings.cancel(booking_id, {"operator": "bob"})
        actions = _actions(self.bookings.get_audit_timeline(booking_id)["items"])
        self.assertEqual(actions, ["booking_created", "booking_cancelled"])

        # 人工介入缺少操作者/说明：校验失败同样不留痕
        with self.assertRaises(Exception):
            self.bookings.intervene(booking_id, {"note": "x"})
        with self.assertRaises(Exception):
            self.bookings.intervene(booking_id, {"operator": "bob"})
        self.assertEqual(
            _actions(self.bookings.get_audit_timeline(booking_id)["items"]),
            ["booking_created", "booking_cancelled"],
        )

    def test_same_instant_events_ordered_by_seq(self) -> None:
        # 时钟不动：多次创建事件 occurred_at 完全相同，先后只能由序号决定
        first = self.bookings.apply(apply_payload(self.ids, "k-audit-t1", operator="alice"))
        second = self.bookings.apply(apply_payload(self.ids, "k-audit-t2", operator="alice"))
        events = self.bookings.get_audit_timeline(operator="alice")["items"]
        self.assertEqual([e["booking_id"] for e in events], [first["booking_id"], second["booking_id"]])
        self.assertEqual(_seqs(events), sorted(_seqs(events)))
        self.assertEqual(len({e["occurred_at"] for e in events}), 1)


class AuditTimelineSqliteTests(unittest.TestCase):
    def test_timeline_survives_restart_in_seq_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/booking.db"
            store = SQLiteStore(db_path)
            catalog, bookings, _, _ = make_services(store)
            ids = seed_catalog(catalog)
            applied = bookings.apply(apply_payload(ids, "k-sql-audit", operator="alice"))
            booking_id = applied["booking_id"]
            bookings.intervene(booking_id, {"operator": "bob", "note": "人工核对库存"})
            bookings.reschedule(
                booking_id, {"slot_start": NEW_SLOT_START, "slot_end": NEW_SLOT_END, "operator": "alice"}
            )
            before = [(e["seq"], e["action"], e["operator"]) for e in bookings.get_audit_timeline(booking_id)["items"]]
            store.close()

            # 重启：新服务实例挂载同一数据库，时间链仍按序号复现
            store2 = SQLiteStore(db_path)
            catalog2, bookings2, _, _ = make_services(store2)
            after = bookings2.get_audit_timeline(booking_id)
            self.assertEqual(
                [(e["seq"], e["action"], e["operator"]) for e in after["items"]],
                before,
            )
            filtered = bookings2.get_audit_timeline(booking_id, operator="alice")["items"]
            self.assertEqual([e["action"] for e in filtered], ["booking_created", "booking_modified"])
            self.assertEqual([e["seq"] for e in filtered], sorted(e["seq"] for e in filtered))
            store2.close()


class AuditTimelineHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        catalog, bookings, clock, store = make_services()
        cls.ids = seed_catalog(catalog)
        cls.bookings = bookings
        cls.server = create_server("127.0.0.1", 0, catalog, bookings)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _request(
        self, method: str, path: str, body: dict | None = None, headers: dict | None = None
    ) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers or {}
        )
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_operator_header_and_timeline_endpoint(self) -> None:
        body = {
            "institution": "审计学院",
            "package_id": self.ids["package_id"],
            "mentor_id": self.ids["mentor_id"],
            "resource_id": self.ids["resource_id"],
            "window_id": self.ids["window_id"],
            "seats": 4,
            "slot_start": "2026-10-01T02:00:00+00:00",
            "slot_end": "2026-10-01T04:00:00+00:00",
        }
        status, applied = self._request(
            "POST", "/bookings", body, headers={"Idempotency-Key": "http-audit-1", "X-Operator": "alice"}
        )
        self.assertEqual(status, 201)
        booking_id = applied["booking_id"]

        status, _ = self._request(
            "POST",
            f"/bookings/{booking_id}/intervene",
            {"note": "人工标记重点回访"},
            headers={"X-Operator": "bob"},
        )
        self.assertEqual(status, 200)

        status, full = self._request("GET", f"/bookings/{booking_id}/timeline")
        self.assertEqual(status, 200)
        self.assertEqual([e["action"] for e in full["items"]], ["booking_created", "manual_intervention"])
        self.assertEqual([e["operator"] for e in full["items"]], ["alice", "bob"])

        status, bob_only = self._request("GET", f"/bookings/{booking_id}/timeline?operator=bob")
        self.assertEqual(status, 200)
        self.assertEqual([e["action"] for e in bob_only["items"]], ["manual_intervention"])
        # 过滤后序号仍与全量链中的对应事件一致
        self.assertEqual(
            [e["seq"] for e in bob_only["items"]],
            [e["seq"] for e in full["items"] if e["operator"] == "bob"],
        )

        status, missing = self._request("GET", "/bookings/bkg_missing/timeline")
        self.assertEqual(status, 404)
        self.assertEqual(missing["error"], "not_found")


if __name__ == "__main__":
    unittest.main()
