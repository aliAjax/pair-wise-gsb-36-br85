import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo
from carryover import compute_carryover, plan_drawdown


class CarryoverCalcTest(unittest.TestCase):
    def test_two_tenths_cap_and_forfeit(self):
        calc = compute_carryover(1000, 100, 100)
        self.assertEqual(calc, {"unused": 800.0, "cap": 200.0, "carried": 200.0, "forfeited": 600.0})

    def test_unused_below_cap_carries_all(self):
        calc = compute_carryover(1000, 950, 0)
        self.assertEqual(calc["carried"], 50)
        self.assertEqual(calc["forfeited"], 0)

    def test_overused_account_carries_nothing(self):
        calc = compute_carryover(100, 120, 0)
        self.assertEqual(calc["unused"], 0)
        self.assertEqual(calc["carried"], 0)
        self.assertEqual(calc["forfeited"], 0)

    def test_plan_drawdown_current_first_then_pool(self):
        from_current, draws = plan_drawdown(250, 200, [{"id": 7, "remaining": 100}])
        self.assertEqual(from_current, 200)
        self.assertEqual(draws, [(7, 50)])

    def test_plan_drawdown_spans_multiple_pools(self):
        from_current, draws = plan_drawdown(120, 0, [{"id": 1, "remaining": 50}, {"id": 2, "remaining": 100}])
        self.assertEqual(from_current, 0)
        self.assertEqual(draws, [(1, 50), (2, 70)])


class CarryoverBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.accounts = seed_demo(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_batch_deducts_used_and_pending_then_caps(self):
        source, target = self.accounts["北区水库"], self.accounts["河口灌区"]
        self.db.create_transfer("alice", {"from_account_id": source, "to_account_id": target, "amount": 200, "effective_date": "2026-06-01"}, "editor")
        result = self.db.create_carryover_batch("alice", {"year": 2026, "note": "年度切换"}, "editor")
        entries = {e["account_name"]: e for e in result["entries"]}
        north = entries["北区水库"]
        self.assertEqual((north["quota"], north["used"], north["reserved"]), (1000, 100, 200))
        self.assertEqual(north["unused"], 700)
        self.assertEqual(north["cap"], 200)
        self.assertEqual(north["carried"], 200)
        self.assertEqual(north["forfeited"], 500)
        delta = entries["河口灌区"]
        self.assertEqual(delta["carried"], 100)
        self.assertEqual(delta["forfeited"], 400)
        self.assertEqual(result["batch"]["year"], 2026)

    def test_duplicate_batch_points_to_original(self):
        first = self.db.create_carryover_batch("alice", {"year": 2026}, "editor")
        original_id = first["batch"]["id"]
        with self.assertRaisesRegex(DomainError, f"原批次 #{original_id}"):
            self.db.create_carryover_batch("alice", {"year": 2026}, "editor")
        second = self.db.create_carryover_batch("alice", {"year": 2027}, "editor")
        self.assertNotEqual(second["batch"]["id"], original_id)

    def test_partial_duplicate_batch_is_atomic(self):
        self.db.create_carryover_batch("alice", {"year": 2026, "account_ids": [self.accounts["北区水库"]]}, "editor")
        with self.assertRaisesRegex(DomainError, "不能重复生成"):
            self.db.create_carryover_batch(
                "alice", {"year": 2026, "account_ids": [self.accounts["河口灌区"], self.accounts["北区水库"]]}, "editor")
        self.assertEqual(self.db.account_carryover(self.accounts["河口灌区"])["entries"], [])

    def test_non_admin_cannot_generate_batch(self):
        with self.assertRaisesRegex(DomainError, "配额管理员"):
            self.db.create_carryover_batch("bob", {"year": 2026}, "viewer")

    def test_list_and_get_batches(self):
        created = self.db.create_carryover_batch("alice", {"year": 2026}, "editor")
        batches = self.db.list_carryover_batches()
        self.assertEqual(len(batches), 1)
        self.assertEqual({e["account_name"] for e in batches[0]["entries"]}, {"北区水库", "河口灌区"})
        detail = self.db.get_carryover_batch(created["batch"]["id"])
        self.assertEqual(detail["batch"]["id"], created["batch"]["id"])
        with self.assertRaisesRegex(DomainError, "不存在"):
            self.db.get_carryover_batch(999)


class CarryoverUsageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.account = self.db.create_account(
            "alice", {"name": "跨年灌区", "region": "mid", "holder": "灌区合作社", "priority": 3,
                      "valid_from": "2026-01-01", "valid_to": "2027-12-31", "quota": 1000}, "editor")
        self.db.record_usage("meter-01", {"account_id": self.account["id"], "amount": 800,
                                          "meter_event_id": "M-2026", "occurred_at": "2026-05-01"}, "meter")
        # 剩余 200，两成上限 200，全部结入 2027 年结转池。
        self.db.create_carryover_batch("alice", {"year": 2026, "account_ids": [self.account["id"]]}, "editor")

    def tearDown(self):
        self.tmp.cleanup()

    def test_next_year_usage_draws_current_quota_then_pool(self):
        usage = self.db.record_usage("meter-01", {"account_id": self.account["id"], "amount": 250,
                                                  "meter_event_id": "M-2027", "occurred_at": "2027-03-01"}, "meter")
        self.assertEqual(usage["from_current_quota"], 200)
        self.assertEqual(usage["from_carryover_pool"], 50)
        self.assertEqual(self.db.account_carryover(self.account["id"])["carryover_pool"], 150)
        self.assertEqual(self.db.available(self.account["id"], "2027-04-01")["used"], 1000)

    def test_pool_not_available_in_source_year(self):
        with self.assertRaisesRegex(DomainError, "超过可用额度"):
            self.db.record_usage("meter-01", {"account_id": self.account["id"], "amount": 250,
                                              "meter_event_id": "M-LATE", "occurred_at": "2026-11-01"}, "meter")

    def test_usage_beyond_current_and_pool_rejected(self):
        with self.assertRaisesRegex(DomainError, "超过可用额度"):
            self.db.record_usage("meter-01", {"account_id": self.account["id"], "amount": 500,
                                              "meter_event_id": "M-X", "occurred_at": "2027-01-01"}, "meter")

    def test_transfer_does_not_recognize_pool(self):
        target = self.db.create_account(
            "alice", {"name": "邻区账户", "region": "mid", "holder": "邻区水务", "priority": 4,
                      "valid_from": "2026-01-01", "valid_to": "2027-12-31", "quota": 100}, "editor")
        # 当年可用 200，结转池 200；转让 300 只认当年额度，应被拒绝。
        with self.assertRaisesRegex(DomainError, "可用额度不足"):
            self.db.create_transfer("alice", {"from_account_id": self.account["id"], "to_account_id": target["id"],
                                              "amount": 300, "effective_date": "2027-06-01"}, "editor")
        transfer = self.db.create_transfer("alice", {"from_account_id": self.account["id"], "to_account_id": target["id"],
                                                     "amount": 200, "effective_date": "2027-06-01"}, "editor")
        self.assertEqual(transfer["status"], "pending")

    def test_available_reports_pool_for_following_year_only(self):
        self.assertEqual(self.db.available(self.account["id"], "2027-01-01")["carryover_pool"], 200)
        self.assertEqual(self.db.available(self.account["id"], "2026-01-01")["carryover_pool"], 0)
        listed = {a["id"]: a for a in self.db.list_accounts()}
        self.assertEqual(listed[self.account["id"]]["carryover_pool"], 200)


if __name__ == "__main__":
    unittest.main()
