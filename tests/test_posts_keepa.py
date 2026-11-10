import unittest

from skd import posts
from skd.deals import Verdict
from skd.sources import keepa

P = posts.Product(product_id="min-spf50", name="Sunscreen SPF 50 <PA++++>", brand="Minimalist",
                  size="50g", store="amazon", asin="B0TESTASIN",
                  key_ingredient="Multi-filter", skin_types="oily, combination")
V = Verdict(True, "new low", avg_30d=399.0, low_60d=349.0, pct_below_avg=0.25, lowest_in_days=60)


class Posts(unittest.TestCase):
    def test_inr_grouping(self):
        self.assertEqual(posts.inr(299), "₹299")
        self.assertEqual(posts.inr(1299.6), "₹1,300")
        self.assertEqual(posts.inr(123456), "₹1,23,456")
        self.assertEqual(posts.inr(12345678), "₹1,23,45,678")

    def test_amazon_link_uses_tag(self):
        self.assertEqual(posts.affiliate_link(P, "skd-21"),
                         "https://www.amazon.in/dp/B0TESTASIN?tag=skd-21")

    def test_non_amazon_needs_pasted_link(self):
        q = posts.Product(product_id="x", name="n", brand="b", size="", store="nykaa")
        self.assertIsNone(posts.affiliate_link(q, "skd-21"))

    def test_telegram_escapes_html_and_discloses(self):
        t = posts.telegram_text(P, 299, V, "https://x.test/?a=1&b=2")
        self.assertIn("&lt;PA++++&gt;", t)
        self.assertIn('href="https://x.test/?a=1&amp;b=2"', t)
        self.assertIn("lowest in 60 days", t)
        self.assertIn("#ad", t)

    def test_whatsapp_plain_link_and_disclosure(self):
        w = posts.whatsapp_text(P, 299, V, "https://x.test/a")
        self.assertIn("*Minimalist Sunscreen SPF 50 <PA++++> (50g)*", w)
        self.assertIn("🛒 https://x.test/a", w)
        self.assertIn("25% lower", w)
        self.assertIn("#ad", w)


class Keepa(unittest.TestCase):
    def test_time_conversion(self):
        self.assertEqual(keepa.keepa_time_to_unix(0), 1293840000)  # 2011-01-01T00:00Z

    def test_current_prefers_amazon_then_new(self):
        self.assertEqual(keepa.current_price({"stats": {"current": [29900, 27900]}}), (299.0, True))
        self.assertEqual(keepa.current_price({"stats": {"current": [-1, 27900]}}), (279.0, True))
        self.assertEqual(keepa.current_price({"stats": {"current": [-1, -1]}}), (None, False))

    def test_history_parsing(self):
        h = keepa.price_history({"csv": [[0, 49900, 60, -1, 120, 39900]]})
        self.assertEqual(h, [(1293840000, 499.0), (1293843600, None), (1293847200, 399.0)])


if __name__ == "__main__":
    unittest.main()
