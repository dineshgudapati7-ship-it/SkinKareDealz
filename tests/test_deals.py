import unittest

from skd.deals import DAY, Rules, evaluate, split_current_run, time_weighted_average

NOW = 1_800_000_000


def daily(price, days, end=NOW):
    """One observation per day for `days` days before `end`."""
    return [(end - d * DAY, price) for d in range(days, 0, -1)]


class TimeWeightedAverage(unittest.TestCase):
    def test_uneven_sampling_is_weighted_by_duration(self):
        # 500 for 29 days, then 300 for 1 day (sampled 10x in that last day).
        start = NOW - 30 * DAY
        pts = [(start, 500)] + [(NOW - DAY + i * 8640, 300) for i in range(10)]
        avg = time_weighted_average(pts, start, NOW)
        self.assertAlmostEqual(avg, (500 * 29 + 300 * 1) / 30, places=6)

    def test_carry_in_fills_window_start(self):
        start = NOW - 10 * DAY
        avg = time_weighted_average([(NOW - 5 * DAY, 200)], start, NOW, carry_in=(start - DAY, 400))
        self.assertAlmostEqual(avg, 300)


class Evaluate(unittest.TestCase):
    def test_flat_price_is_not_a_deal(self):
        v = evaluate(500, True, NOW, daily(500, 60))
        self.assertFalse(v.is_deal)
        self.assertEqual(v.reason, "no real drop")

    def test_20pct_below_average_is_a_deal(self):
        v = evaluate(399, True, NOW, daily(500, 20))
        self.assertTrue(v.is_deal)
        self.assertAlmostEqual(v.pct_below_avg, 0.202, places=3)
        self.assertIsNone(v.lowest_in_days)  # only 20 days: can't claim a 60-day low

    def test_exactly_20pct_counts(self):
        self.assertTrue(evaluate(400, True, NOW, daily(500, 20)).is_deal)

    def test_small_dip_below_avg_is_not_enough_without_history(self):
        v = evaluate(450, True, NOW, daily(500, 20))   # 10% drop, < 30 days history
        self.assertFalse(v.is_deal)

    def test_new_60_day_low_with_enough_history(self):
        v = evaluate(449, True, NOW, daily(500, 45) + [(NOW - DAY // 2, 460)])
        self.assertTrue(v.is_deal)
        self.assertEqual(v.reason, "new low")
        self.assertEqual(v.lowest_in_days, 45)

    def test_equal_to_previous_low_is_not_new_low(self):
        past = daily(500, 59) + [(NOW - 30 * DAY, 449)]
        past.sort()
        v = evaluate(449, True, NOW, past)
        self.assertFalse(v.is_deal)

    def test_fake_mrp_discount_ignored(self):
        # Price never changes; "MRP" is irrelevant to the rule.
        self.assertFalse(evaluate(299, True, NOW, daily(299, 90)).is_deal)

    def test_out_of_stock(self):
        self.assertEqual(evaluate(None, False, NOW, daily(500, 60)).reason, "out of stock")

    def test_too_little_history(self):
        v = evaluate(100, True, NOW, daily(500, 3))
        self.assertFalse(v.is_deal)
        self.assertIn("history", v.reason)

    def test_cooldown_blocks_repost_at_same_price(self):
        v = evaluate(399, True, NOW, daily(500, 30), last_post=(NOW - 10 * 3600, 399))
        self.assertEqual(v.reason, "cooldown")

    def test_cooldown_allows_much_lower_price(self):
        v = evaluate(370, True, NOW, daily(500, 30), last_post=(NOW - 10 * 3600, 399))
        self.assertTrue(v.is_deal)

    def test_same_run_not_reposted_after_cooldown(self):
        run_start = NOW - 5 * DAY
        v = evaluate(399, True, NOW, daily(500, 30, end=run_start),
                     last_post=(run_start + 3600, 399), run_start=run_start)
        self.assertEqual(v.reason, "already posted")

    def test_price_bounce_then_drop_again_after_cooldown_posts(self):
        run_start = NOW - 3600
        past = daily(500, 30, end=run_start)
        v = evaluate(399, True, NOW, past, last_post=(NOW - 10 * DAY, 399), run_start=run_start)
        self.assertTrue(v.is_deal)

    def test_custom_rules(self):
        v = evaluate(450, True, NOW, daily(500, 20), rules=Rules(drop_pct=0.10))
        self.assertTrue(v.is_deal)


class SplitCurrentRun(unittest.TestCase):
    def test_keepa_change_point_is_not_history(self):
        pts = [(1, 500), (2, 500), (3, 399), (4, 399)]
        before, start = split_current_run(pts, 399)
        self.assertEqual(before, [(1, 500), (2, 500)])
        self.assertEqual(start, 3)

    def test_price_not_in_points(self):
        self.assertEqual(split_current_run([(1, 500)], 399), ([(1, 500)], None))


if __name__ == "__main__":
    unittest.main()
