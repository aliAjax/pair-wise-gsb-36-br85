import unittest

from carryover import draw_water, plan_carryover, transfer_capacity


class CarryoverRuleTest(unittest.TestCase):
    def test_used_and_pending_are_deducted_first(self):
        # 许可 1000、已用 100、待批转出 300 → 结余 600
        plan = plan_carryover(1000, 100, 300)
        self.assertAlmostEqual(plan.unused, 600)
        # 两成上限 = 200，600 结余只能带走 200，其余 400 作废
        self.assertAlmostEqual(plan.cap, 200)
        self.assertAlmostEqual(plan.carry, 200)
        self.assertAlmostEqual(plan.void, 400)
        self.assertTrue(plan.capped)

    def test_small_unused_kept_in_full(self):
        plan = plan_carryover(1000, 900, 0)
        self.assertAlmostEqual(plan.unused, 100)
        self.assertAlmostEqual(plan.carry, 100)
        self.assertAlmostEqual(plan.void, 0)
        self.assertFalse(plan.capped)

    def test_overused_balance_is_never_negative(self):
        plan = plan_carryover(1000, 800, 300)
        self.assertAlmostEqual(plan.unused, 0)
        self.assertAlmostEqual(plan.carry, 0)
        self.assertAlmostEqual(plan.void, 0)

    def test_draw_uses_annual_quota_before_pool(self):
        draw = draw_water(annual_quota=500, annual_used=450, pool_in=200,
                          pool_used=0, amount=100)
        self.assertAlmostEqual(draw.annual_taken, 50)
        self.assertAlmostEqual(draw.pool_taken, 50)
        self.assertAlmostEqual(draw.annual_remaining, 0)
        self.assertAlmostEqual(draw.pool_remaining, 150)

    def test_draw_rejected_when_both_empty(self):
        with self.assertRaisesRegex(ValueError, "不足"):
            draw_water(500, 500, 100, 100, 1)

    def test_transfer_ignores_carryover_pool(self):
        # 当年许可剩余 50，结转池还有 200：可转让额度仍只有 50
        capacity = transfer_capacity(annual_quota=500, annual_used=420,
                                     pending_out=30)
        self.assertAlmostEqual(capacity, 50)

    def test_validation(self):
        with self.assertRaises(ValueError):
            plan_carryover(-1, 0, 0)
        with self.assertRaises(ValueError):
            draw_water(100, 0, 0, 0, 0)


if __name__ == "__main__":
    unittest.main()
