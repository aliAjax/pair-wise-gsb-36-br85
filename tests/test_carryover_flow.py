import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo


class CarryoverFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.ids = seed_demo(self.db)
        self.up = self.ids["北区水库"]
        self.down = self.ids["河口灌区"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_batch_uses_demo_balances_and_seeds_next_year_pool(self):
        # 北区水库：许可 1000，已用 100（seed 中一笔计量），无待批 → 结余 900，触顶 200
        result = self.db.create_carryover_batch(
            "alice", {"year": 2026, "account_ids": [self.up, self.down], "note": "年度切换"}, "editor")
        by_account = {e["account_id"]: e for e in result["entries"]}
        self.assertEqual(by_account[self.up]["carry"], 200)
        self.assertEqual(by_account[self.up]["void"], 700)
        # 河口灌区：许可 500，未用水 → 结余 500，上限 100
        self.assertEqual(by_account[self.down]["carry"], 100)
        self.assertEqual(by_account[self.down]["void"], 400)
        balance = self.db.annual_balance(self.up, 2027)
        self.assertEqual(balance["permit_quota"], 1000)
        self.assertEqual(balance["pool_in"], 200)
        self.assertEqual(balance["available_total"], 1200)
        self.assertEqual(balance["carried_from_batch"]["batch_id"], result["batch_id"])

    def test_pending_transfer_reduces_carryover(self):
        # 待审批转出 400 要先扣：结余 500，仍触顶 200，但 pending_snapshot 留痕
        self.db.create_transfer(
            "alice", {"from_account_id": self.up, "to_account_id": self.down,
                      "amount": 400, "effective_date": "2026-06-01"}, "editor")
        result = self.db.create_carryover_batch(
            "alice", {"year": 2026, "account_ids": [self.up]}, "editor")
        entry = result["entries"][0]
        self.assertEqual(entry["pending_out"], 400)
        self.assertEqual(entry["unused"], 500)
        self.assertEqual(entry["carry"], 200)
        self.assertEqual(entry["void"], 300)

    def test_same_account_same_year_rejected_and_points_to_original(self):
        first = self.db.create_carryover_batch(
            "alice", {"year": 2026, "account_ids": [self.up]}, "editor")
        # 两个账户全是重复结转 → 拒绝，并在 skipped 中指出原批次
        other = self.db.create_carryover_batch(
            "alice", {"year": 2026, "account_ids": [self.down]}, "editor")
        with self.assertRaises(DomainError) as ctx:
            self.db.create_carryover_batch(
                "alice", {"year": 2026, "account_ids": [self.up, self.down]}, "editor")
        self.assertEqual(ctx.exception.status, 409)
        skipped = {s["account_id"]: s for s in ctx.exception.extra["skipped"]}
        self.assertEqual(skipped[self.up]["original_batch_id"], first["batch_id"])
        self.assertEqual(skipped[self.down]["original_batch_id"], other["batch_id"])

    def test_partial_duplicate_batch_skips_and_still_creates(self):
        first = self.db.create_carryover_batch(
            "alice", {"year": 2026, "account_ids": [self.up]}, "editor")
        second = self.db.create_carryover_batch(
            "alice", {"year": 2026, "account_ids": [self.up, self.down]}, "editor")
        self.assertEqual([e["account_id"] for e in second["entries"]], [self.down])
        self.assertEqual(second["skipped"][0]["original_batch_id"], first["batch_id"])
        # 河口灌区的结转池不受重复账户影响
        self.assertEqual(self.db.annual_balance(self.down, 2027)["pool_in"], 100)

    def test_next_year_usage_annual_first_then_pool(self):
        self.db.create_carryover_batch(
            "alice", {"year": 2026, "account_ids": [self.up, self.down]}, "editor")
        # 河口灌区 2027：许可 500，结转池 100。取水 550 → 当年 500 + 结转池 50
        draw = self.db.record_usage(
            "meter-02", {"account_id": self.down, "amount": 550,
                         "meter_event_id": "DOWN-2027-01", "occurred_at": "2027-03-01"}, "meter")
        self.assertEqual(draw["annual_taken"], 500)
        self.assertEqual(draw["pool_taken"], 50)
        balance = self.db.annual_balance(self.down, 2027)
        self.assertEqual(balance["annual_remaining"], 0)
        self.assertEqual(balance["pool_remaining"], 50)
        self.assertEqual(balance["available_total"], 50)
        # 再取 60 只剩结转池 50 → 拒绝
        with self.assertRaisesRegex(DomainError, "不足"):
            self.db.record_usage(
                "meter-02", {"account_id": self.down, "amount": 60,
                             "meter_event_id": "DOWN-2027-02", "occurred_at": "2027-04-01"}, "meter")

    def test_transfer_does_not_recognize_carryover_water(self):
        # 接收方用更低优先级账户，绕开“低优先级不能转给高优先级”规则
        receiver = self.db.create_account(
            "alice", {"name": "工业水厂", "region": "downstream", "holder": "水务集团",
                      "priority": 3, "valid_from": "2027-01-01", "valid_to": "2027-12-31",
                      "quota": 100}, "editor")
        recv_id = receiver["id"]
        self.db.create_carryover_batch(
            "alice", {"year": 2026, "account_ids": [self.down, recv_id]}, "editor")
        # 河口灌区 2027 先把当年许可用到剩 20，结转池有 100
        self.db.record_usage(
            "meter-02", {"account_id": self.down, "amount": 480,
                         "meter_event_id": "DOWN-2027-01", "occurred_at": "2027-03-01"}, "meter")
        # 可转让只有 20，哪怕可取水合计 120；尝试转 50 必须失败
        self.assertEqual(self.db.annual_balance(self.down, 2027)["transferable"], 20)
        with self.assertRaisesRegex(DomainError, "结转水量不可转让"):
            self.db.create_transfer(
                "alice", {"from_account_id": self.down, "to_account_id": recv_id,
                          "amount": 50, "effective_date": "2027-06-01"}, "editor")
        # 20 以内可以发起，批准后只移动当年许可额度，结转池不变
        transfer = self.db.create_transfer(
            "alice", {"from_account_id": self.down, "to_account_id": recv_id,
                      "amount": 20, "effective_date": "2027-06-01"}, "editor")
        self.db.approve_transfer(transfer["id"], "bob", "reviewer")
        balance = self.db.annual_balance(self.down, 2027)
        self.assertEqual(balance["permit_quota"], 480)
        self.assertEqual(balance["pool_in"], 100)

    def test_role_required_for_batch(self):
        with self.assertRaisesRegex(DomainError, "只有配额管理员"):
            self.db.create_carryover_batch(
                "bob", {"year": 2026, "account_ids": [self.up]}, "viewer")


if __name__ == "__main__":
    unittest.main()
